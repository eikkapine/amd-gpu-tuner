"""Dynamic voltage runner regressions; no hardware calls."""

import threading
from types import SimpleNamespace

import pytest

from voltshift.bridgeclient import BridgeError
from voltshift.engine import EngineConfig, Threshold
from voltshift.runner import EngineRunner


class Bridge:
    def __init__(self):
        self.gfx = {"interface": "MGT2_1", "voltageRange": {"min": -200, "max": 0, "step": 1}}
        self.writes = []
        self.fail = False

    def tuning_get(self):
        return {"gfx": self.gfx}

    def set_voltage_offset(self, value):
        self.writes.append(value)
        if self.fail:
            raise BridgeError("driver rejected write")

    def tuning_reset(self):
        self.writes.append("reset")


class Hub:
    def __init__(self):
        self.callback = None
        self.offset = None

    def subscribe(self, callback):
        self.callback = callback
        return self.unsubscribe

    def unsubscribe(self):
        self.callback = None

    def set_applied_offset(self, value):
        self.offset = value


def make_runner(config=None):
    bridge, hub = Bridge(), Hub()
    runner = EngineRunner(bridge, config or EngineConfig(
        hysteresis_count=1, idle_offset_mv=-100, thresholds=[Threshold(3000, -150)]), hub=hub)
    return runner, bridge, hub


@pytest.mark.parametrize("interface", ["MGT1", "MGT2", "unknown"])
def test_relative_voltage_interfaces_cannot_start(interface):
    runner, bridge, hub = make_runner()
    bridge.gfx["interface"] = interface
    with pytest.raises(BridgeError, match="voltage-offset range"):
        runner.start()
    runner.stop()
    assert hub.callback is None
    assert not runner.running
    assert bridge.writes == []


@pytest.mark.parametrize("voltage_range", [
    {"min": 700, "max": 1200, "step": 1},
    {"min": -200, "max": 50, "step": 1},
    {"min": -200, "max": 0, "step": 0},
    {"min": 0, "max": -200, "step": 1},
    {"min": -200, "max": 0},
    {"min": -200, "max": 0, "step": True},
    None,
    [],
])
def test_unusable_driver_range_cannot_start(voltage_range):
    runner, bridge, hub = make_runner()
    bridge.gfx["voltageRange"] = voltage_range
    with pytest.raises(BridgeError):
        runner.start()
    assert bridge.writes == []
    assert hub.callback is None


@pytest.mark.parametrize("idle,threshold", [(-210, -100), (-100, -210), (1, -100), (-100, -151)])
def test_every_offset_is_validated_without_silent_clamping(idle, threshold):
    runner, bridge, hub = make_runner(EngineConfig(idle_offset_mv=idle,
        thresholds=[Threshold(3000, threshold)]))
    bridge.gfx["voltageRange"]["step"] = 5
    with pytest.raises(BridgeError, match="Configured offset"):
        runner.start()
    assert bridge.writes == []
    assert hub.callback is None


def test_driver_range_can_be_narrower_than_engine_limits():
    runner, bridge, _ = make_runner()
    bridge.gfx["voltageRange"]["min"] = -120
    with pytest.raises(BridgeError, match="Configured offset -150"):
        runner.start()


def test_repeated_offset_does_not_accumulate():
    runner, bridge, hub = make_runner()
    runner.start()
    for clock in (3100, 3200, 2900, 3100):
        runner._process_metrics({"clockMhz": clock})
    assert bridge.writes == [-150, -100, -150]
    assert runner.engine.current_mv == -150
    assert hub.offset == -150
    runner.stop(reset_gpu=False)


def test_failed_write_is_unknown_and_same_target_is_retried():
    runner, bridge, hub = make_runner()
    samples, changes, records = [], [], []
    runner.on_sample = samples.append
    runner.on_voltage_change = lambda *values: changes.append(values)
    runner._crash_logger = SimpleNamespace(record=lambda **values: records.append(values),
                                          on_voltage_changed=lambda *values: None)
    runner.start()
    bridge.fail = True
    runner._process_metrics({"clockMhz": 3100})
    assert runner.engine.current_mv is None
    assert hub.offset is None
    assert samples[-1]["appliedOffsetMv"] is None
    assert records[-1]["voltage"] is None
    assert changes == []
    bridge.fail = False
    runner._process_metrics({"clockMhz": 3100})
    assert bridge.writes == [-150, -150]
    assert changes == [(None, -150)]
    assert samples[-1]["appliedOffsetMv"] == -150
    runner.stop(reset_gpu=False)


def test_stop_waits_for_inflight_write_and_rejects_queued_samples():
    runner, bridge, _ = make_runner()
    entered, release = threading.Event(), threading.Event()
    original = bridge.set_voltage_offset

    def delayed(value):
        entered.set()
        assert release.wait(3)
        original(value)

    bridge.set_voltage_offset = delayed
    runner.start()
    sample_thread = threading.Thread(target=runner._process_metrics, args=({"clockMhz": 3100},))
    sample_thread.start()
    assert entered.wait(1)
    stop_thread = threading.Thread(target=runner.stop)
    stop_thread.start()
    assert runner._stop.wait(1)
    queued = threading.Thread(target=runner._process_metrics, args=({"clockMhz": 2900},))
    queued.start()
    release.set()
    for thread in (sample_thread, stop_thread, queued):
        thread.join(3)
        assert not thread.is_alive()
    assert bridge.writes == [-150, "reset"]
    assert runner.engine.current_mv is None
    assert not runner.running


def test_stop_does_not_join_its_own_polling_thread():
    runner, bridge, _ = make_runner()
    runner.start()
    runner._thread = threading.current_thread()
    runner.stop()
    assert bridge.writes == ["reset"]
