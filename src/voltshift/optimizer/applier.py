"""Writing a configuration to the GPU, and reading one back.

Separated from the optimiser so the search can be tested without hardware,
and so there is exactly one place that understands ADLX's quirks.

The quirk that matters: `tuning.setVoltageOffset` is absolute on MGT2_1
(RDNA 4 — the value written *is* the offset) but relative on MGT2, where the
bridge adds the argument to the current voltage. A closed-loop optimiser that
re-applies "the same" configuration every few seconds would walk the voltage
steadily downward on an MGT2 card until it fell over. `TuningApplier` always
works in absolute terms and converts on the way out.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from ..bridgeclient import BridgeClient, BridgeError
from .space import (MAX_CLOCK, MIN_CLOCK, POWER_LIMIT, VOLTAGE, VRAM_CLOCK,
                    SearchSpace)


class TuningApplier:
    """Applies canonical configuration dicts to the GPU."""

    def __init__(self, bridge: BridgeClient, space: SearchSpace,
                 on_log: Optional[Callable[[str, str], None]] = None):
        self._bridge = bridge
        self._space = space
        self._lock = threading.Lock()
        self._on_log = on_log
        self._last_applied: Optional[dict] = None

    def _log(self, message: str, level: str = "info") -> None:
        if self._on_log:
            self._on_log(message, level)

    # ── reading ──────────────────────────────────────────────────────────────

    def read_current(self) -> dict:
        """Current values for every knob in the space, as absolute values."""
        raw = self._tuning_values(self._bridge.tuning_get())
        return {k.name: raw[k.name] for k in self._space.knobs if k.name in raw}

    @staticmethod
    def _tuning_values(tuning: dict) -> dict:
        gfx = tuning.get("gfx", {})
        vram = tuning.get("vram", {})
        power = tuning.get("power", {})
        raw = {
            VOLTAGE: gfx.get("voltageMv"),
            MAX_CLOCK: gfx.get("maxFreqMhz"),
            MIN_CLOCK: gfx.get("minFreqMhz"),
            VRAM_CLOCK: vram.get("maxFreqMhz"),
            POWER_LIMIT: power.get("powerLimit"),
        }
        return {name: value for name, value in raw.items() if value is not None}

    def _reset_extras(self, tuning: dict) -> dict:
        """Capture controls outside the optimiser before a global reset."""
        extras = {}
        for section, flag, field in (("vram", "timingSupported", "timing"),
                                     ("power", "tdcSupported", "tdcLimit")):
            data = tuning.get(section, {})
            if data.get(flag):
                if type(data.get(field)) is not int:
                    raise BridgeError(f"cannot preserve {field} across a factory reset")
                extras[field] = data[field]
        fan_supported = self._bridge.caps().get("tuning", {}).get("manualFan")
        if type(fan_supported) is not bool:
            raise BridgeError("cannot determine fan support before a factory reset")
        if fan_supported:
            fans = self._bridge.fans_get()
            if not fans.get("curve") or type(fans.get("zeroRpmSupported")) is not bool:
                raise BridgeError("cannot preserve fan tuning across a factory reset")
            if fans["zeroRpmSupported"] and type(fans.get("zeroRpm")) is not bool:
                raise BridgeError("cannot preserve ZeroRPM across a factory reset")
            extras["fans"] = fans
        return extras

    def _restore_extras(self, extras: dict) -> None:
        if "timing" in extras:
            self._bridge.set_memory_timing(extras["timing"])
        if "tdcLimit" in extras:
            self._bridge.set_tdc(extras["tdcLimit"])
        if "fans" in extras:
            fans = extras["fans"]
            self._bridge.set_fan_curve(fans["curve"])
            if fans["zeroRpmSupported"]:
                self._bridge.set_zero_rpm(fans["zeroRpm"])

    def _verify_reset_restore(self, config: dict, extras: dict) -> None:
        tuning = self._bridge.tuning_get()
        current = self._tuning_values(tuning)
        if any(current.get(name) != value for name, value in config.items()):
            raise BridgeError("tuning readback differs after restoring a factory reset")
        for section, field in (("vram", "timing"), ("power", "tdcLimit")):
            if field in extras and tuning.get(section, {}).get(field) != extras[field]:
                raise BridgeError(f"{field} was not restored after a factory reset")
        if "fans" in extras:
            expected, actual = extras["fans"], self._bridge.fans_get()
            if (actual.get("curve") != expected["curve"]
                    or (expected["zeroRpmSupported"]
                        and actual.get("zeroRpm") != expected["zeroRpm"])):
                raise BridgeError("fan tuning was not restored after a factory reset")

    def read_defaults(self) -> dict:
        """The card's factory values, where ADLX reports them."""
        tuning = self._bridge.tuning_get()
        gfx_defaults = tuning.get("gfx", {}).get("defaults", {})
        power_default = tuning.get("power", {}).get("powerLimitDefault")
        raw = {
            VOLTAGE: gfx_defaults.get("voltageMv"),
            MAX_CLOCK: gfx_defaults.get("maxFreqMhz"),
            MIN_CLOCK: gfx_defaults.get("minFreqMhz"),
            POWER_LIMIT: power_default,
        }
        return {k: v for k, v in raw.items() if v is not None}

    @property
    def last_applied(self) -> Optional[dict]:
        return dict(self._last_applied) if self._last_applied else None

    # ── writing ──────────────────────────────────────────────────────────────

    def apply(self, config: dict, skip_unchanged: bool = True) -> list[str]:
        """Write a configuration. Returns a log of what happened.

        Failed writes propagate to the session's recovery path. The cache is
        discarded on any failure because a driver call may partially apply.
        Raising an absolute MGT2 voltage needs a factory reset; do that before
        restoring the other controls so the reset cannot erase their writes.
        """
        applied: list[str] = []
        with self._lock:
            previous = self._last_applied or {}
            config = {k: v for k, v in config.items() if v is not None}
            reset_extras = None

            try:
                if VOLTAGE in config and not self._space.voltage_is_offset:
                    tuning = self._bridge.tuning_get()
                    current = self._tuning_values(tuning)
                    voltage = current.get(VOLTAGE)
                    if voltage is None:
                        raise BridgeError("cannot read current voltage to compute a delta")
                    if config[VOLTAGE] > voltage:
                        full_space = SearchSpace.from_tuning(tuning)
                        if any(name not in current for name in full_space.names):
                            raise BridgeError("cannot preserve incomplete tuning across a factory reset")
                        reset_extras = self._reset_extras(tuning)
                        # Include controls excluded from the active search space.
                        config = {**current, **config}
                        self._bridge.tuning_reset()
                        self._last_applied = None
                        previous = {}
            except Exception:
                self._last_applied = None
                raise

            def changed(name: str) -> bool:
                if name not in config or config[name] is None:
                    return False
                return not skip_unchanged or previous.get(name) != config[name]

            try:
                if changed(MIN_CLOCK) or changed(MAX_CLOCK):
                    self._bridge.set_core_clocks(config.get(MIN_CLOCK),
                                                 config.get(MAX_CLOCK))
                    applied.append("core clocks")
                if changed(VRAM_CLOCK):
                    self._bridge.set_vram_max(config[VRAM_CLOCK])
                    applied.append("vram clock")
                if changed(POWER_LIMIT):
                    self._bridge.set_power_limit(config[POWER_LIMIT])
                    applied.append("power limit")
                if changed(VOLTAGE):
                    self._write_voltage(config[VOLTAGE])
                    applied.append("voltage")
                if reset_extras is not None:
                    self._restore_extras(reset_extras)
                    self._verify_reset_restore(config, reset_extras)
            except Exception as exc:
                self._last_applied = None
                self._log(f"tuning write failed: {exc}", "error")
                raise

            merged = dict(previous)
            merged.update({k: v for k, v in config.items() if v is not None})
            self._last_applied = merged
        return applied

    def _write_voltage(self, target_mv: int) -> None:
        """Write an absolute voltage value, whatever the interface wants.

        On MGT2_1 the bridge treats the argument as the value to set, so it
        goes straight through. On MGT2 the bridge adds it to the current
        voltage, so the difference is what must be sent.
        """
        if self._space.voltage_is_offset:
            self._bridge.set_voltage_offset(int(target_mv))
            return
        current = self._bridge.tuning_get().get("gfx", {}).get("voltageMv")
        if current is None:
            raise BridgeError("cannot read current voltage to compute a delta")
        delta = int(target_mv) - int(current)
        if delta == 0:
            return
        if delta > 0:
            raise BridgeError("requested voltage exceeds the factory baseline")
        self._bridge.set_voltage_offset(delta)

    def reset(self) -> list[str]:
        """Restore AMD factory tuning and forget what we thought was applied."""
        with self._lock:
            try:
                self._bridge.tuning_reset()
                self._last_applied = None
                return ["factory reset"]
            except BridgeError as exc:
                self._last_applied = None
                self._log(f"factory reset failed: {exc}", "error")
                raise


class RecordingApplier:
    """In-memory applier for tests and dry runs."""

    def __init__(self, initial: Optional[dict] = None):
        self.history: list[dict] = []
        self.current = dict(initial or {})
        self.reset_count = 0

    def read_current(self) -> dict:
        return dict(self.current)

    def read_defaults(self) -> dict:
        return dict(self.current)

    def apply(self, config: dict, skip_unchanged: bool = True) -> list[str]:
        self.current.update({k: v for k, v in config.items() if v is not None})
        self.history.append(dict(self.current))
        return list(config.keys())

    def reset(self) -> list[str]:
        self.reset_count += 1
        return ["factory reset"]

    @property
    def last_applied(self) -> Optional[dict]:
        return dict(self.current) if self.current else None
