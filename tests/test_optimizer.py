import pytest

from voltshift.optimizer import (GOALS, RecordingApplier, Safeguard, SearchSpace,
                                 make_optimizer, score_trial)
from voltshift.optimizer.space import (MAX_CLOCK, POWER_LIMIT, VOLTAGE,
                                       SearchSpace as Space)
from voltshift.telemetry.sample import FrameStats, Sample
from voltshift.telemetry.window import WindowStats

TUNING = {
    "gfx": {
        "interface": "MGT2_1",
        "voltageMv": 0, "minFreqMhz": 500, "maxFreqMhz": 3100,
        "voltageRange": {"min": -200, "max": 0, "step": 1},
        "minFreqRange": {"min": 500, "max": 3200, "step": 1},
        "maxFreqRange": {"min": 500, "max": 3400, "step": 1},
    },
    "vram": {"maxFreqMhz": 2518, "maxFreqRange": {"min": 2000, "max": 2800, "step": 1}},
    "power": {"powerLimit": 0, "powerLimitRange": {"min": -30, "max": 15, "step": 1}},
}


@pytest.fixture
def space():
    return SearchSpace.from_tuning(TUNING)


# ── search space ──────────────────────────────────────────────────────────────

def test_space_reads_bounds_from_the_driver(space):
    assert VOLTAGE in space.names
    assert space.knob(VOLTAGE).low == -200
    assert space.knob(VOLTAGE).high == 0
    assert space.voltage_is_offset  # MGT2_1


def test_space_marks_absolute_voltage_interfaces():
    tuning = {"gfx": dict(TUNING["gfx"], interface="MGT2")}
    assert not SearchSpace.from_tuning(tuning).voltage_is_offset


def test_space_skips_knobs_the_card_lacks():
    minimal = SearchSpace.from_tuning({"gfx": {"unsupported": "no manual tuning"}})
    assert minimal.names == []
    assert not minimal


def test_space_skips_degenerate_ranges():
    tuning = {"power": {"powerLimit": 0,
                        "powerLimitRange": {"min": 0, "max": 0, "step": 1}}}
    assert POWER_LIMIT not in SearchSpace.from_tuning(tuning).names


@pytest.mark.parametrize("rng", [
    {"min": 10, "max": 0}, {"min": 0, "max": 10, "step": 0},
    {"min": 0, "max": 10, "step": -2}, {"min": 0, "max": float("inf")},
    {"min": None, "max": 10}, {"min": 0, "max": 10, "step": 20},
])
def test_space_rejects_invalid_driver_ranges(rng):
    assert not SearchSpace.from_tuning({"power": {"powerLimitRange": rng}})


def test_knob_snaps_relative_to_driver_minimum():
    from voltshift.optimizer.space import Knob

    knob = Knob("test", low=503, high=599, step=10)
    assert knob.clamp(510) == 513
    assert knob.clamp(999) == 593
    assert knob.clamp(0) == 503
    assert all((knob.clamp(v) - knob.low) % knob.step == 0 for v in range(490, 610))


def test_lost_frame_source_cannot_create_a_false_efficiency_gain():
    baseline = _window(fps=100.0)
    candidate = WindowStats.from_samples([
        Sample(t=float(i), board_w=250, hotspot_c=70, clock_mhz=2900,
               gpu_util_pct=98) for i in range(6)])
    score = score_trial([candidate], [baseline], "efficiency")
    assert score.value == 0
    assert score.note == "no comparable measurement"


def test_vector_round_trip_is_stable(space):
    config = {VOLTAGE: -120, MAX_CLOCK: 3000, "min_clock_mhz": 800,
              "vram_max_mhz": 2600, POWER_LIMIT: 5}
    assert space.from_vector(space.to_vector(config)) == config


def test_clamp_respects_bounds(space):
    clamped = space.clamp_config({VOLTAGE: -900, POWER_LIMIT: 999})
    assert clamped[VOLTAGE] == -200
    assert clamped[POWER_LIMIT] == 15


def test_limit_step_caps_movement(space):
    current = {VOLTAGE: 0, MAX_CLOCK: 3100}
    stepped = space.limit_step({VOLTAGE: -200, MAX_CLOCK: 3400}, current)
    assert stepped[VOLTAGE] == -40      # DEFAULT_MAX_DELTA for voltage
    assert stepped[MAX_CLOCK] == 3300   # +200 MHz cap


def test_distance_is_zero_for_identical_configs(space):
    config = space.default_config()
    assert space.distance(config, config) == pytest.approx(0.0)


# ── safeguard ─────────────────────────────────────────────────────────────────

def test_safeguard_rejects_out_of_bounds(space):
    guard = Safeguard(space)
    assert not guard.check({VOLTAGE: -500})
    assert "outside" in guard.check({VOLTAGE: -500}).reason


def test_safeguard_rejects_oversized_steps(space):
    guard = Safeguard(space)
    verdict = guard.check({VOLTAGE: -150}, current={VOLTAGE: 0})
    assert not verdict
    assert "one step" in verdict.reason


def test_safeguard_blocks_configs_near_a_known_failure(space):
    guard = Safeguard(space)
    guard.mark_unsafe({VOLTAGE: -150, MAX_CLOCK: 3100, "min_clock_mhz": 500,
                       "vram_max_mhz": 2518, POWER_LIMIT: 0}, "tdr")
    near = {VOLTAGE: -148, MAX_CLOCK: 3100, "min_clock_mhz": 500,
            "vram_max_mhz": 2518, POWER_LIMIT: 0}
    assert not guard.check(near)
    assert "destabilised" in guard.check(near).reason


def test_safeguard_allows_configs_far_from_a_failure(space):
    guard = Safeguard(space)
    guard.mark_unsafe({VOLTAGE: -190, MAX_CLOCK: 3100, "min_clock_mhz": 500,
                       "vram_max_mhz": 2518, POWER_LIMIT: 0}, "tdr")
    far = {VOLTAGE: -20, MAX_CLOCK: 3100, "min_clock_mhz": 500,
           "vram_max_mhz": 2518, POWER_LIMIT: 0}
    assert guard.check(far)


def test_safeguard_sanitise_pulls_into_range(space):
    guard = Safeguard(space)
    safe = guard.sanitise({VOLTAGE: -999}, current={VOLTAGE: 0})
    assert safe[VOLTAGE] == -40


def _frontier_knowledge(limit):
    class FrontierOnly:
        def unsafe_configs(self, gpu):
            return []

        def frontier_limit(self, gpu, clock):
            return limit

    return FrontierOnly()


def test_safeguard_honours_the_learned_frontier(space):
    guard = Safeguard(space, knowledge=_frontier_knowledge(-140), gpu_key="card")
    assert not guard.check_frontier({VOLTAGE: -150})
    assert not guard.check_frontier({VOLTAGE: -130})   # inside the margin
    assert guard.check_frontier({VOLTAGE: -100})


def test_a_frontier_that_forbids_everything_is_ignored(space):
    """A frontier at stock would reject every value the knob can take.

    That is not information, it is a corrupt entry, and obeying it leaves the
    card permanently untunable with no way to recover.
    """
    guard = Safeguard(space, knowledge=_frontier_knowledge(0), gpu_key="card")
    assert guard.check_frontier({VOLTAGE: 0})
    assert guard.check_frontier({VOLTAGE: -100})
    assert guard.check_frontier({VOLTAGE: -200})


def test_a_frontier_just_below_stock_still_constrains(space):
    # -20 + 15 margin = -5, which is still inside the -200..0 range, so it is
    # real information and must be obeyed.
    guard = Safeguard(space, knowledge=_frontier_knowledge(-20), gpu_key="card")
    assert not guard.check_frontier({VOLTAGE: -100})
    assert guard.check_frontier({VOLTAGE: 0})


# ── objective ─────────────────────────────────────────────────────────────────

def _window(fps=100.0, watts=250.0, hotspot=70.0, fan=1200, n=6):
    frames = FrameStats.from_frametimes([1000.0 / fps] * 30, "g.exe", 1, "test")
    return WindowStats.from_samples([
        Sample(t=float(i), board_w=watts, hotspot_c=hotspot, fan_rpm=fan,
               clock_mhz=2900, gpu_util_pct=98.0, frames=frames)
        for i in range(n)])


def test_faster_is_better_under_max_fps():
    candidate = [_window(fps=110.0), _window(fps=111.0)]
    baseline = [_window(fps=100.0), _window(fps=100.0)]
    assert score_trial(candidate, baseline, "max_fps").value > 0


def test_slower_is_worse_under_every_goal():
    candidate = [_window(fps=80.0), _window(fps=81.0)]
    baseline = [_window(fps=100.0), _window(fps=100.0)]
    for goal in GOALS:
        assert score_trial(candidate, baseline, goal).value < 0, goal


def test_efficiency_goal_rewards_the_same_speed_for_less_power():
    candidate = [_window(fps=100.0, watts=200.0), _window(fps=100.0, watts=201.0)]
    baseline = [_window(fps=100.0, watts=250.0), _window(fps=100.0, watts=250.0)]
    assert score_trial(candidate, baseline, "efficiency").value > 0


def test_max_fps_barely_cares_about_power():
    candidate = [_window(fps=100.0, watts=200.0), _window(fps=100.0, watts=200.0)]
    baseline = [_window(fps=100.0, watts=250.0), _window(fps=100.0, watts=250.0)]
    efficiency = score_trial(candidate, baseline, "efficiency").value
    max_fps = score_trial(candidate, baseline, "max_fps").value
    assert efficiency > max_fps


def test_instability_dominates_any_gain():
    candidate = [_window(fps=200.0), _window(fps=200.0)]
    baseline = [_window(fps=100.0), _window(fps=100.0)]
    score = score_trial(candidate, baseline, "max_fps", unstable_reason="TDR")
    assert score.unstable
    assert score.value < -1
    assert "TDR" in score.explain()


def test_dangerous_hotspot_is_rejected_outright():
    candidate = [_window(fps=140.0, hotspot=110.0), _window(fps=140.0, hotspot=110.0)]
    baseline = [_window(fps=100.0, hotspot=70.0), _window(fps=100.0, hotspot=70.0)]
    assert score_trial(candidate, baseline, "max_fps").unstable


def test_scoring_without_frames_is_marked_blind():
    def hardware_only(watts):
        return WindowStats.from_samples([
            Sample(t=float(i), board_w=watts, hotspot_c=70.0, clock_mhz=2900,
                   gpu_util_pct=98.0) for i in range(6)])

    score = score_trial([hardware_only(200.0)], [hardware_only(250.0)], "efficiency")
    assert score.blind
    assert score.value > 0


def test_no_measurement_scores_zero():
    assert score_trial([], [], "balanced").value == 0.0


# ── optimiser ─────────────────────────────────────────────────────────────────

def test_optimizer_finds_a_known_optimum(space):
    optimizer = make_optimizer(space, seed=7)
    guard = Safeguard(space)

    def truth(config):
        return -abs(config[VOLTAGE] + 120) / 200.0

    best = None
    for _ in range(30):
        config = optimizer.suggest(reject=guard.rejects())
        value = truth(config)
        optimizer.observe(config, value)
        if best is None or value > best:
            best, best_config = value, config

    assert abs(best_config[VOLTAGE] + 120) < 20


def test_optimizer_never_proposes_a_rejected_config(space):
    optimizer = make_optimizer(space, seed=3)
    guard = Safeguard(space)
    guard.mark_unsafe({VOLTAGE: -150, MAX_CLOCK: 3100, "min_clock_mhz": 500,
                       "vram_max_mhz": 2518, POWER_LIMIT: 0}, "tdr")
    reject = guard.rejects()
    for _ in range(15):
        config = optimizer.suggest(reject=reject)
        optimizer.observe(config, 0.5)
        assert guard.check(config)


def test_optimizer_seeded_prior_counts_less_than_a_real_trial(space):
    from voltshift.optimizer.gp import Observation

    optimizer = make_optimizer(space, seed=1)
    optimizer.seed_prior([Observation({VOLTAGE: -100}, 1.0, 0.3)])
    assert optimizer.trial_count == 0
    optimizer.observe({VOLTAGE: -110}, 0.2)
    assert optimizer.trial_count == 1


def test_optimizer_tracks_the_best_observation(space):
    optimizer = make_optimizer(space, seed=2)
    optimizer.observe({VOLTAGE: -100}, 0.1)
    optimizer.observe({VOLTAGE: -110}, 0.9)
    optimizer.observe({VOLTAGE: -120}, 0.4)
    assert optimizer.best.config[VOLTAGE] == -110


# ── applier ───────────────────────────────────────────────────────────────────

def test_recording_applier_tracks_state():
    applier = RecordingApplier({VOLTAGE: 0})
    applier.apply({VOLTAGE: -50})
    assert applier.read_current()[VOLTAGE] == -50
    assert applier.last_applied[VOLTAGE] == -50


def test_absolute_voltage_interface_sends_a_delta(space):
    """MGT2 adds the argument to the current voltage, so re-applying the same
    absolute target must not walk the voltage downward."""
    from voltshift.optimizer.applier import TuningApplier

    class FakeBridge:
        def __init__(self):
            self.voltage = 1000
            self.offsets = []

        def tuning_get(self):
            return {"gfx": {"voltageMv": self.voltage, "interface": "MGT2"}}

        def set_voltage_offset(self, mv):
            self.offsets.append(mv)
            self.voltage += mv

    mgt2_space = Space.from_tuning({"gfx": dict(TUNING["gfx"], interface="MGT2")})
    bridge = FakeBridge()
    applier = TuningApplier(bridge, mgt2_space)

    applier.apply({VOLTAGE: 950})
    assert bridge.offsets == [-50]
    assert bridge.voltage == 950

    # Applying the same absolute target again must be a no-op, not another -50.
    applier.apply({VOLTAGE: 950})
    assert bridge.offsets == [-50]
    assert bridge.voltage == 950
