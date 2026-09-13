"""Which controls this card actually honours.

ADLX reporting a range for a knob is not a promise that writing to it does
anything. Some fields are advertised on hardware that silently ignores them,
and which fields those are varies by architecture, by card, and by driver
build. Trusting the advertised range means the optimiser can spend trials
moving a control that is inert — and worse, can attribute another knob's
effect to it.

The fix is not a hand-maintained table of GPU models. That would be stale the
week a driver ships and useless for cards nobody has tested. Instead
VoltShift *asks the hardware*: write a small, deliberately safe delta, read
the value back, and see whether it took. A knob that does not read back is
dropped from the search space and the result is remembered per card.

A short table of architecture-level facts still exists below, but only for
things a read-back cannot reveal — semantics rather than support.

Verification writes to the GPU, so it never runs silently at startup. It runs
once per card before the first auto-tune session, and can be re-run on demand
with `voltshift verify --force`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .bridgeclient import BridgeError
from .optimizer.space import (MAX_CLOCK, MIN_CLOCK, POWER_LIMIT, VOLTAGE,
                              VRAM_CLOCK, SearchSpace)

# Seconds to wait after a write before reading it back. ADLX applies
# asynchronously; reading immediately can return the previous value and
# report a working knob as dead.
READBACK_DELAY_SEC = 0.4

# Architecture notes that a read-back cannot tell you. These describe what a
# value *means*, not whether it is supported — support is always measured.
ARCH_NOTES = {
    "MGT2_1": (
        "RDNA 4 (RX 9000 series): GPU voltage and max core clock are both "
        "offsets from stock, not absolute values, and read 0 at defaults. "
        "Minimum-clock tuning is not exposed by this interface."
    ),
    "MGT2": (
        "RDNA 2/3: GPU voltage is an absolute value. AMD GPU Tuner converts "
        "absolute targets to the deltas this interface expects."
    ),
    "MGT1": (
        "Legacy VF-curve interface: voltage is applied by shifting every "
        "point on the voltage/frequency curve."
    ),
}


@dataclass(frozen=True)
class KnobCheck:
    name: str
    supported: bool
    detail: str


def _probe_delta(knob) -> Optional[int]:
    """The smallest safe test movement for a knob.

    Direction matters. Where a knob has room in both directions the
    conservative one is chosen — down for voltage, clocks and power — so a
    verification pass never briefly overclocks or overvolts the card while
    finding out whether the control works.
    """
    step = max(1, knob.step)
    current = knob.default
    if current is None:
        return None

    prefer_down = knob.name in (VOLTAGE, MAX_CLOCK, MIN_CLOCK, POWER_LIMIT)
    magnitude = step if knob.name != VOLTAGE else max(step, 5)

    down_ok = current - magnitude >= knob.low
    up_ok = current + magnitude <= knob.high

    if prefer_down and down_ok:
        return -magnitude
    if down_ok:
        return -magnitude
    if up_ok:
        return magnitude
    return None


def verify_knob(applier, knob, log: Optional[Callable[[str, str], None]] = None
                ) -> KnobCheck:
    """Write a small delta, read it back, restore. Report whether it stuck."""
    delta = _probe_delta(knob)
    if delta is None:
        return KnobCheck(knob.name, False, "no headroom to test")

    try:
        before = applier.read_current().get(knob.name)
    except Exception as exc:
        return KnobCheck(knob.name, False, f"could not read current value ({exc})")
    if before is None:
        return KnobCheck(knob.name, False, "not reported by the driver")

    target = knob.clamp(before + delta)
    if target == before:
        return KnobCheck(knob.name, False, "no distinct value to test")

    try:
        applier.apply({knob.name: target}, skip_unchanged=False)
        time.sleep(READBACK_DELAY_SEC)
        after = applier.read_current().get(knob.name)
    except Exception as exc:
        return KnobCheck(knob.name, False, f"write failed ({exc})")
    finally:
        # A failed or silently ignored restore must stop the entire verification
        # pass. It cannot be cached as "verified" while the probe remains live.
        try:
            applier.apply({knob.name: before}, skip_unchanged=False)
        except Exception:
            pass  # The readback below establishes whether it is already restored.
        time.sleep(READBACK_DELAY_SEC)
        try:
            restored = applier.read_current().get(knob.name)
        except Exception as exc:
            raise BridgeError(f"Cannot verify restoration of {knob.name}; tuning stopped") from exc
        if restored != before:
            raise BridgeError(f"Restoration of {knob.name} failed: expected {before}, got {restored}; tuning stopped")

    if after is None:
        return KnobCheck(knob.name, False, "value disappeared after writing")
    if after == target:
        return KnobCheck(knob.name, True, f"verified ({before} → {target} → restored)")
    if after == before:
        return KnobCheck(knob.name, False,
                         f"driver ignored the write (asked {target}, still {before})")
    return KnobCheck(knob.name, False,
                     f"driver clamped the write (asked {target}, got {after})")


def verify_space(applier, space: SearchSpace, knowledge=None, gpu_key: str = "",
                 log: Optional[Callable[[str, str], None]] = None,
                 force: bool = False) -> tuple[SearchSpace, list[KnobCheck]]:
    """Return a search space containing only knobs this card honours.

    Results are cached per card. Without `force` a card that has already been
    verified is trusted, so the GPU is written to once rather than on every
    launch.
    """
    def emit(message: str, level: str = "info") -> None:
        if log:
            log(message, level)

    if not space:
        return space, []

    cached = None
    if knowledge is not None and gpu_key and not force:
        cached = knowledge.knob_support(gpu_key)

    if cached:
        supported = {name for name, ok in cached.items() if ok}
        unknown = [k for k in space.knobs if k.name not in cached]
        # A knob the cache has never seen still needs testing; anything else
        # comes straight from the cache.
        if not unknown:
            checks = [KnobCheck(name, ok, "cached") for name, ok in cached.items()]
            return _filtered(space, supported), checks

    emit("verifying which tuning controls this GPU honours "
         "(small test writes, restored immediately)")

    checks: list[KnobCheck] = []
    for knob in list(space.knobs):
        if cached is not None and knob.name in cached and not force:
            checks.append(KnobCheck(knob.name, cached[knob.name], "cached"))
            continue
        check = verify_knob(applier, knob, log)
        checks.append(check)
        emit(f"  {knob.name:<18} {'ok' if check.supported else 'unsupported'} "
             f"— {check.detail}", "info" if check.supported else "warn")

    if knowledge is not None and gpu_key:
        knowledge.record_knob_support(
            gpu_key, {c.name: c.supported for c in checks})

    supported = {c.name for c in checks if c.supported}
    dropped = [c.name for c in checks if not c.supported]
    if dropped:
        emit(f"excluded from tuning on this card: {', '.join(dropped)}", "warn")

    return _filtered(space, supported), checks


def _filtered(space: SearchSpace, supported: set[str]) -> SearchSpace:
    kept = [k for k in space.knobs if k.name in supported]
    return SearchSpace(knobs=kept, voltage_is_offset=space.voltage_is_offset,
                       interface=space.interface)


def arch_note(space: SearchSpace) -> str:
    return ARCH_NOTES.get(space.interface, "")
