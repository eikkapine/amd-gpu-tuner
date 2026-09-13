"""Verification that a knob ADLX advertises is one the driver actually honours."""

import pytest

from voltshift import gpuprofile
from voltshift.gpuprofile import verify_knob, verify_space
from voltshift.knowledge import KnowledgeStore
from voltshift.optimizer import SearchSpace
from voltshift.optimizer.space import MAX_CLOCK, POWER_LIMIT, VOLTAGE, VRAM_CLOCK

# An RX 9070 XT as its own ADLX actually reports it: voltage and max clock are
# offsets that read 0 at stock, there is no minimum-clock range at all, and
# VRAM is an absolute clock.
RDNA4_TUNING = {
    "gfx": {
        "interface": "MGT2_1", "voltageMv": 0, "maxFreqMhz": 0,
        "voltageRange": {"min": -200, "max": 0, "step": 1},
        "maxFreqRange": {"min": -500, "max": 1000, "step": 1},
    },
    "vram": {"maxFreqMhz": 2518, "maxFreqRange": {"min": 2518, "max": 3000, "step": 2}},
    "power": {"powerLimit": 0, "powerLimitRange": {"min": -30, "max": 10, "step": 1}},
}


@pytest.fixture(autouse=True)
def no_readback_delay(monkeypatch):
    monkeypatch.setattr(gpuprofile, "READBACK_DELAY_SEC", 0.0)


class FakeApplier:
    """An applier whose knobs can be told to silently ignore writes."""

    def __init__(self, initial, inert=(), clamp_to=None):
        self.values = dict(initial)
        self.inert = set(inert)
        self.clamp_to = clamp_to or {}
        self.writes = []

    def read_current(self):
        return dict(self.values)

    def apply(self, config, skip_unchanged=True):
        for name, value in config.items():
            self.writes.append((name, value))
            if name in self.inert:
                continue           # advertised, but the driver does nothing
            if name in self.clamp_to:
                value = self.clamp_to[name]
            self.values[name] = value
        return list(config)

    def reset(self):
        return []

    @property
    def last_applied(self):
        return dict(self.values)


@pytest.fixture
def space():
    return SearchSpace.from_tuning(RDNA4_TUNING)


def test_rdna4_space_has_no_minimum_clock(space):
    """RDNA 4 does not expose minimum-clock tuning, so it must not appear."""
    assert "min_clock_mhz" not in space.names
    assert MAX_CLOCK in space.names


def test_working_knob_verifies(space):
    applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0})
    check = verify_knob(applier, space.knob(VOLTAGE))
    assert check.supported
    assert "verified" in check.detail


def test_verification_restores_the_original_value(space):
    applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0})
    verify_knob(applier, space.knob(VOLTAGE))
    assert applier.values[VOLTAGE] == 0, "verification must leave the card as it was"


def test_verification_probes_in_the_safe_direction(space):
    """A test write must never briefly overvolt or overclock the card."""
    applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0})
    for name in (VOLTAGE, MAX_CLOCK, POWER_LIMIT):
        applier.writes.clear()
        verify_knob(applier, space.knob(name))
        probe = applier.writes[0][1]
        assert probe < 0, f"{name} probed upward ({probe}) instead of downward"


def test_ignored_knob_is_reported_unsupported(space):
    applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0},
                          inert=[MAX_CLOCK])
    check = verify_knob(applier, space.knob(MAX_CLOCK))
    assert not check.supported
    assert "ignored" in check.detail


def test_clamped_knob_is_reported_unsupported(space):
    class ClampedProbe(FakeApplier):
        def apply(self, config, skip_unchanged=True):
            if config.get(POWER_LIMIT) not in (None, 0):
                config = {**config, POWER_LIMIT: -3}
            return super().apply(config, skip_unchanged)
    applier = ClampedProbe({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0})
    check = verify_knob(applier, space.knob(POWER_LIMIT))
    assert not check.supported
    assert "clamped" in check.detail


def test_knob_without_headroom_is_skipped():
    """VRAM sitting at the bottom of its range with no room to move down."""
    tuning = {"vram": {"maxFreqMhz": 2518,
                       "maxFreqRange": {"min": 2518, "max": 2520, "step": 2}}}
    space = SearchSpace.from_tuning(tuning)
    applier = FakeApplier({VRAM_CLOCK: 2518})
    check = verify_knob(applier, space.knob(VRAM_CLOCK))
    # Nowhere below, so it probes upward by one step and that is fine.
    assert check.supported


def test_verify_space_drops_inert_knobs(space):
    applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0},
                          inert=[MAX_CLOCK, VRAM_CLOCK])
    narrowed, checks = verify_space(applier, space)
    assert MAX_CLOCK not in narrowed.names
    assert VRAM_CLOCK not in narrowed.names
    assert VOLTAGE in narrowed.names
    assert POWER_LIMIT in narrowed.names


def test_verify_space_preserves_voltage_semantics(space):
    applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0})
    narrowed, _ = verify_space(applier, space)
    assert narrowed.voltage_is_offset
    assert narrowed.interface == "MGT2_1"


def test_results_are_cached_per_card(space):
    store = KnowledgeStore(":memory:")
    try:
        applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518,
                               POWER_LIMIT: 0}, inert=[MAX_CLOCK])
        verify_space(applier, space, store, "card-a")
        assert store.knob_support("card-a")[MAX_CLOCK] is False

        # A second pass must trust the cache rather than write to the GPU again.
        applier.writes.clear()
        narrowed, checks = verify_space(applier, space, store, "card-a")
        assert applier.writes == []
        assert MAX_CLOCK not in narrowed.names
        assert all(c.detail == "cached" for c in checks)
    finally:
        store.close()


def test_force_retests_even_when_cached(space):
    store = KnowledgeStore(":memory:")
    try:
        applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518,
                               POWER_LIMIT: 0}, inert=[MAX_CLOCK])
        verify_space(applier, space, store, "card-a")

        # A driver update makes the knob work; --force must notice.
        applier.inert.clear()
        applier.writes.clear()
        narrowed, _ = verify_space(applier, space, store, "card-a", force=True)
        assert applier.writes
        assert MAX_CLOCK in narrowed.names
    finally:
        store.close()


def test_cache_is_per_card(space):
    store = KnowledgeStore(":memory:")
    try:
        applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518,
                               POWER_LIMIT: 0}, inert=[MAX_CLOCK])
        verify_space(applier, space, store, "card-a")
        assert store.knob_support("card-b") == {}
    finally:
        store.close()


def test_a_card_that_honours_nothing_yields_an_empty_space(space):
    applier = FakeApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0},
                          inert=[VOLTAGE, MAX_CLOCK, VRAM_CLOCK, POWER_LIMIT])
    narrowed, _ = verify_space(applier, space)
    assert not narrowed, "nothing responded, so there is nothing to tune"


def test_arch_note_describes_the_interface(space):
    note = gpuprofile.arch_note(space)
    assert "offset" in note.lower()
    assert "minimum" in note.lower()


def test_write_failure_is_reported_not_raised(space):
    class Exploding(FakeApplier):
        def apply(self, config, skip_unchanged=True):
            raise RuntimeError("driver said no")

    check = verify_knob(Exploding({VOLTAGE: 0}), space.knob(VOLTAGE))
    assert not check.supported
    assert "write failed" in check.detail


def test_failed_restore_aborts_verification_and_is_not_cached(space):
    from voltshift.bridgeclient import BridgeError
    class StuckProbe(FakeApplier):
        def apply(self, config, skip_unchanged=True):
            if self.writes and config.get(VOLTAGE) == 0:
                return []  # driver silently ignores restoration
            return super().apply(config, skip_unchanged)
    applier = StuckProbe(space.default_config())
    store = KnowledgeStore(":memory:")
    try:
        with pytest.raises(BridgeError, match="Restoration.*failed"):
            verify_space(applier, space, store, "card-a")
        assert store.knob_support("card-a") == {}
        assert all(name == VOLTAGE for name, _ in applier.writes)
    finally:
        store.close()
