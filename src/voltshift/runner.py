"""Engine runner — drives the dynamic voltage engine from telemetry.

Reads metrics, feeds the dynamic voltage engine, applies its decisions, and
records telemetry into the crash logger. Consumers subscribe via callbacks;
nothing here touches a UI.

Two modes. Given a `TelemetryHub` the runner subscribes to it and does no
polling of its own, so the whole app reads the bridge once per interval.
Without a hub it falls back to owning a polling thread, which keeps the CLI
and the unit tests independent of the telemetry stack.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .bridgeclient import BridgeClient, BridgeError
from .crashlog import CrashLogger
from .engine import DynamicVoltageEngine, EngineConfig


class EngineRunner:
    def __init__(self, bridge: BridgeClient, config: EngineConfig,
                 crash_logger: Optional[CrashLogger] = None,
                 hub=None):
        self._bridge = bridge
        self._requested_config = config
        self._engine = DynamicVoltageEngine(config)
        self._crash_logger = crash_logger
        self._hub = hub
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._stop.set()
        self._subscribed = False
        self._processing_lock = threading.RLock()
        self._started = False

        # Callbacks (called from the runner or hub thread).
        self.on_sample: Optional[Callable[[dict], None]] = None            # every poll
        self.on_voltage_change: Optional[Callable[[Optional[int], int], None]] = None
        self.on_log_entry: Optional[Callable[[str, str], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    @property
    def engine(self) -> DynamicVoltageEngine:
        return self._engine

    @property
    def running(self) -> bool:
        if self._hub is not None:
            return self._subscribed
        return self._thread is not None and self._thread.is_alive()

    def _log(self, msg: str, level: str = "info") -> None:
        if self.on_log_entry:
            self.on_log_entry(msg, level)

    def start(self) -> None:
        with self._processing_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        gfx = self._bridge.tuning_get().get("gfx", {})
        if not isinstance(gfx, dict):
            raise BridgeError("Dynamic voltage cannot read GPU tuning capabilities")
        voltage_range = gfx.get("voltageRange", {})
        if not isinstance(voltage_range, dict):
            raise BridgeError("Dynamic voltage cannot read the voltage-offset range")
        low, high, step = (voltage_range.get(key) for key in ("min", "max", "step"))
        if (gfx.get("interface") != "MGT2_1"
                or any(type(value) is not int for value in (low, high, step))
                or not low <= high <= 0 or step <= 0):
            raise BridgeError("Dynamic voltage requires a supported MGT2_1 voltage-offset range")
        requested = [self._requested_config.idle_offset_mv,
                     *(t.offset_mv for t in self._requested_config.thresholds)]
        effective = [self._engine.config.idle_offset_mv,
                     *(t.offset_mv for t in self._engine.config.thresholds)]
        for value, applied in zip(requested, effective):
            if (type(value) is not int or value != applied
                    or not low <= value <= high or (value - low) % step):
                raise BridgeError(
                    f"Configured offset {value!r} mV is invalid; use {low}..{high} mV "
                    f"in steps of {step}, within the engine's -200..0 mV limits"
                )
        self._engine.reset()
        self._stop.clear()
        self._started = True
        if self._hub is not None:
            self._unsubscribe = self._hub.subscribe(self._on_hub_sample)
            self._subscribed = True
            return
        self._thread = threading.Thread(target=self._run, name="voltshift-engine",
                                        daemon=True)
        self._thread.start()

    def stop(self, reset_gpu: bool = True) -> None:
        """Stop polling; by default restores AMD factory tuning."""
        self._stop.set()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._subscribed = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=self._engine.config.poll_interval_sec + 3)
            if not self._thread.is_alive():
                self._thread = None
        # A sample already inside the bridge must finish before the reset.
        # A queued sample sees _stop under this same lock and cannot write.
        with self._processing_lock:
            was_started, self._started = self._started, False
            if reset_gpu and was_started:
                try:
                    self._bridge.tuning_reset()
                    self._log("GPU restored to factory tuning")
                except BridgeError as exc:
                    self._log(f"Factory reset failed: {exc}", "error")
            self._engine.reset()
            if self._hub is not None:
                self._hub.set_applied_offset(None)

    def _run(self) -> None:
        interval = self._engine.config.poll_interval_sec
        consecutive_errors = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                metrics = self._bridge.metrics()
                consecutive_errors = 0
            except BridgeError as exc:
                consecutive_errors += 1
                self._log(f"Metrics read failed: {exc}", "error")
                if consecutive_errors >= 5 and self.on_error:
                    self.on_error(f"Bridge unreachable ({exc})")
                    break
                self._stop.wait(interval)
                continue

            self._process_metrics(metrics)

            elapsed = time.monotonic() - started
            self._stop.wait(max(0.05, interval - elapsed))

    def _on_hub_sample(self, sample) -> None:
        """Hub-driven path: one telemetry Sample instead of a raw metrics dict."""
        if self._stop.is_set():
            return
        self._process_metrics(sample.raw or sample.as_dict(), sample=sample)

    def _process_metrics(self, metrics: dict, sample=None) -> None:
        """Shared per-reading logic for both the self-polled and hub paths."""
        with self._processing_lock:
            if self._stop.is_set():
                return
            self._process_metrics_locked(metrics, sample)

    def _process_metrics_locked(self, metrics: dict, sample=None) -> None:
        clock = metrics.get("clockMhz")
        if clock is not None:
            previous = self._engine.current_mv
            decision = self._engine.step(int(clock))
            if decision.applied_mv is not None:
                try:
                    self._bridge.set_voltage_offset(decision.applied_mv)
                except BridgeError as exc:
                    # The pure engine commits its decision before I/O. Forget
                    # that unacknowledged write so telemetry reports unknown
                    # and the same target can be retried after hysteresis.
                    self._engine.reset()
                    if self._hub is not None:
                        self._hub.set_applied_offset(None)
                    self._log(f"Voltage write failed: {exc}", "error")
                else:
                    self._log(f"{clock} MHz  →  {decision.applied_mv:+d} mV", "volt")
                    if self.on_voltage_change:
                        self.on_voltage_change(previous, decision.applied_mv)
                    if self._crash_logger:
                        self._crash_logger.on_voltage_changed(previous, decision.applied_mv)
                    if self._hub is not None:
                        self._hub.set_applied_offset(decision.applied_mv)

        if self._crash_logger and clock is not None:
            self._crash_logger.record(
                clock=int(clock),
                voltage=self._engine.current_mv,
                temp=metrics.get("tempC"),
                hotspot=metrics.get("hotspotC"),
                power=metrics.get("boardPowerW", metrics.get("powerW")),
                fan_rpm=metrics.get("fanRpm"),
            )

        if self.on_sample:
            payload = sample.as_dict() if sample is not None else dict(metrics)
            payload["appliedOffsetMv"] = self._engine.current_mv
            self.on_sample(payload)
