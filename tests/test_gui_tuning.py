"""GUI actions with mocked bridge calls, plus a real Tk widget-lifecycle check."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

pytest.importorskip("customtkinter")

from voltshift.bridgeclient import BridgeError
from voltshift.gui.pages.tuning import CONTROLS, TuningPage, control_range
from voltshift.gui.state import AppState
from voltshift.gui.widgets import LabeledSlider


def snapshot():
    return {
        "gfx": {"voltageMv": 0, "voltageRange": {"min": -200, "max": 0, "step": 5},
                "minFreqMhz": 500, "minFreqRange": {"min": 500, "max": 3000, "step": 10},
                "maxFreqMhz": 2800, "maxFreqRange": {"min": 500, "max": 3400, "step": 10}},
        "vram": {"unsupported": "Unavailable"},
        "power": {"powerLimit": 0, "powerLimitRange": {"min": -30, "max": 15, "step": 1},
                  "tdcSupported": True, "tdcLimit": 50,
                  "tdcRange": {"min": 20, "max": 100, "step": 2}},
    }


def make_page(data=None):
    page = object.__new__(TuningPage)
    bridge = Mock()
    bridge.tuning_get.return_value = data or snapshot()
    page.state = SimpleNamespace(connected=True, bridge=bridge, log=Mock())
    page._applied = {}
    page._supported = set()
    page._sliders = {key: Mock() for key in CONTROLS}
    for slider in page._sliders.values():
        slider.set.side_effect = lambda value, selected=slider: setattr(selected.get, "return_value", value)
    page._buttons = {key: Mock() for key in ("gfx", "vram", "power")}
    page._group_status = {key: Mock() for key in page._buttons}
    page._timing_options = {}
    page._timing_menu = Mock()
    page._reset_button = Mock()
    page._status = Mock()
    page._load_snapshot(bridge.tuning_get.return_value)
    return page


def last_notice(page):
    return page._status.configure.call_args.kwargs["text"]


def test_unavailable_and_unchanged_controls_never_write():
    page = make_page()
    for group in ("gfx", "vram", "power"):
        page._apply_group(group)
    assert page.state.bridge.method_calls == []
    assert "vram_clock" not in page._supported


def test_voltage_only_apply_does_not_write_clocks_and_checks_readback():
    page = make_page()
    before = snapshot()
    after = deepcopy(before)
    after["gfx"]["voltageMv"] = -50
    page.state.bridge.tuning_get.side_effect = [before, after]
    page._sliders["voltage"].get.return_value = -50
    page._apply_group("gfx")
    page.state.bridge.set_voltage_offset.assert_called_once_with(-50)
    page.state.bridge.set_core_clocks.assert_not_called()
    assert "confirmed by driver readback" in last_notice(page)
    assert page._applied["voltage"] == -50


def test_tdc_change_is_applied_without_unchanged_power_write():
    page = make_page()
    before = snapshot()
    after = deepcopy(before)
    after["power"]["tdcLimit"] = 48
    page.state.bridge.tuning_get.side_effect = [before, after]
    page._sliders["tdc"].get.return_value = 48
    page._apply_group("power")
    page.state.bridge.set_tdc.assert_called_once_with(48)
    page.state.bridge.set_power_limit.assert_not_called()


@pytest.mark.parametrize("controller", ["engine_running", "autotune_running", "governor_running", "appboost_active"])
def test_competing_controller_blocks_manual_apply_and_reset(controller):
    page = make_page()
    setattr(page.state, controller, True)
    page._sliders["voltage"].get.return_value = -50
    page._apply_group("gfx")
    page._reset()
    assert page.state.bridge.method_calls == []
    assert "before manual tuning" in last_notice(page)


def test_stale_driver_state_requires_restaging_without_writes():
    page = make_page()
    fresh = snapshot()
    fresh["gfx"]["voltageMv"] = -25
    page.state.bridge.tuning_get.return_value = fresh
    page._sliders["voltage"].get.return_value = -50
    page._apply_group("gfx")
    page.state.bridge.set_voltage_offset.assert_not_called()
    assert "changed since" in last_notice(page)
    assert page._applied["voltage"] == -25


def test_readback_mismatch_is_not_reported_as_success():
    page = make_page()
    page._sliders["voltage"].get.return_value = -50
    page._apply_group("gfx")
    assert "differs" in last_notice(page)
    assert page._applied["voltage"] == 0


def test_failed_read_disables_previously_loaded_controls():
    page = make_page()
    page.state.bridge.tuning_get.side_effect = BridgeError("offline")
    assert not page._refresh()
    assert not page._supported
    assert "Read failed" in last_notice(page)
    page._apply_group("gfx")
    page.state.bridge.set_voltage_offset.assert_not_called()


def test_partial_apply_failure_reads_actual_values_and_surfaces_error():
    page = make_page()
    page._sliders["voltage"].get.return_value = -50
    page._sliders["max_clock"].get.return_value = 2900
    page.state.bridge.set_core_clocks.side_effect = BridgeError("driver rejected clocks")
    page._apply_group("gfx")
    assert "Earlier writes may have succeeded" in last_notice(page)
    assert page.state.bridge.tuning_get.call_count == 2


def test_signed_core_offset_is_not_compared_with_absolute_minimum():
    before = snapshot()
    before["gfx"].update(minFreqMhz=0, minFreqRange={"min": 0, "max": 0, "step": 0},
                          maxFreqMhz=0, maxFreqRange={"min": -500, "max": 1000, "step": 1})
    page = make_page(before)
    after = deepcopy(before)
    after["gfx"]["maxFreqMhz"] = -100
    page.state.bridge.tuning_get.side_effect = [before, after]
    page._sliders["max_clock"].get.return_value = -100
    page._apply_group("gfx")
    page.state.bridge.set_core_clocks.assert_called_once_with(None, -100)
    assert "min_clock" not in page._supported


def test_invalid_absolute_clock_order_is_rejected_before_any_write():
    page = make_page()
    page._sliders["min_clock"].get.return_value = 3000
    page._sliders["voltage"].get.return_value = -50
    page._apply_group("gfx")
    page.state.bridge.set_voltage_offset.assert_not_called()
    page.state.bridge.set_core_clocks.assert_not_called()
    assert "must not exceed" in last_notice(page)


@pytest.mark.parametrize("limits", [{}, {"min": 0, "max": 10, "step": 0},
                                  {"min": 10, "max": 0, "step": 1}])
def test_malformed_ranges_are_disabled(limits):
    assert control_range({"value": 0, "range": limits}, "value", "range") is None


def test_absolute_voltage_is_not_exposed_as_offset():
    assert control_range({"v": 1000, "r": {"min": 600, "max": 1200, "step": 5}},
                         "v", "r", voltage=True) is None


def test_slider_preserves_readback_and_respects_hardware_step():
    slider = object.__new__(LabeledSlider)
    slider._slider = Mock()
    slider._value = Mock()
    slider._name_label = Mock()
    slider._unit = "MHz"
    slider._supported = True
    slider._command = Mock()
    slider.configure_range(100, 111, 3)
    assert slider._slider.configure.call_args.kwargs["to"] == 109
    slider.set(101)
    assert slider.get() == 101
    slider._on_move(105)
    assert slider.get() == 106
    slider.set_supported(False)
    slider._on_release(None)
    slider._command.assert_not_called()


def test_real_widgets_keep_tk_identity_and_destroy_cleanly():
    import customtkinter as ctk
    from voltshift.gui.pages.profiles import ProfilesPage
    root = ctk.CTk()
    root.withdraw()
    try:
        page = ProfilesPage(root, Mock())
        page.ensure_built()
        slider = LabeledSlider(root, "Test clock", 0, 10, 1)
        # Tk uses _name to remove children during destruction. Replacing it
        # with an entry/label leaves a scrollable page in a recursive cycle.
        assert isinstance(page._name, str)
        assert isinstance(slider._name, str)
        page.destroy()
        slider.destroy()
    finally:
        root.destroy()


def make_state(monkeypatch):
    from voltshift.gui import state as module
    bridge = Mock()
    monkeypatch.setattr(module, "BridgeClient", lambda: bridge)
    monkeypatch.setattr(AppState, "_load_settings", lambda self: None)
    monkeypatch.setattr(module, "CrashLogger", Mock())
    stack = Mock(verified=False, tunable=True)
    stack.space.names = ["voltage_mv"]
    monkeypatch.setattr(module.autostack, "build", Mock(return_value=stack))
    recovery = Mock(return_value=None)
    monkeypatch.setattr(module.autostack, "recover_previous_session", recovery)
    state = AppState(Mock())
    return state, stack, recovery


def test_passive_connect_never_recovers_or_verifies_hardware(monkeypatch):
    state, stack, recovery = make_state(monkeypatch)
    assert state.connect()
    recovery.assert_not_called()
    stack.verify_controls.assert_not_called()
    assert [call[0] for call in state.bridge.method_calls] == ["start", "info", "caps"]


def test_explicit_verification_recovers_once_and_cached_start_succeeds(monkeypatch):
    state, stack, recovery = make_state(monkeypatch)
    state.stack = stack
    assert state.verify_controls()
    stack.verified = True
    assert state.verify_controls()
    recovery.assert_called_once_with(stack)
    stack.verify_controls.assert_called_once()


def test_verification_failure_blocks_start_even_when_controls_advertised(monkeypatch):
    state, stack, _ = make_state(monkeypatch)
    state.stack = stack
    stack.verify_controls.side_effect = BridgeError("write failed")
    assert not state.verify_controls()


def test_recovery_failure_blocks_verification_and_remains_retryable(monkeypatch):
    state, stack, recovery = make_state(monkeypatch)
    state.stack = stack
    recovery.side_effect = BridgeError("restore failed")
    assert not state.verify_controls()
    assert not state._recovery_checked
    stack.verify_controls.assert_not_called()


def test_worker_post_never_calls_tk_and_main_thread_drain_delivers(monkeypatch):
    state, _, _ = make_state(monkeypatch)
    state._root.reset_mock()
    callback = Mock()
    worker = threading.Thread(target=lambda: state.post(callback, "sample"))
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert state._root.method_calls == []
    callback.assert_not_called()
    state._drain_posts()
    callback.assert_called_once_with("sample")
    state._root.after.assert_called_once()


def test_closing_drops_queued_callbacks_and_does_not_reschedule(monkeypatch):
    state, _, _ = make_state(monkeypatch)
    callback = Mock()
    state.post(callback)
    state.shutdown()
    state._root.reset_mock()
    state.post(callback)
    state._drain_posts()
    callback.assert_not_called()
    state._root.after.assert_not_called()
    assert state._post_queue.empty()
