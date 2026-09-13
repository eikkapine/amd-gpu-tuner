"""Turning a measurement into a single number the optimiser can maximise.

Everything is scored as a *relative* change against the paired baseline, so
"3% more 1%-low fps" and "3% less power" are directly comparable without
anyone picking units. A goal preset is just a set of weights over those
relative deltas.

The 1% low is weighted above average fps on purpose. Undervolting usually
trades peak clocks for sustained clocks; average fps can drift upward while
the experience gets worse, and the low is what actually shows that.

When no frame source is installed there is nothing to say about frame pacing,
so the objective silently falls back to the hardware-only terms — efficiency,
thermals, noise — and reports that it did through `Score.blind`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from ..telemetry.window import PairedDelta, WindowStats, relative_paired_delta

# Above this hotspot the score is penalised regardless of the goal; running a
# card into its thermal limit is never the intent.
HOTSPOT_SOFT_LIMIT_C = 95.0
HOTSPOT_HARD_LIMIT_C = 105.0

# A stability event dominates every other term. Nothing gained is worth it.
INSTABILITY_PENALTY = 10.0


@dataclass(frozen=True)
class GoalWeights:
    label: str
    description: str
    fps_avg: float = 0.0
    fps_low: float = 0.0
    efficiency: float = 0.0
    power: float = 0.0
    stutter: float = 0.0
    thermal: float = 0.0
    noise: float = 0.0


GOALS: dict[str, GoalWeights] = {
    "max_fps": GoalWeights(
        label="Max FPS",
        description="Chase frame rate; accept the power and heat it costs.",
        fps_avg=1.0, fps_low=1.6, stutter=1.2, thermal=0.3),
    "balanced": GoalWeights(
        label="Balanced",
        description="Frame rate first, but do not pay much power or heat for it.",
        fps_avg=0.8, fps_low=1.4, efficiency=0.6, stutter=1.2, thermal=0.5, noise=0.15),
    "efficiency": GoalWeights(
        label="Efficiency",
        description="Most frames per watt. Small frame-rate losses are acceptable.",
        fps_avg=0.3, fps_low=0.6, efficiency=1.8, power=0.5, stutter=0.8, thermal=0.4),
    "silent": GoalWeights(
        label="Silent & Cool",
        description="Lowest heat and fan speed that keeps frame pacing intact.",
        fps_avg=0.15, fps_low=0.5, efficiency=0.8, power=0.7, stutter=1.0,
        thermal=1.2, noise=1.0),
    "benchmark": GoalWeights(
        label="Max Benchmark Score",
        description=("Highest possible score. Power and heat are spent freely, "
                     "limited only by the hard thermal guard."),
        fps_avg=1.6, fps_low=0.8, stutter=0.4, thermal=0.05),
}

DEFAULT_GOAL = "balanced"


@dataclass(frozen=True)
class Score:
    """A scored trial, with the parts kept visible so the UI can explain it."""

    value: float
    terms: dict[str, float]
    confidence: float
    blind: bool          # True when no frame data was available
    unstable: bool
    note: str = ""

    def explain(self) -> str:
        if self.unstable:
            return f"rejected — {self.note}"
        ranked = sorted(self.terms.items(), key=lambda kv: -abs(kv[1]))
        head = ", ".join(f"{name} {value:+.1%}" for name, value in ranked[:3] if value)
        return head or "no measurable change"


def _series(windows: Sequence[WindowStats], attr: str) -> list[Optional[float]]:
    return [getattr(w, attr) for w in windows]


def _weighted(delta: PairedDelta, weight: float) -> float:
    """Shrink a delta toward zero when the evidence for it is weak.

    Multiplying by confidence is what stops the optimiser from chasing a
    single lucky window: an unrepeatable 5% gain contributes almost nothing.
    """
    if weight == 0.0 or delta.pairs == 0:
        return 0.0
    return weight * delta.mean * max(0.25, delta.confidence)


def score_trial(candidate: Sequence[WindowStats], baseline: Sequence[WindowStats],
                goal: str = DEFAULT_GOAL,
                unstable_reason: Optional[str] = None) -> Score:
    """Score paired candidate/baseline windows under a goal preset."""
    weights = GOALS.get(goal, GOALS[DEFAULT_GOAL])

    if unstable_reason:
        return Score(-INSTABILITY_PENALTY, {}, 1.0, False, True, unstable_reason)

    if not candidate or not baseline:
        return Score(0.0, {}, 0.0, True, False, "no measurement")

    hotspots = [w.hotspot_max_c for w in candidate if w.hotspot_max_c is not None]
    if hotspots and max(hotspots) >= HOTSPOT_HARD_LIMIT_C:
        return Score(-INSTABILITY_PENALTY, {}, 1.0, True, True,
                     f"hotspot reached {max(hotspots):.0f}°C")

    # A frame source dropping out changes the efficiency units from FPS/W
    # to a clock-based proxy. Such windows cannot form a measured comparison.
    paired = [(c, b) for c, b in zip(candidate, baseline)
              if c.has_frames == b.has_frames and c.is_usable(1) and b.is_usable(1)]
    if not paired:
        return Score(0.0, {}, 0.0, True, False, "no comparable measurement")
    candidate, baseline = zip(*paired)

    has_frames = any(w.has_frames for w in candidate) and any(w.has_frames for w in baseline)
    terms: dict[str, float] = {}
    total = 0.0
    confidences: list[float] = []

    def add(name: str, attr: str, weight: float, invert: bool = False) -> None:
        nonlocal total
        if weight == 0.0:
            return
        delta = relative_paired_delta(_series(candidate, attr), _series(baseline, attr))
        if delta.pairs == 0:
            return
        signed = -delta.mean if invert else delta.mean
        contribution = _weighted(PairedDelta(signed, delta.stderr, delta.pairs), weight)
        terms[name] = signed
        total += contribution
        confidences.append(delta.confidence)

    if has_frames:
        add("fps", "fps_avg", weights.fps_avg)
        add("fps 1% low", "fps_p1", weights.fps_low)
        add("stutter", "stutter_ratio", weights.stutter, invert=True)

    add("efficiency", "perf_per_watt", weights.efficiency)
    add("power", "board_w", weights.power, invert=True)
    add("hotspot", "hotspot_c", weights.thermal, invert=True)
    add("fan", "fan_rpm", weights.noise, invert=True)

    # Absolute thermal guard, independent of how the baseline behaved.
    note = ""
    if hotspots:
        peak = max(hotspots)
        if peak >= HOTSPOT_HARD_LIMIT_C:
            return Score(-INSTABILITY_PENALTY, terms, 1.0, not has_frames, True,
                         f"hotspot reached {peak:.0f}°C")
        if peak > HOTSPOT_SOFT_LIMIT_C:
            over = (peak - HOTSPOT_SOFT_LIMIT_C) / 10.0
            total -= over
            note = f"hotspot {peak:.0f}°C"

    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return Score(total, terms, confidence, not has_frames, False, note)


def goal_choices() -> list[tuple[str, str]]:
    return [(key, weights.label) for key, weights in GOALS.items()]
