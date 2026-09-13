"""Failure-path regressions: all hardware and benchmark inputs are simulated."""

from types import SimpleNamespace

import pytest

from voltshift import autostack, paths
from voltshift.bridgeclient import BridgeError
from voltshift.optimizer import RecordingApplier, Safeguard, SearchSpace, make_optimizer
from voltshift.optimizer.applier import TuningApplier
from voltshift.optimizer.benchsession import BenchmarkConfig, BenchmarkSession, BenchmarkTrial
from voltshift.optimizer.session import SessionState
from voltshift.optimizer.space import MAX_CLOCK, POWER_LIMIT, VOLTAGE
from voltshift.benchmark import BenchmarkResult
from voltshift.watchdog import GuardedApply, Watchdog
from voltshift.telemetry.hub import TelemetryHub
from voltshift.telemetry.sample import Sample


class Bridge:
    def __init__(self, interface="MGT2_1"):
        self.interface = interface
        self.voltage = 0 if interface == "MGT2_1" else 900
        self.clock = 2800
        self.power = 5
        self.calls = []
        self.fail_power = False
        self.fail_reset = False

    def caps(self):
        return {"tuning": {"manualFan": False}}

    def tuning_get(self):
        return {
            "gfx": {"interface": self.interface, "voltageMv": self.voltage,
                    "maxFreqMhz": self.clock,
                    "voltageRange": {"min": -200, "max": 0},
                    "maxFreqRange": {"min": 2000, "max": 3400}},
            "power": {"powerLimit": self.power,
                      "powerLimitRange": {"min": -20, "max": 15}},
        }

    def set_core_clocks(self, minimum, maximum):
        self.calls.append("clocks")
        if maximum is not None:
            self.clock = maximum

    def set_power_limit(self, value):
        self.calls.append("power")
        if self.fail_power:
            raise BridgeError("power rejected")
        self.power = value

    def set_voltage_offset(self, value):
        self.calls.append("voltage")
        self.voltage = value if self.interface == "MGT2_1" else self.voltage + value

    def tuning_reset(self):
        self.calls.append("reset")
        if self.fail_reset:
            raise BridgeError("reset rejected")
        self.voltage, self.clock, self.power = 1000, 3000, 0


def test_failed_write_invalidates_cache_and_is_retried():
    bridge = Bridge()
    applier = TuningApplier(bridge, SearchSpace.from_tuning(bridge.tuning_get()))
    applier.apply({POWER_LIMIT: 5})
    bridge.fail_power = True
    with pytest.raises(BridgeError, match="power rejected"):
        applier.apply({MAX_CLOCK: 2900, POWER_LIMIT: 10, VOLTAGE: -80})
    assert applier.last_applied is None
    assert bridge.voltage == 0, "stop writing after the first driver rejection"
    bridge.fail_power = False
    applier.apply({MAX_CLOCK: 2900, POWER_LIMIT: 10, VOLTAGE: -80})
    assert bridge.power == 10
    assert bridge.voltage == -80


def test_failed_factory_reset_is_reported():
    bridge = Bridge()
    applier = TuningApplier(bridge, SearchSpace.from_tuning(bridge.tuning_get()))
    applier.apply({POWER_LIMIT: 5})
    bridge.fail_reset = True
    with pytest.raises(BridgeError, match="reset rejected"):
        applier.reset()
    assert applier.last_applied is None


def test_absolute_voltage_restore_preserves_controls_across_global_reset():
    bridge = Bridge("MGT2")
    applier = TuningApplier(bridge, SearchSpace.from_tuning(bridge.tuning_get()))
    applier.apply({VOLTAGE: 950})
    assert bridge.calls[0] == "reset"
    assert (bridge.voltage, bridge.clock, bridge.power) == (950, 2800, 5)
    assert applier.last_applied == {VOLTAGE: 950, MAX_CLOCK: 2800, POWER_LIMIT: 5}


def test_absolute_voltage_restore_preserves_excluded_clock_and_power_controls():
    bridge = Bridge("MGT2")
    space = SearchSpace.from_tuning(bridge.tuning_get(), enabled=[VOLTAGE])
    applier = TuningApplier(bridge, space)
    applier.apply({VOLTAGE: 950})
    assert (bridge.voltage, bridge.clock, bridge.power) == (950, 2800, 5)


def test_absolute_restore_refuses_reset_without_fan_snapshot():
    bridge = Bridge("MGT2")
    bridge.caps = lambda: {"tuning": {"manualFan": True}}

    def unavailable():
        raise BridgeError("fan readback unavailable")

    bridge.fans_get = unavailable
    applier = TuningApplier(bridge, SearchSpace.from_tuning(bridge.tuning_get()))
    with pytest.raises(BridgeError, match="fan readback unavailable"):
        applier.apply({VOLTAGE: 950})
    assert bridge.calls == []


def test_absolute_restore_preserves_fans_timing_and_tdc():
    class FullBridge(Bridge):
        def __init__(self):
            super().__init__("MGT2")
            self.timing, self.tdc = 2, 150
            self.curve = [{"tempC": 30, "speedPct": 25}, {"tempC": 80, "speedPct": 80}]
            self.zero_rpm = False

        def caps(self):
            return {"tuning": {"manualFan": True}}

        def tuning_get(self):
            tuning = super().tuning_get()
            tuning["vram"] = {"timingSupported": True, "timing": self.timing}
            tuning["power"].update(tdcSupported=True, tdcLimit=self.tdc)
            return tuning

        def fans_get(self):
            return {"curve": list(self.curve), "zeroRpmSupported": True,
                    "zeroRpm": self.zero_rpm}

        def tuning_reset(self):
            super().tuning_reset()
            self.timing, self.tdc, self.curve, self.zero_rpm = 1, 100, [], True

        def set_memory_timing(self, value):
            self.timing = value

        def set_tdc(self, value):
            self.tdc = value

        def set_fan_curve(self, value):
            self.curve = list(value)

        def set_zero_rpm(self, value):
            self.zero_rpm = value

    bridge = FullBridge()
    expected_fans = bridge.fans_get()
    applier = TuningApplier(bridge, SearchSpace.from_tuning(bridge.tuning_get(), enabled=[VOLTAGE]))
    applier.apply({VOLTAGE: 950})
    assert (bridge.voltage, bridge.clock, bridge.power) == (950, 2800, 5)
    assert (bridge.timing, bridge.tdc) == (2, 150)
    assert bridge.fans_get() == expected_fans


@pytest.fixture
def watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: str(tmp_path))
    return Watchdog(probation_sec=0)


def test_context_entry_failure_restores_before_propagating(watchdog):
    applied = []

    def apply(config):
        applied.append(config)
        if config[VOLTAGE] == -100:
            raise BridgeError("partial write")

    with pytest.raises(BridgeError, match="partial write"):
        with GuardedApply(watchdog, {VOLTAGE: -100}, {VOLTAGE: 0}, apply):
            pytest.fail("failed application must never enter the block")
    assert applied == [{VOLTAGE: -100}, {VOLTAGE: 0}]
    assert Watchdog().check_previous_session() is None


def test_failed_context_restore_keeps_recovery_journal(watchdog):
    def fail(config):
        raise BridgeError("driver unavailable")

    with pytest.raises(BridgeError):
        with GuardedApply(watchdog, {VOLTAGE: -100}, {VOLTAGE: 0}, fail):
            pass
    assert Watchdog().check_previous_session(clear=False).config == {VOLTAGE: -100}


def recovery_stack(watchdog, reset):
    def fail(*args, **kwargs):
        raise BridgeError("restore failed")

    return SimpleNamespace(
        watchdog=watchdog, gpu_key="test",
        safeguard=SimpleNamespace(mark_unsafe=lambda *args: None),
        knowledge=SimpleNamespace(record_failure=lambda *args: None),
        applier=SimpleNamespace(apply=fail, reset=reset))


def test_startup_failed_restore_keeps_journal(watchdog):
    watchdog.set_known_good({VOLTAGE: 0})
    watchdog.journal({VOLTAGE: -100})

    def fail():
        raise BridgeError("reset failed")

    with pytest.raises(BridgeError, match="reset failed"):
        autostack.recover_previous_session(recovery_stack(watchdog, fail))
    assert Watchdog().check_previous_session(clear=False) is not None


def test_startup_reports_factory_fallback_accurately(watchdog):
    watchdog.set_known_good({VOLTAGE: 0})
    watchdog.journal({VOLTAGE: -100})
    message = autostack.recover_previous_session(recovery_stack(watchdog, lambda: None))
    assert "Restored factory tuning" in message
    assert Watchdog().check_previous_session() is None


def benchmark_session(watchdog, confirm_runs=1):
    bridge = Bridge()
    space = SearchSpace.from_tuning(bridge.tuning_get())
    applier = RecordingApplier({VOLTAGE: 0, MAX_CLOCK: 2800, POWER_LIMIT: 5})
    hub = SimpleNamespace(history=lambda *args: [], latest=None)
    session = BenchmarkSession(
        hub, applier, space, Safeguard(space), make_optimizer(space, seed=1),
        BenchmarkConfig(test="TimeSpy", confirm_runs=confirm_runs), watchdog=watchdog)
    session.baseline = applier.read_current()
    session.baseline_score = 100.0
    best = {**session.baseline, VOLTAGE: -50}
    result = BenchmarkResult("TimeSpy", "simulated", 0, False, graphics=110.0)
    session.trials = [BenchmarkTrial(0, best, result, 110.0, 10.0)]
    return session, applier, best


def test_stopped_benchmark_never_commits_an_earlier_winner(watchdog):
    session, applier, best = benchmark_session(watchdog)
    applier.apply(best)
    session._stop.set()
    session._commit_best()
    assert session.report.state == SessionState.ABORTED
    assert session.report.best_config is None
    assert applier.current == session.baseline


def test_benchmark_timeout_during_confirmation_restores_baseline(watchdog):
    session, applier, _ = benchmark_session(watchdog)
    session._wait_for_run = lambda: None
    session._commit_best()
    assert session.report.state == SessionState.ABORTED
    assert applier.current == session.baseline
    assert watchdog.pending is None


def test_confirmation_promotes_the_actual_winner_journal(watchdog):
    session, applier, best = benchmark_session(watchdog)
    watchdog.journal({VOLTAGE: -150}, "last trial was not the winner")
    session._wait_for_run = lambda: BenchmarkResult(
        "TimeSpy", "simulated", 0, False, graphics=109.0)
    session._commit_best()
    assert session.report.best_config == best
    assert watchdog.known_good() == best


def test_benchmark_stop_during_final_write_restores_without_promoting(watchdog):
    session, applier, _ = benchmark_session(watchdog)
    apply = applier.apply

    def stopping_apply(config, **kwargs):
        result = apply(config, **kwargs)
        if session.state == SessionState.APPLYING:
            session._stop.set()
        return result

    applier.apply = stopping_apply
    session._wait_for_run = lambda: BenchmarkResult(
        "TimeSpy", "simulated", 0, False, graphics=109.0)
    session._commit_best()
    assert session.report.state == SessionState.ABORTED
    assert session.report.best_config is None
    assert applier.current == session.baseline
    assert watchdog.pending is None
    assert watchdog.known_good() is None


@pytest.mark.parametrize("score", [None, 0, -1, float("nan"), float("inf")])
def test_confirmation_requires_a_positive_finite_comparable_score(watchdog, score):
    session, applier, _ = benchmark_session(watchdog)
    session._wait_for_run = lambda: BenchmarkResult(
        "TimeSpy", "simulated", 0, False, overall=999, graphics=score)
    session._commit_best()
    assert session.report.best_config is None
    assert applier.current == session.baseline


def test_every_confirmation_must_hold_up(watchdog):
    session, applier, _ = benchmark_session(watchdog, confirm_runs=2)
    results = iter([99.0, 110.0])
    session._wait_for_run = lambda: BenchmarkResult(
        "TimeSpy", "simulated", 0, False, graphics=next(results))
    session._commit_best()
    assert session.report.best_config is None
    assert applier.current == session.baseline


def test_frame_source_failure_does_not_stop_hardware_telemetry():
    bridge = SimpleNamespace(metrics=lambda: {"clockMhz": 2900, "hotspotC": 70})

    def fail(window):
        raise OSError("capture unavailable")

    hub = TelemetryHub(bridge, SimpleNamespace(stats=fail))
    published = []
    hub.subscribe(published.append)
    sample = hub.poll_once()
    assert sample.clock_mhz == 2900
    assert sample.frames is None
    assert published == [sample]


def test_bridge_failure_invalidates_latest_sample():
    def fail():
        raise BridgeError("unreachable")

    hub = TelemetryHub(SimpleNamespace(metrics=fail))
    hub._publish(Sample(t=1, clock_mhz=2900))
    assert hub.poll_once() is None
    assert hub.latest is None


def test_benchmark_thermal_abort_rejects_the_run(watchdog):
    session, _, _ = benchmark_session(watchdog)
    session._run_started = 10
    session._hub.history = lambda *args: [Sample(t=11, hotspot_c=110)]
    assert session._run_should_stop()
    assert session._stop.is_set()


def test_benchmark_does_not_attribute_earlier_heat_to_current_run(watchdog):
    session, _, _ = benchmark_session(watchdog)
    session._run_started = 10
    session._hub.history = lambda *args: [Sample(t=1, hotspot_c=110)]
    assert not session._run_should_stop()
