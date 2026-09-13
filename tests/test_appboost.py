"""App boost recovery tests use only an in-memory bridge."""

import threading

import pytest

from voltshift.appboost import AppBoostWatcher, BoostConfig
from voltshift.bridgeclient import BridgeError


class Bridge:
    def __init__(self):
        self.power, self.clock = 0, 2800
        self.calls = []
        self.reads = 0
        self.fail_boost_clock = False
        self.fail_restore_power = False
        self.fail_restore_clock = False

    def tuning_get(self):
        self.reads += 1
        return {"power": {"powerLimit": self.power}, "gfx": {"maxFreqMhz": self.clock}}

    def set_power_limit(self, value):
        self.calls.append(("power", value))
        if value == 0 and self.fail_restore_power:
            raise BridgeError("power restore unavailable")
        self.power = value

    def set_core_clocks(self, max_mhz):
        self.calls.append(("clock", max_mhz))
        if max_mhz == 3000 and self.fail_boost_clock:
            self.clock = max_mhz  # a failing driver call can still change state
            raise BridgeError("clock boost failed")
        if max_mhz == 2800 and self.fail_restore_clock:
            raise BridgeError("clock restore unavailable")
        self.clock = max_mhz


def watcher(bridge):
    return AppBoostWatcher(bridge, BoostConfig(
        apps=["game.exe"], power_limit_pct=10, max_clock_mhz=3000))


def test_partial_boost_failure_restores_the_original_baseline():
    bridge = Bridge()
    bridge.fail_boost_clock = True
    boost = watcher(bridge)
    with pytest.raises(BridgeError, match="clock boost failed"):
        boost._apply_boost({"game.exe"})
    assert (bridge.power, bridge.clock) == (0, 2800)
    assert not boost.boosted
    assert not boost._restore_pending
    assert (boost._saved_power, boost._saved_max_clock) == (None, None)


@pytest.mark.parametrize("value", [None, float("nan"), True, "2800"])
def test_missing_or_invalid_baseline_prevents_every_boost_write(value):
    bridge = Bridge()
    bridge.clock = value
    boost = watcher(bridge)
    with pytest.raises(BridgeError, match="complete app boost baseline"):
        boost._apply_boost({"game.exe"})
    assert bridge.calls == []
    assert not boost.boosted


def test_pending_state_exists_before_the_first_driver_write():
    bridge = Bridge()
    boost = watcher(bridge)
    write = bridge.set_power_limit

    def checked_write(value):
        assert boost.boosted
        assert boost._saved_power == 0
        assert boost._saved_max_clock == 2800
        write(value)

    bridge.set_power_limit = checked_write
    boost._apply_boost({"game.exe"})
    assert boost.boosted


def test_failed_restore_retains_only_failed_values_and_retries_without_new_baseline():
    bridge = Bridge()
    boost = watcher(bridge)
    boost._apply_boost({"game.exe"})
    bridge.fail_restore_clock = True
    assert boost._restore() is False
    assert (bridge.power, bridge.clock) == (0, 3000)
    assert boost._saved_power is None
    assert boost._saved_max_clock == 2800
    assert boost.active, "an unresolved restore must block replacing the watcher"
    reads, writes = bridge.reads, list(bridge.calls)
    boost._apply_boost({"game.exe"})
    boost.start()
    assert bridge.reads == reads
    assert bridge.calls == writes
    assert boost._thread is None
    bridge.fail_restore_clock = False
    boost.stop()
    assert bridge.clock == 2800
    assert not boost.active
    assert not boost.boosted


def test_failed_power_restore_still_attempts_clock_restore():
    bridge = Bridge()
    boost = watcher(bridge)
    boost._apply_boost({"game.exe"})
    bridge.fail_restore_power = True
    boost._restore()
    assert (bridge.power, bridge.clock) == (10, 2800)
    assert boost._saved_power == 0
    assert boost._saved_max_clock is None
    bridge.fail_restore_power = False
    boost._restore()
    assert (bridge.power, bridge.clock) == (0, 2800)


def test_stop_serializes_with_an_inflight_boost_write():
    bridge = Bridge()
    boost = watcher(bridge)
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    write = bridge.set_power_limit

    def slow_write(value):
        write(value)
        if value == 10:
            entered.set()
            assert release.wait(2)

    bridge.set_power_limit = slow_write
    applying = threading.Thread(target=boost._apply_boost, args=({"game.exe"},))
    stopping = threading.Thread(target=lambda: (boost.stop(), stopped.set()))
    applying.start()
    try:
        assert entered.wait(2)
        stopping.start()
        assert boost._stop.wait(2)
        assert not stopped.is_set()
    finally:
        release.set()
        applying.join(2)
        if stopping.ident is not None:
            stopping.join(2)
    assert not applying.is_alive()
    assert not stopping.is_alive()
    assert (bridge.power, bridge.clock) == (0, 2800)
    assert ("clock", 3000) not in bridge.calls
    assert not boost.boosted
    writes = list(bridge.calls)
    boost._apply_boost({"game.exe"})
    assert bridge.calls == writes


def test_a_broken_log_observer_does_not_block_partial_failure_recovery():
    bridge = Bridge()
    boost = watcher(bridge)
    bridge.fail_boost_clock = True

    def fail(*args):
        raise RuntimeError("window closed")

    boost.on_log_entry = fail
    with pytest.raises(BridgeError):
        boost._apply_boost({"game.exe"})
    assert (bridge.power, bridge.clock) == (0, 2800)
