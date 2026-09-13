import time

import pytest

from voltshift.telemetry.sample import FrameStats, Sample, _percentile
from voltshift.telemetry.window import (WindowStats, paired_delta,
                                        relative_paired_delta)


def test_percentile_interpolates():
    values = [1.0, 2.0, 3.0, 4.0]
    assert _percentile(values, 0.0) == 1.0
    assert _percentile(values, 1.0) == 4.0
    assert _percentile(values, 0.5) == pytest.approx(2.5)


def test_frame_stats_needs_two_frames():
    assert FrameStats.from_frametimes([16.6], "g.exe", 1, "test") is None
    assert FrameStats.from_frametimes([], "g.exe", 1, "test") is None


def test_frame_stats_basic_maths():
    frametimes = [10.0] * 100
    stats = FrameStats.from_frametimes(frametimes, "game.exe", 42, "test")
    assert stats.frame_count == 100
    assert stats.fps_avg == pytest.approx(100.0)
    assert stats.fps_p1 == pytest.approx(100.0)
    assert stats.stutter_ratio == 0.0
    assert stats.process == "game.exe"
    assert stats.pid == 42


def test_one_percent_low_reflects_worst_frames():
    # 99 good frames and one terrible one: the average barely moves but the
    # 1% low collapses, which is the whole reason it is the primary metric.
    frametimes = [10.0] * 99 + [100.0]
    stats = FrameStats.from_frametimes(frametimes, "g.exe", 1, "test")
    assert stats.fps_avg > 90
    assert stats.fps_p1 == pytest.approx(10.0)
    assert stats.stutter_ratio == pytest.approx(0.01)


def test_sample_perf_per_watt_prefers_frames():
    frames = FrameStats.from_frametimes([10.0] * 10, "g.exe", 1, "test")
    sample = Sample(t=0.0, board_w=200.0, gpu_util_pct=99.0, frames=frames)
    assert sample.perf_per_watt == pytest.approx(100.0 / 200.0)

    assert Sample(t=0.0, board_w=0).perf_per_watt is None


def test_blind_efficiency_uses_clock_not_utilisation_alone():
    """Utilisation saturates under load, so it cannot carry the signal.

    Numbers taken from a real Dead by Daylight session: utilisation held
    87-92% throughout while the clock did the actual varying.
    """
    healthy = Sample(t=0.0, clock_mhz=3215, gpu_util_pct=89.0, board_w=237.0)
    # Same load, same utilisation, but the card is throttling badly — this is
    # what an over-aggressive undervolt looks like without a frame source.
    throttled = Sample(t=0.0, clock_mhz=2100, gpu_util_pct=89.0, board_w=180.0)

    assert throttled.perf_per_watt < healthy.perf_per_watt, (
        "a throttling undervolt must not score as an efficiency win")

    # The old utilisation-only proxy got this exactly backwards.
    util_only_healthy = 89.0 / 237.0
    util_only_throttled = 89.0 / 180.0
    assert util_only_throttled > util_only_healthy


def test_blind_efficiency_still_rewards_a_genuine_win():
    """Same clock and load for less power is a real efficiency gain."""
    before = Sample(t=0.0, clock_mhz=3215, gpu_util_pct=89.0, board_w=237.0)
    after = Sample(t=0.0, clock_mhz=3215, gpu_util_pct=89.0, board_w=205.0)
    assert after.perf_per_watt > before.perf_per_watt


def test_blind_efficiency_falls_back_without_a_clock():
    assert Sample(t=0.0, gpu_util_pct=50.0, board_w=200.0).perf_per_watt == \
        pytest.approx(0.25)


def test_sample_from_metrics_maps_bridge_keys():
    sample = Sample.from_metrics(
        {"clockMhz": 2900, "tempC": 60.0, "hotspotC": 80.0, "boardPowerW": 250.0,
         "fanRpm": 1200, "usagePct": 97.0, "vramClockMhz": 2518}, t=1.0)
    assert sample.clock_mhz == 2900
    assert sample.board_w == 250.0
    assert sample.gpu_util_pct == 97.0
    assert sample.as_dict()["clockMhz"] == 2900


def test_sample_from_metrics_falls_back_to_power_w():
    sample = Sample.from_metrics({"powerW": 180.0}, t=1.0)
    assert sample.board_w == 180.0


def _window(fps, watts, hotspot=70.0, n=6):
    frames = FrameStats.from_frametimes([1000.0 / fps] * 20, "g.exe", 1, "test")
    samples = [Sample(t=float(i), board_w=watts, hotspot_c=hotspot,
                      clock_mhz=2900, gpu_util_pct=98.0, frames=frames)
               for i in range(n)]
    return WindowStats.from_samples(samples)


def test_window_stats_aggregates():
    window = _window(100.0, 250.0)
    assert window.sample_count == 6
    assert window.fps_avg == pytest.approx(100.0)
    assert window.board_w == pytest.approx(250.0)
    assert window.perf_per_watt == pytest.approx(0.4)
    assert window.has_frames


def test_empty_window_is_not_usable():
    window = WindowStats.from_samples([])
    assert not window.is_usable()
    assert not window.has_frames


def test_paired_delta_cancels_drift():
    # Baseline drifts downward across the session; the candidate is a
    # consistent +10 above whatever the baseline was at the time. An unpaired
    # comparison of means would be polluted by the drift; the paired one is not.
    baseline = [100.0, 90.0, 80.0, 70.0]
    candidate = [110.0, 100.0, 90.0, 80.0]
    delta = paired_delta(candidate, baseline)
    assert delta.mean == pytest.approx(10.0)
    assert delta.stderr == pytest.approx(0.0)
    assert delta.pairs == 4


def test_paired_delta_reports_noise_as_insignificant():
    candidate = [100.0, 80.0, 120.0, 70.0]
    baseline = [90.0, 95.0, 85.0, 100.0]
    delta = paired_delta(candidate, baseline)
    assert not delta.significant


def test_paired_delta_flags_a_real_difference():
    candidate = [110.0, 111.0, 109.0, 110.5]
    baseline = [100.0, 101.0, 99.0, 100.5]
    delta = paired_delta(candidate, baseline)
    assert delta.significant
    assert delta.confidence > 0.9


def test_relative_paired_delta_is_fractional():
    delta = relative_paired_delta([110.0, 220.0], [100.0, 200.0])
    assert delta.mean == pytest.approx(0.1)


def test_paired_delta_handles_missing_values():
    delta = paired_delta([None, 10.0], [None, 5.0])
    assert delta.pairs == 1
    assert delta.mean == pytest.approx(5.0)
    assert paired_delta([], []).pairs == 0


def test_empty_hardware_samples_do_not_count_as_usable_telemetry():
    window = WindowStats.from_samples([Sample(t=float(i)) for i in range(8)])
    assert not window.is_usable()


def test_nonfinite_sensor_values_are_treated_as_missing():
    sample = Sample.from_metrics({"clockMhz": float("nan"),
                                  "boardPowerW": float("inf"), "powerW": 250,
                                  "hotspotC": "bad", "fanRpm": True}, t=1.0)
    assert sample.clock_mhz is None
    assert sample.board_w == 250
    assert sample.hotspot_c is None
    assert sample.fan_rpm is None


def test_nonfinite_frames_and_pairs_are_ignored():
    stats = FrameStats.from_frametimes([10, 10, float("inf"), float("nan")],
                                      "test.exe", 1, "test")
    assert stats.frame_count == 2
    assert stats.fps_avg == 100
    assert relative_paired_delta([10, float("inf")], [5, 10]).pairs == 1
    assert paired_delta([float("nan"), 10], [3, 5]).mean == 5
