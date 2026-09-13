"""Window aggregation and paired comparison.

Auto-tuning compares configurations while the workload is moving underneath
it — a game's load changes with what is on screen, so "config A scored 140
fps at 10:00 and config B scored 145 fps at 10:02" says nothing on its own.

The answer is paired sampling: alternate baseline and candidate in short
windows and average the *differences*. Drift that affects both members of a
pair cancels out. `paired_delta` reports the mean difference together with
its standard error, so a caller can tell "3% faster, reliably" from "3%
faster, noise".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from .sample import Sample


@dataclass(frozen=True)
class WindowStats:
    """Aggregate of the samples collected during one measurement window."""

    duration_sec: float
    sample_count: int
    fps_avg: Optional[float]
    fps_p1: Optional[float]
    frametime_p99_ms: Optional[float]
    stutter_ratio: Optional[float]
    clock_mhz: Optional[float]
    hotspot_c: Optional[float]
    hotspot_max_c: Optional[float]
    board_w: Optional[float]
    fan_rpm: Optional[float]
    gpu_util_pct: Optional[float]
    perf_per_watt: Optional[float]
    has_frames: bool

    @classmethod
    def from_samples(cls, samples: Sequence[Sample]) -> "WindowStats":
        if not samples:
            return cls(0.0, 0, None, None, None, None, None, None, None,
                       None, None, None, None, False)

        def mean(values: list[float]) -> Optional[float]:
            usable = [v for v in values if v is not None and math.isfinite(v)]
            return sum(usable) / len(usable) if usable else None

        framed = [s for s in samples if s.frames is not None]
        duration = samples[-1].t - samples[0].t if len(samples) > 1 else 0.0

        fps_avg = mean([s.frames.fps_avg for s in framed]) if framed else None
        board_w = mean([s.board_w for s in samples])
        clock_mean = mean([s.clock_mhz for s in samples])
        perf_per_watt = None
        if fps_avg is not None and board_w:
            perf_per_watt = fps_avg / board_w
        elif board_w:
            # Blind mode: clock x utilisation, not utilisation alone. See
            # Sample.perf_per_watt — utilisation saturates under a GPU-bound
            # load, so on its own it would score a throttling undervolt as an
            # efficiency win.
            util = mean([s.gpu_util_pct for s in samples])
            if util is not None and clock_mean:
                perf_per_watt = (clock_mean * util / 100.0) / board_w
            elif util is not None:
                perf_per_watt = util / board_w

        hotspots = [s.hotspot_c for s in samples
                    if s.hotspot_c is not None and math.isfinite(s.hotspot_c)]

        return cls(
            duration_sec=duration,
            sample_count=len(samples),
            fps_avg=fps_avg,
            fps_p1=mean([s.frames.fps_p1 for s in framed]) if framed else None,
            frametime_p99_ms=(mean([s.frames.frametime_ms_p99 for s in framed])
                              if framed else None),
            stutter_ratio=(mean([s.frames.stutter_ratio for s in framed])
                           if framed else None),
            clock_mhz=mean([s.clock_mhz for s in samples]),
            hotspot_c=mean([s.hotspot_c for s in samples]),
            hotspot_max_c=max(hotspots) if hotspots else None,
            board_w=board_w,
            fan_rpm=mean([s.fan_rpm for s in samples]),
            gpu_util_pct=mean([s.gpu_util_pct for s in samples]),
            perf_per_watt=perf_per_watt,
            has_frames=bool(framed),
        )

    def is_usable(self, min_samples: int = 3) -> bool:
        return self.sample_count >= min_samples and any(
            value is not None and math.isfinite(value)
            for value in (self.clock_mhz, self.board_w, self.hotspot_c,
                          self.gpu_util_pct))


@dataclass(frozen=True)
class PairedDelta:
    """Mean difference between paired candidate and baseline windows."""

    mean: float
    stderr: float
    pairs: int

    @property
    def significant(self) -> bool:
        """True when the difference is at least two standard errors from zero.

        With few pairs the standard error is large, so this stays False until
        the evidence is real. That is deliberate: an optimiser that believes
        noise will happily chase it.

        A standard error of exactly zero means every pair showed the same
        difference, which is the strongest evidence available rather than the
        weakest — so it counts as significant whenever the difference is
        non-zero.
        """
        if self.pairs < 2:
            return False
        if self.stderr <= 0:
            return self.mean != 0.0
        return abs(self.mean) > 2 * self.stderr

    @property
    def confidence(self) -> float:
        """Rough 0..1 confidence that the sign of the difference is real."""
        if self.pairs < 2:
            return 0.0
        if self.stderr <= 0:
            return 1.0 if self.mean != 0.0 else 0.0
        z = abs(self.mean) / self.stderr
        return max(0.0, min(1.0, 1.0 - math.exp(-0.5 * z * z)))


def paired_delta(candidate_values: Sequence[Optional[float]],
                 baseline_values: Sequence[Optional[float]]) -> PairedDelta:
    """Mean of (candidate - baseline) over matched pairs, with standard error."""
    pairs = [(c, b) for c, b in zip(candidate_values, baseline_values)
             if c is not None and b is not None and math.isfinite(c) and math.isfinite(b)]
    if not pairs:
        return PairedDelta(0.0, 0.0, 0)

    diffs = [c - b for c, b in pairs]
    n = len(diffs)
    mean = sum(diffs) / n
    if n < 2:
        return PairedDelta(mean, 0.0, n)
    variance = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    stderr = math.sqrt(variance / n)
    return PairedDelta(mean, stderr, n)


def relative_paired_delta(candidate_values: Sequence[Optional[float]],
                          baseline_values: Sequence[Optional[float]]) -> PairedDelta:
    """Paired delta expressed as a fraction of the baseline value.

    Relative units let the objective weigh a 3% fps gain against a 3% power
    saving without caring that one is measured in frames and the other in
    watts.
    """
    pairs = [(c, b) for c, b in zip(candidate_values, baseline_values)
             if c is not None and b is not None and b != 0
             and math.isfinite(c) and math.isfinite(b)]
    if not pairs:
        return PairedDelta(0.0, 0.0, 0)
    ratios = [(c - b) / abs(b) for c, b in pairs]
    n = len(ratios)
    mean = sum(ratios) / n
    if n < 2:
        return PairedDelta(mean, 0.0, n)
    variance = sum((r - mean) ** 2 for r in ratios) / (n - 1)
    return PairedDelta(mean, math.sqrt(variance / n), n)
