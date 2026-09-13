"""End-to-end auto-tune tests against a simulated GPU.

The simulation is deliberately simple but has the shape of the real problem:
a performance peak at some undervolt, degradation past it, and a hard
stability cliff further down. A tuner that works here is one that finds the
peak without falling off the cliff.
"""

import time

import pytest

from voltshift.optimizer import (RecordingApplier, Safeguard, SearchSpace,
                                 make_optimizer)
from voltshift.optimizer.session import (AutoTuneSession, SessionConfig,
                                         SessionState)
from voltshift.optimizer.space import VOLTAGE
from voltshift.telemetry.sample import FrameStats, Sample

TUNING = {
    "gfx": {
        "interface": "MGT2_1", "voltageMv": 0, "minFreqMhz": 500, "maxFreqMhz": 3100,
        "voltageRange": {"min": -200, "max": 0, "step": 5},
        "maxFreqRange": {"min": 2000, "max": 3400, "step": 10},
    },
}

# Below this the simulated card misbehaves.
CLIFF_MV = -150
# Performance peaks here: efficiency gains, before the cliff.
PEAK_MV = -100


class SimulatedGpu:
    """Turns a configuration into plausible telemetry."""

    def __init__(self, applier, cliff_mv=CLIFF_MV, peak_mv=PEAK_MV):
        self.applier = applier
        self.cliff_mv = cliff_mv
        self.peak_mv = peak_mv
        self.unstable_writes = 0

    def _fps(self) -> float:
        voltage = self.applier.current.get(VOLTAGE, 0)
        if voltage <= self.cliff_mv:
            return 20.0  # the card is falling over
        # A gentle peak at peak_mv, worth about 8% over stock.
        distance = abs(voltage - self.peak_mv) / 100.0
        return 100.0 * (1.08 - 0.10 * distance)

    def _watts(self) -> float:
        voltage = self.applier.current.get(VOLTAGE, 0)
        return 300.0 + voltage * 0.5   # less voltage, less power

    def sample(self, t: float) -> Sample:
        voltage = self.applier.current.get(VOLTAGE, 0)
        unstable = voltage <= self.cliff_mv
        frametimes = [1000.0 / self._fps()] * 30
        if unstable:
            frametimes += [400.0] * 4   # the spike train a real card produces
        return Sample(
            t=t,
            clock_mhz=1400 if unstable else 2900,
            gpu_util_pct=98.0,
            board_w=self._watts(),
            hotspot_c=75.0,
            fan_rpm=1500,
            frames=FrameStats.from_frametimes(frametimes, "game.exe", 100, "sim"),
        )


class FakeHub:
    """Enough of TelemetryHub for the session to run against."""

    def __init__(self, gpu: SimulatedGpu, samples_per_window: int = 8):
        self.gpu = gpu
        self.samples_per_window = samples_per_window
        self.subscribers = []
        self._offset = None

    def subscribe(self, callback):
        self.subscribers.append(callback)
        return lambda: self.subscribers.remove(callback)

    def set_applied_offset(self, mv):
        self._offset = mv

    @property
    def latest(self):
        return self.gpu.sample(time.monotonic())

    def history(self, seconds=None):
        now = time.monotonic()
        samples = [self.gpu.sample(now - 0.001 * (self.samples_per_window - i))
                   for i in range(self.samples_per_window)]
        for sample in samples:
            for callback in list(self.subscribers):
                callback(sample)
        return samples


def _fast_config(**overrides):
    defaults = dict(trials=10, pairs_per_trial=1, window_sec=0.01, settle_sec=0.0,
                    confirm_pairs=1, min_samples_per_window=2)
    defaults.update(overrides)
    return SessionConfig(**defaults)


def _build(goal="balanced", knowledge=None, **session_overrides):
    space = SearchSpace.from_tuning(TUNING)
    applier = RecordingApplier({VOLTAGE: 0, "max_clock_mhz": 3100})
    gpu = SimulatedGpu(applier)
    hub = FakeHub(gpu)
    guard = Safeguard(space, knowledge=knowledge, gpu_key="sim")
    optimizer = make_optimizer(space, seed=11)
    session = AutoTuneSession(
        hub, applier, space, guard, optimizer,
        _fast_config(goal=goal, **session_overrides),
        knowledge=knowledge, gpu_key="sim", exe="game.exe")
    return session, applier, gpu, guard


def _run(session, timeout=30.0):
    session.start()
    deadline = time.monotonic() + timeout
    while session.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not session.running, "session did not finish in time"
    return session.report


def test_session_finishes_and_reports():
    session, _, _, _ = _build()
    report = _run(session)
    assert report is not None
    assert report.state in (SessionState.DONE, SessionState.ABORTED)
    assert session.trials, "the session should have run trials"


def test_session_records_a_baseline():
    session, _, _, _ = _build()
    _run(session)
    assert session.baseline[VOLTAGE] == 0


def test_session_finds_an_undervolt_that_beats_stock():
    session, _, _, _ = _build(goal="efficiency")
    report = _run(session)
    assert report.best_config is not None, report.message
    assert report.best_config[VOLTAGE] < 0
    assert report.improved


def test_session_stays_above_the_stability_cliff():
    session, applier, _, _ = _build(goal="efficiency")
    report = _run(session)
    assert report.best_config is not None, report.message
    assert report.best_config[VOLTAGE] > CLIFF_MV


def test_unstable_trials_are_marked_and_never_committed():
    session, _, _, guard = _build(goal="max_fps", trials=14)
    report = _run(session)
    unstable = [t for t in session.trials if not t.stable]
    if unstable:
        # Anything that destabilised the card must be in the tabu set, and
        # must not be what we ended up applying.
        for trial in unstable:
            assert not guard.check_tabu(trial.config)
            assert report.best_config != trial.config


def test_aborting_restores_the_baseline():
    session, applier, _, _ = _build(trials=200, window_sec=0.05)
    session.start()
    time.sleep(0.3)
    session.stop()
    assert applier.current[VOLTAGE] == 0, "abort must put the card back"


def test_session_without_telemetry_fails_cleanly():
    space = SearchSpace.from_tuning(TUNING)
    applier = RecordingApplier(space.default_config())

    class DeadHub:
        def subscribe(self, callback):
            return lambda: None

        def set_applied_offset(self, mv):
            pass

        latest = None

        def history(self, seconds=None):
            return []

    session = AutoTuneSession(DeadHub(), applier, space,
                              Safeguard(space), make_optimizer(space, seed=1),
                              _fast_config())
    report = _run(session)
    assert report.state == SessionState.FAILED
    assert "telemetry" in report.message


def test_stop_during_final_apply_restores_baseline(monkeypatch):
    session, applier, _, _ = _build(goal="efficiency")
    apply = applier.apply

    def stopping_apply(config, **kwargs):
        result = apply(config, **kwargs)
        if session.state == SessionState.APPLYING:
            session._stop.set()
        return result

    monkeypatch.setattr(applier, "apply", stopping_apply)
    report = _run(session)
    assert report.state == SessionState.ABORTED
    assert report.best_config is None
    assert applier.current == session.baseline


def test_knowledge_is_written_and_reused():
    from voltshift.knowledge import KnowledgeStore

    store = KnowledgeStore(":memory:")
    try:
        session, _, _, _ = _build(goal="efficiency", knowledge=store)
        report = _run(session)
        assert store.stats("sim")["observations"] > 0

        if report.best_config:
            assert store.best_config("sim", "game.exe", "efficiency") is not None
            # A second session on the same game starts warm rather than cold.
            priors = store.prior_observations("sim", "game.exe", "efficiency")
            assert priors
    finally:
        store.close()


def test_frontier_learns_from_a_failure():
    from voltshift.knowledge import KnowledgeStore

    store = KnowledgeStore(":memory:")
    try:
        session, _, _, _ = _build(goal="max_fps", knowledge=store, trials=14)
        _run(session)
        if any(not t.stable for t in session.trials):
            assert store.frontier_limit("sim", 2900) is not None
    finally:
        store.close()
