"""Fused telemetry sample types.

A `Sample` is one instant of the machine's state: GPU hardware counters read
from the bridge, joined with frame-pacing statistics read from a frame source
(PresentMon or RTSS). The optimizer scores configurations off these, so the
types here are the contract between "what the hardware did" and "was that
better".

Frame statistics are optional: on a desktop with nothing presenting, or when
no frame source is installed, `Sample.frames` is None and consumers fall back
to hardware-only reasoning.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(sorted_values[int(rank)])
    weight = rank - low
    return float(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)


def _worst_percent_fps(sorted_frametimes: Sequence[float], fraction: float) -> float:
    """Average FPS across the slowest `fraction` of frames.

    This is the modern "1% low" definition (mean of the worst 1% of frames)
    rather than "FPS at the 99th percentile frametime" — it reacts to the
    depth of a stutter, not just its existence.
    """
    if not sorted_frametimes:
        return 0.0
    count = max(1, int(len(sorted_frametimes) * fraction))
    worst = sorted_frametimes[-count:]
    mean_ms = sum(worst) / len(worst)
    return 1000.0 / mean_ms if mean_ms > 0 else 0.0


@dataclass(frozen=True)
class FrameStats:
    """Frame-pacing statistics aggregated over a rolling window."""

    process: str
    pid: int
    frame_count: int
    fps_avg: float
    fps_p1: float           # mean FPS of the worst 1% of frames
    fps_p01: float          # mean FPS of the worst 0.1% of frames
    frametime_ms_avg: float
    frametime_ms_p99: float
    stutter_ratio: float    # fraction of frames longer than 2x the median
    gpu_busy_ms_avg: Optional[float]
    source: str

    @classmethod
    def from_frametimes(cls, frametimes_ms: Sequence[float], process: str, pid: int,
                        source: str, gpu_busy_ms: Optional[Sequence[float]] = None
                        ) -> Optional["FrameStats"]:
        """Build stats from raw per-frame present intervals (milliseconds)."""
        usable = [ft for ft in frametimes_ms
                  if ft is not None and math.isfinite(ft) and ft > 0]
        if len(usable) < 2:
            return None

        ordered = sorted(usable)
        mean_ms = sum(usable) / len(usable)
        median_ms = _percentile(ordered, 0.5)
        stutter_threshold = median_ms * 2.0
        stutters = sum(1 for ft in usable if ft > stutter_threshold)

        busy_avg = None
        if gpu_busy_ms:
            busy = [b for b in gpu_busy_ms
                    if b is not None and math.isfinite(b) and b >= 0]
            if busy:
                busy_avg = sum(busy) / len(busy)

        return cls(
            process=process,
            pid=pid,
            frame_count=len(usable),
            fps_avg=1000.0 / mean_ms if mean_ms > 0 else 0.0,
            fps_p1=_worst_percent_fps(ordered, 0.01),
            fps_p01=_worst_percent_fps(ordered, 0.001),
            frametime_ms_avg=mean_ms,
            frametime_ms_p99=_percentile(ordered, 0.99),
            stutter_ratio=stutters / len(usable),
            gpu_busy_ms_avg=busy_avg,
            source=source,
        )


@dataclass(frozen=True)
class Sample:
    """One fused reading: GPU counters + (optionally) frame statistics."""

    t: float                          # time.monotonic() when sampled
    clock_mhz: Optional[int] = None
    vram_clock_mhz: Optional[int] = None
    temp_c: Optional[float] = None
    hotspot_c: Optional[float] = None
    intake_c: Optional[float] = None
    board_w: Optional[float] = None
    fan_rpm: Optional[int] = None
    gpu_util_pct: Optional[float] = None
    vram_used_mb: Optional[int] = None
    voltage_mv: Optional[int] = None
    applied_offset_mv: Optional[int] = None
    frames: Optional[FrameStats] = None
    raw: dict = None  # type: ignore[assignment]

    @classmethod
    def from_metrics(cls, metrics: dict, t: float,
                     frames: Optional[FrameStats] = None,
                     applied_offset_mv: Optional[int] = None) -> "Sample":
        def value(name):
            raw = metrics.get(name)
            return raw if (isinstance(raw, (int, float))
                           and not isinstance(raw, bool) and math.isfinite(raw)) else None

        power = value("boardPowerW")
        if power is None:
            power = value("powerW")
        return cls(
            t=t,
            clock_mhz=value("clockMhz"),
            vram_clock_mhz=value("vramClockMhz"),
            temp_c=value("tempC"),
            hotspot_c=value("hotspotC"),
            intake_c=value("intakeC"),
            board_w=power,
            fan_rpm=value("fanRpm"),
            gpu_util_pct=value("usagePct"),
            vram_used_mb=value("vramUsedMb"),
            voltage_mv=value("voltageMv"),
            applied_offset_mv=applied_offset_mv,
            frames=frames,
            raw=metrics,
        )

    @property
    def fps(self) -> Optional[float]:
        return self.frames.fps_avg if self.frames else None

    @property
    def perf_per_watt(self) -> Optional[float]:
        """Frames per joule when frames are known, else a throughput proxy.

        The blind fallback deliberately does *not* use utilisation per watt.
        Under a GPU-bound load utilisation pins near 90% whether frames are
        arriving well or badly, so `util / watts` would score any power
        reduction as a win — including one caused by an undervolt throttling
        the card. Measured on a real session: utilisation held 87-92% while
        the clock carried all the actual signal.

        Multiplying by clock fixes that. Downclocking is how an unstable or
        over-aggressive undervolt actually loses performance, and the clock
        term registers it immediately.
        """
        if not self.board_w or self.board_w <= 0:
            return None
        if self.frames:
            return self.frames.fps_avg / self.board_w
        if self.gpu_util_pct is not None and self.clock_mhz:
            return (self.clock_mhz * self.gpu_util_pct / 100.0) / self.board_w
        if self.gpu_util_pct is not None:
            return self.gpu_util_pct / self.board_w
        return None

    def as_dict(self) -> dict:
        """Flat dict for the GUI's existing sample sinks and the crash logger."""
        out = {
            "clockMhz": self.clock_mhz,
            "vramClockMhz": self.vram_clock_mhz,
            "tempC": self.temp_c,
            "hotspotC": self.hotspot_c,
            "intakeC": self.intake_c,
            "boardPowerW": self.board_w,
            "fanRpm": self.fan_rpm,
            "usagePct": self.gpu_util_pct,
            "vramUsedMb": self.vram_used_mb,
            "voltageMv": self.voltage_mv,
            "appliedOffsetMv": self.applied_offset_mv,
        }
        if self.frames:
            out.update({
                "fps": self.frames.fps_avg,
                "fpsLow1": self.frames.fps_p1,
                "fpsLow01": self.frames.fps_p01,
                "frametimeMs": self.frames.frametime_ms_avg,
                "frametimeP99Ms": self.frames.frametime_ms_p99,
                "stutterRatio": self.frames.stutter_ratio,
                "frameProcess": self.frames.process,
                "frameSource": self.frames.source,
            })
        return out
