"""The search space: which knobs exist, and what values they may take.

Bounds are never invented. Every range comes from `tuning.get`, which reports
what ADLX says the hardware allows, so the optimiser physically cannot
propose a value the driver would reject.

One subtlety worth stating plainly. ADLX exposes GPU voltage differently by
interface generation: on MGT2_1 (RDNA 4 and later) the value *is* an offset
in millivolts, negative for an undervolt; on MGT2 it is an absolute voltage.
The knob here always carries the raw ADLX value with the raw ADLX range, and
`SearchSpace.voltage_is_offset` records which meaning applies. Either way
lower means more aggressive, so the optimiser needs no special case — only
the applier does.
"""

from __future__ import annotations

import random
import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Knobs the optimiser may touch, in the order they are applied.
VOLTAGE = "voltage_mv"
POWER_LIMIT = "power_limit_pct"
MAX_CLOCK = "max_clock_mhz"
MIN_CLOCK = "min_clock_mhz"
VRAM_CLOCK = "vram_max_mhz"

# Conservative per-application movement caps. A single write never moves a
# knob further than this, so even a badly wrong proposal lands somewhere the
# hardware can recover from.
DEFAULT_MAX_DELTA = {
    VOLTAGE: 40,        # mV
    POWER_LIMIT: 10,    # %
    MAX_CLOCK: 200,     # MHz
    MIN_CLOCK: 200,     # MHz
    VRAM_CLOCK: 100,    # MHz
}


@dataclass(frozen=True)
class Knob:
    name: str
    low: int
    high: int
    step: int = 1
    default: Optional[int] = None
    unit: str = ""
    max_delta: Optional[int] = None

    @property
    def span(self) -> int:
        return max(1, self.high - self.low)

    def clamp(self, value: float) -> int:
        step = max(1, self.step)
        index = max(0, min((self.high - self.low) // step,
                           round((value - self.low) / step)))
        return int(self.low + index * step)

    def normalise(self, value: float) -> float:
        return (float(value) - self.low) / self.span

    def denormalise(self, unit_value: float) -> int:
        return self.clamp(self.low + unit_value * self.span)


def _range_of(section: dict, key: str) -> Optional[dict]:
    value = section.get(key)
    if not isinstance(value, dict):
        return None
    if "min" not in value or "max" not in value:
        return None
    low, high, step = value["min"], value["max"], value.get("step", 1)
    if any(not isinstance(v, (int, float)) or isinstance(v, bool)
           or not math.isfinite(v) or int(v) != v for v in (low, high, step)):
        return None
    if low >= high or step <= 0 or high - low < step:
        return None
    return value


@dataclass
class SearchSpace:
    knobs: list[Knob] = field(default_factory=list)
    voltage_is_offset: bool = True
    interface: str = "unknown"

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_tuning(cls, tuning: dict, enabled: Optional[Iterable[str]] = None
                    ) -> "SearchSpace":
        """Build the space from a `tuning.get` payload."""
        allow = set(enabled) if enabled is not None else None
        knobs: list[Knob] = []

        def add(name: str, rng: Optional[dict], current, unit: str) -> None:
            if rng is None:
                return
            if allow is not None and name not in allow:
                return
            knobs.append(Knob(
                name=name,
                low=int(rng["min"]),
                high=int(rng["max"]),
                step=max(1, int(rng.get("step", 1) or 1)),
                default=int(current) if current is not None else None,
                unit=unit,
                max_delta=DEFAULT_MAX_DELTA.get(name),
            ))

        gfx = tuning.get("gfx", {})
        interface = gfx.get("interface", "unknown")
        if "unsupported" not in gfx:
            add(VOLTAGE, _range_of(gfx, "voltageRange"), gfx.get("voltageMv"), "mV")
            add(MAX_CLOCK, _range_of(gfx, "maxFreqRange"), gfx.get("maxFreqMhz"), "MHz")
            add(MIN_CLOCK, _range_of(gfx, "minFreqRange"), gfx.get("minFreqMhz"), "MHz")

        vram = tuning.get("vram", {})
        if "unsupported" not in vram:
            add(VRAM_CLOCK, _range_of(vram, "maxFreqRange"), vram.get("maxFreqMhz"), "MHz")

        power = tuning.get("power", {})
        if "unsupported" not in power:
            add(POWER_LIMIT, _range_of(power, "powerLimitRange"),
                power.get("powerLimit"), "%")

        return cls(knobs=knobs, voltage_is_offset=(interface == "MGT2_1"),
                   interface=interface)

    # ── lookups ──────────────────────────────────────────────────────────────

    @property
    def names(self) -> list[str]:
        return [k.name for k in self.knobs]

    @property
    def dimensions(self) -> int:
        return len(self.knobs)

    def knob(self, name: str) -> Optional[Knob]:
        for k in self.knobs:
            if k.name == name:
                return k
        return None

    def __bool__(self) -> bool:
        return bool(self.knobs)

    # ── config <-> vector ────────────────────────────────────────────────────

    def default_config(self) -> dict:
        return {k.name: (k.default if k.default is not None else k.clamp((k.low + k.high) / 2))
                for k in self.knobs}

    def clamp_config(self, config: dict) -> dict:
        return {k.name: k.clamp(config.get(k.name, k.default if k.default is not None
                                            else (k.low + k.high) / 2))
                for k in self.knobs}

    def to_vector(self, config: dict) -> list[float]:
        out = []
        for k in self.knobs:
            value = config.get(k.name)
            if value is None:
                value = k.default if k.default is not None else (k.low + k.high) / 2
            out.append(max(0.0, min(1.0, k.normalise(value))))
        return out

    def from_vector(self, vector: Iterable[float]) -> dict:
        return {k.name: k.denormalise(max(0.0, min(1.0, v)))
                for k, v in zip(self.knobs, vector)}

    def distance(self, a: dict, b: dict) -> float:
        """Euclidean distance in normalised space (0..sqrt(dimensions))."""
        va, vb = self.to_vector(a), self.to_vector(b)
        return sum((x - y) ** 2 for x, y in zip(va, vb)) ** 0.5

    # ── sampling ─────────────────────────────────────────────────────────────

    def random_config(self, rng: Optional[random.Random] = None) -> dict:
        rng = rng or random
        return self.from_vector([rng.random() for _ in self.knobs])

    def perturb(self, config: dict, scale: float = 0.1,
                rng: Optional[random.Random] = None) -> dict:
        """A random neighbour of `config`, `scale` being the normalised sigma."""
        rng = rng or random
        vector = self.to_vector(config)
        return self.from_vector([v + rng.gauss(0.0, scale) for v in vector])

    def limit_step(self, target: dict, current: dict) -> dict:
        """Clip `target` so no knob moves further than its per-step cap."""
        limited = {}
        for k in self.knobs:
            want = k.clamp(target.get(k.name, current.get(k.name, k.low)))
            have = current.get(k.name)
            if have is None or k.max_delta is None:
                limited[k.name] = want
                continue
            have = k.clamp(have)
            delta = want - have
            if abs(delta) > k.max_delta:
                want = have + (k.max_delta if delta > 0 else -k.max_delta)
            limited[k.name] = k.clamp(want)
        return limited

    def describe(self, config: dict) -> str:
        """Human-readable configuration, signed where the field is relative.

        On MGT2_1 several fields ADLX exposes are offsets rather than absolute
        values — voltage, max core clock and power limit all read 0 at stock
        with a range spanning zero. Rendering those with an explicit sign
        stops "max_clock_mhz=-169MHz" from looking like an absolute clock.
        """
        parts = []
        for k in self.knobs:
            value = config.get(k.name)
            if value is None:
                continue
            relative = k.low < 0
            parts.append(f"{k.name}={value:+d}{k.unit}" if relative
                         else f"{k.name}={value}{k.unit}")
        return "  ".join(parts)
