import threading
import time
from types import SimpleNamespace

import pytest

from voltshift.adaptive import (PHASE_CONFIRM_TICKS, AdaptiveGovernor, Phase,
                                ProbeBudget, classify)
from voltshift.bridgeclient import BridgeError
from voltshift.optimizer import RecordingApplier, Safeguard, SearchSpace
from voltshift.optimizer.space import VOLTAGE
from voltshift.telemetry.sample import FrameStats, Sample

TUNING = {
    "gfx": {
        "interface": "MGT2_1", "voltageMv": 0, "maxFreqMhz": 3100,
        "voltageRange": {"min": -200, "max": 0, "step": 5},
        "maxFreqRange": {"min": 2000, "max": 3400, "step": 10},
    },
}


def _sample(util, fps=None, stutter_frames=0, t=0.0):
    frames = None
    if fps is not None:
        frametimes = [1000.0 / fps] * 30 + [300.0] * stutter_frames
        frames = FrameStats.from_frametimes(frametimes, "game.exe", 100, "test")
    return Sample(t=t, gpu_util_pct=util, clock_mhz=2900, board_w=250.0,
                  hotspot_c=70.0, frames=frames)


# ── phase classification ──────────────────────────────────────────────────────

def test_idle_when_the_gpu_is_asleep():
    assert classify(_sample(3.0), []) == Phase.IDLE
    assert classify(Sample(t=0.0), []) == Phase.IDLE


def test_heavy_under_full_load():
    assert classify(_sample(97.0, fps=90.0), []) == Phase.HEAVY


def test_light_under_partial_load():
    assert classify(_sample(50.0, fps=90.0), []) == Phase.LIGHT


def test_menu_is_high_fps_at_low_load():
    assert classify(_sample(30.0, fps=600.0), []) == Phase.MENU


def test_loading_is_thrashing_load_with_broken_pacing():
    recent = [_sample(u, fps=40.0, stutter_frames=8)
              for u in (10.0, 95.0, 12.0, 90.0, 15.0, 88.0)]
    assert classify(recent[-1], recent) == Phase.LOADING


def test_steady_load_is_not_mistaken_for_loading():
    recent = [_sample(u, fps=90.0) for u in (95.0, 96.0, 94.0, 97.0, 95.0, 96.0)]
    assert classify(recent[-1], recent) == Phase.HEAVY


def test_only_gameplay_phases_are_tunable():
    assert Phase.HEAVY.tunable and Phase.LIGHT.tunable
    assert not Phase.LOADING.tunable
    assert not Phase.MENU.tunable
    assert not Phase.IDLE.tunable


# ── probe budget ──────────────────────────────────────────────────────────────

def test_budget_blocks_until_the_interval_has_passed():
    budget = ProbeBudget(min_interval_sec=100.0)
    budget.last_probe_t = 50.0
    assert not budget.ready(100.0)
    assert budget.ready(160.0)


def test_budget_is_exhausted_by_its_probe_count():
    budget = ProbeBudget(max_probes=2, min_interval_sec=0.0)
    budget.spent = 2
    assert budget.exhausted
    assert not budget.ready(1000.0)


def test_a_single_fault_spends_the_whole_budget():
    budget = ProbeBudget(max_probes=10, min_interval_sec=0.0)
    budget.failures = 1
    assert budget.exhausted, "one real fault must stop further experimentation"


def test_zero_budget_never_probes():
    budget = ProbeBudget(max_probes=0)
    assert budget.exhausted
    assert not budget.ready(1e9)


# ── governor ──────────────────────────────────────────────────────────────────

class FakeHub:
    def __init__(self, sample=None):
        self.latest = sample or _sample(97.0, fps=90.0)
        self.subscribers = []
        self.frame_source = None

    def subscribe(self, callback):
        self.subscribers.append(callback)
        return lambda: self.subscribers.remove(callback)

    def history(self, seconds=None):
        return [self.latest] * 6

    def set_applied_offset(self, mv):
        pass


def _governor(knowledge=None, budget=None, initial=None):
    space = SearchSpace.from_tuning(TUNING)
    applier = RecordingApplier(initial or {VOLTAGE: 0, "max_clock_mhz": 3100})
    hub = FakeHub()
    guard = Safeguard(space, knowledge=knowledge, gpu_key="sim")
    governor = AdaptiveGovernor(hub, applier, space, guard, knowledge=knowledge,
                                gpu_key="sim", goal="balanced",
                                budget=budget or ProbeBudget(max_probes=0),
                                tick_sec=0.02)
    return governor, applier, hub, guard


def test_governor_starts_and_stops_cleanly():
    governor, applier, _, _ = _governor()
    governor.start()
    assert governor.running
    time.sleep(0.1)
    governor.stop()
    assert not governor.running
    assert applier.current[VOLTAGE] == 0


def test_stopping_restores_the_desktop_configuration():
    governor, applier, _, _ = _governor()
    governor.start()
    time.sleep(0.05)
    applier.apply({VOLTAGE: -80})
    governor.stop(restore=True)
    assert applier.current[VOLTAGE] == 0


def test_phase_changes_need_confirmation():
    governor, _, hub, _ = _governor()
    governor._phase = Phase.IDLE
    heavy = _sample(97.0, fps=90.0)
    for _ in range(PHASE_CONFIRM_TICKS - 1):
        governor._track_phase(heavy)
        assert governor._phase == Phase.IDLE, "one reading must not flip the phase"
    governor._track_phase(heavy)
    assert governor._phase == Phase.HEAVY


def test_a_flapping_signal_does_not_change_phase():
    governor, _, _, _ = _governor()
    governor._phase = Phase.IDLE
    for i in range(10):
        governor._track_phase(_sample(97.0 if i % 2 else 3.0, fps=90.0))
    assert governor._phase == Phase.IDLE


def test_ramp_moves_one_capped_step_at_a_time():
    governor, applier, _, _ = _governor()
    governor._desktop_config = {VOLTAGE: 0}
    governor._target = {VOLTAGE: -200, "max_clock_mhz": 3100}

    governor._ramp_toward_target()
    assert applier.current[VOLTAGE] == -40   # the per-step cap, not -200

    governor._ramp_toward_target()
    assert applier.current[VOLTAGE] == -80


def test_ramp_stops_once_the_target_is_reached():
    governor, applier, _, _ = _governor()
    governor._target = {VOLTAGE: -20, "max_clock_mhz": 3100}
    governor._ramp_toward_target()
    assert applier.current[VOLTAGE] == -20
    assert governor._target is None


def test_ramp_refuses_a_target_the_safeguard_rejects():
    knowledge_stub = type("K", (), {
        "unsafe_configs": lambda self, gpu: [],
        "frontier_limit": lambda self, gpu, clock: -50,
    })()
    governor, applier, _, _ = _governor(knowledge=knowledge_stub)
    governor._target = {VOLTAGE: -40, "max_clock_mhz": 3100}
    governor._ramp_toward_target()
    assert applier.current[VOLTAGE] == 0, "below the learned frontier must not apply"
    assert governor._target is None


def test_a_critical_event_reverts_and_records():
    from voltshift.stability import SEVERITY_CRITICAL, StabilityEvent

    marked = []

    class Knowledge:
        def unsafe_configs(self, gpu):
            return []

        def frontier_limit(self, gpu, clock):
            return None

        def mark_unsafe(self, gpu, config, kind):
            marked.append((config, kind))

        def record_failure(self, gpu, mv, clock):
            marked.append(("failure", mv))

    governor, applier, _, _ = _governor(knowledge=Knowledge())
    governor._desktop_config = {VOLTAGE: 0}
    applier.apply({VOLTAGE: -120})

    governor._on_stability_event(
        StabilityEvent("tdr", SEVERITY_CRITICAL, "driver reset"))

    assert applier.current[VOLTAGE] == 0, "a fault must revert immediately"
    assert governor._budget.failures == 1
    assert any(entry[0] == "failure" for entry in marked)


def test_non_critical_events_are_ignored():
    from voltshift.stability import SEVERITY_INFO, StabilityEvent

    governor, applier, _, _ = _governor()
    governor._desktop_config = {VOLTAGE: 0}
    applier.apply({VOLTAGE: -60})
    governor._on_stability_event(StabilityEvent("noise", SEVERITY_INFO, "hmm"))
    assert applier.current[VOLTAGE] == -60


def test_probe_proposal_is_a_single_small_step():
    governor, applier, _, _ = _governor(budget=ProbeBudget(max_probes=4))
    current = {VOLTAGE: 0, "max_clock_mhz": 3100}
    proposal = governor._propose_probe(current)
    assert proposal is not None
    changed = [k for k in current if proposal[k] != current[k]]
    assert len(changed) == 1, "a probe changes exactly one knob"
    knob = governor._space.knob(changed[0])
    assert abs(proposal[changed[0]] - current[changed[0]]) <= knob.max_delta


def test_probe_proposal_respects_the_tabu_set():
    governor, _, _, guard = _governor(budget=ProbeBudget(max_probes=4))
    current = {VOLTAGE: 0, "max_clock_mhz": 3100}
    for _ in range(40):
        proposal = governor._propose_probe(current)
        if proposal is None:
            break
        assert guard.check(proposal, current)
        guard.mark_unsafe(proposal, "test")


def test_status_reports_the_governors_view():
    governor, _, _, _ = _governor(budget=ProbeBudget(max_probes=5))
    status = governor.status()
    assert status.phase == Phase.IDLE
    assert status.probes_left == 5
    assert status.game is None


class RecordingWatchdog:
    def __init__(self):
        self.pending = None
        self.journals = []

    def journal(self, config, reason):
        self.pending = dict(config)
        self.journals.append((dict(config), reason))

    def abandon(self):
        self.pending = None


def _probe_governor():
    governor, applier, hub, guard = _governor(
        initial={VOLTAGE: -40, "max_clock_mhz": 3100},
        budget=ProbeBudget(max_probes=4, min_interval_sec=0,
                           settle_sec=0, window_sec=0))
    governor._desktop_config = {VOLTAGE: 0, "max_clock_mhz": 3100}
    governor._phase = Phase.HEAVY
    governor._watchdog = RecordingWatchdog()
    return governor, applier, hub, guard


def test_rejected_probe_journal_describes_the_restored_baseline(monkeypatch):
    governor, applier, _, _ = _probe_governor()
    baseline = applier.read_current()
    monkeypatch.setattr("voltshift.adaptive.score_trial", lambda *args: SimpleNamespace(
        unstable=False, value=-1, explain=lambda: "no improvement"))

    governor._run_probe()

    assert applier.current == baseline
    assert governor._watchdog.pending == baseline
    assert governor._watchdog.journals[-1][1] == "governor probe baseline"


def test_each_probe_transition_is_journaled_before_the_hardware_write(monkeypatch):
    governor, applier, _, _ = _probe_governor()
    apply = applier.apply
    reasons = []

    def checked_apply(config, **kwargs):
        assert governor._watchdog.pending == config
        reasons.append(governor._watchdog.journals[-1][1])
        return apply(config, **kwargs)

    monkeypatch.setattr(applier, "apply", checked_apply)
    monkeypatch.setattr("voltshift.adaptive.score_trial", lambda *args: SimpleNamespace(
        unstable=False, value=1, explain=lambda: "better efficiency"))

    governor._run_probe()

    assert reasons == ["governor probe", "governor probe baseline",
                       "governor probe accepted"]
    assert applier.current[VOLTAGE] < -40
    assert governor._watchdog.pending == applier.current


def test_probe_restores_after_a_partially_successful_write(monkeypatch):
    governor, applier, _, _ = _probe_governor()
    baseline = applier.read_current()
    apply = applier.apply

    def partial_failure(config, **kwargs):
        apply(config, **kwargs)
        if config != baseline:
            raise BridgeError("voltage write failed after clock write")

    monkeypatch.setattr(applier, "apply", partial_failure)
    governor._run_probe()

    assert applier.current == baseline
    assert governor._watchdog.pending is None
    assert governor._budget.failures == 1
    assert governor._budget.exhausted


def test_probe_measurement_error_restores_previous_configuration(monkeypatch):
    governor, applier, _, _ = _probe_governor()
    baseline = applier.read_current()

    def failed_measurement():
        raise RuntimeError("telemetry stopped")

    monkeypatch.setattr(governor, "_measure", failed_measurement)
    governor._run_probe()

    assert applier.current == baseline
    assert governor._watchdog.pending is None


def test_broken_log_observer_cannot_prevent_probe_recovery(monkeypatch):
    governor, applier, _, _ = _probe_governor()
    baseline = applier.read_current()

    def failed_measurement():
        raise RuntimeError("telemetry stopped")

    def failed_logger(*args):
        raise RuntimeError("GUI closed")

    monkeypatch.setattr(governor, "_measure", failed_measurement)
    governor.on_log = failed_logger
    governor._run_probe()
    assert applier.current == baseline
    assert governor._watchdog.pending is None
    assert governor._budget.failures == 1


def test_failed_restore_uses_reset_and_retains_journal_if_reset_fails(monkeypatch):
    governor, applier, _, _ = _probe_governor()
    resets = []

    def failed_apply(*args, **kwargs):
        raise BridgeError("driver unavailable")

    def failed_reset():
        resets.append(True)
        raise BridgeError("reset unavailable")

    monkeypatch.setattr(applier, "apply", failed_apply)
    monkeypatch.setattr(applier, "reset", failed_reset)
    governor._run_probe()

    assert resets == [True]
    assert governor._watchdog.pending is not None
    assert "probe recovery failed" in governor.status().last_note


def _start_long_probe(governor, applier, monkeypatch):
    governor._budget.settle_sec = 60
    governor._budget.window_sec = 60
    governor._phase_since = time.monotonic() - 300
    applied = threading.Event()
    apply = applier.apply

    def signal_apply(config, **kwargs):
        result = apply(config, **kwargs)
        applied.set()
        return result

    monkeypatch.setattr(applier, "apply", signal_apply)
    governor._maybe_probe(governor._hub.latest)
    assert applied.wait(2), "the simulated probe did not start"


def test_critical_event_interrupts_active_probe_without_waiting_for_measurement(monkeypatch):
    from voltshift.stability import SEVERITY_CRITICAL, StabilityEvent

    governor, applier, _, _ = _probe_governor()
    _start_long_probe(governor, applier, monkeypatch)
    probe_thread = governor._probe_thread
    callback = threading.Thread(target=governor._on_stability_event, args=(
        StabilityEvent("tdr", SEVERITY_CRITICAL, "driver reset"),), daemon=True)
    try:
        callback.start()
        callback.join(timeout=2)
        assert not callback.is_alive(), "emergency recovery waited for the probe"
        probe_thread.join(timeout=2)
        assert not probe_thread.is_alive()
        assert applier.current == governor._desktop_config
        assert governor._budget.failures == 1
        assert governor._watchdog.pending is None
    finally:
        governor.stop()
        callback.join(timeout=2)


def test_stop_joins_the_probe_before_restoring_desktop(monkeypatch):
    governor, applier, _, _ = _probe_governor()
    _start_long_probe(governor, applier, monkeypatch)
    probe_thread = governor._probe_thread

    governor.stop()

    assert not probe_thread.is_alive()
    assert governor._probe_thread is None
    assert applier.current == governor._desktop_config
    assert applier.history[-1] == governor._desktop_config
    assert governor._watchdog.pending is None


def test_stop_without_desktop_restore_still_backs_out_unfinished_probe(monkeypatch):
    governor, applier, _, _ = _probe_governor()
    baseline = applier.read_current()
    _start_long_probe(governor, applier, monkeypatch)

    governor.stop(restore=False)

    assert applier.current == baseline
    assert governor._probe_thread is None


def test_fault_during_baseline_measurement_never_reapplies_candidate(monkeypatch):
    from voltshift.stability import SEVERITY_CRITICAL, StabilityEvent

    governor, applier, _, guard = _probe_governor()
    measure = governor._measure
    measurements = []
    candidate = governor._propose_probe(applier.read_current())

    def fault_in_baseline_window():
        measurements.append(True)
        window = measure()
        if len(measurements) == 2:
            governor._on_stability_event(StabilityEvent(
                "tdr", SEVERITY_CRITICAL, "delayed reset", config=candidate))
        return window

    monkeypatch.setattr(governor, "_measure", fault_in_baseline_window)
    governor._run_probe()

    assert len(measurements) == 2
    assert applier.current == governor._desktop_config
    assert applier.history[-1] == governor._desktop_config
    assert not guard.check(candidate, candidate).ok


def test_critical_event_still_restores_when_recording_failure_fails(monkeypatch):
    from voltshift.stability import SEVERITY_CRITICAL, StabilityEvent

    governor, applier, _, guard = _probe_governor()

    def failed_record(*args):
        raise OSError("database unavailable")

    monkeypatch.setattr(guard, "mark_unsafe", failed_record)
    with pytest.raises(OSError, match="database unavailable"):
        governor._on_stability_event(StabilityEvent(
            "tdr", SEVERITY_CRITICAL, "driver reset"))

    assert applier.current == governor._desktop_config


def test_ramp_write_failure_restores_desktop_and_clears_target(monkeypatch):
    governor, applier, _, _ = _probe_governor()
    governor._target = {VOLTAGE: -80, "max_clock_mhz": 3100}
    apply = applier.apply

    def failed_ramp(config, **kwargs):
        if config != governor._desktop_config:
            raise BridgeError("driver rejected ramp")
        return apply(config, **kwargs)

    monkeypatch.setattr(applier, "apply", failed_ramp)
    with pytest.raises(BridgeError, match="driver rejected ramp"):
        governor._ramp_toward_target()

    assert applier.current == governor._desktop_config
    assert governor._target is None
    assert governor._budget.exhausted
