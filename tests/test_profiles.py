"""Profile safety regressions. Every bridge is mocked; no GPU writes occur."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from voltshift import profiles
from voltshift.bridgeclient import BridgeClient, BridgeError
from voltshift.engine import EngineConfig


GPU = {
    "name": "AMD Radeon RX 9070 XT", "vendorId": "1002", "deviceId": "7550",
    "revisionId": "C0", "vramMb": 16304,
    "bios": {"partNumber": "113-EXT112333-100", "version": "023.008.000.068",
             "date": "2025/02/11 21:22"},
}
# Range shapes from the read-only RX 9070 XT diagnostic: maximum clock is
# a signed offset, and the zero-width minimum control is not writable.
TUNING = {
    "gfx": {"interface": "MGT2_1", "voltageMv": -100,
            "voltageRange": {"min": -200, "max": 0, "step": 1},
            "minFreqMhz": 0, "minFreqRange": {"min": 0, "max": 0, "step": 0},
            "maxFreqMhz": -100, "maxFreqRange": {"min": -500, "max": 1000, "step": 1}},
    "vram": {"maxFreqMhz": 2518, "timingSupported": True, "timing": 0},
    "power": {"powerLimit": 0, "tdcSupported": False},
}


@pytest.fixture
def bridge():
    result = Mock(spec=BridgeClient)
    result.info.return_value = copy.deepcopy(GPU)
    result.tuning_get.return_value = copy.deepcopy(TUNING)
    result.fans_get.return_value = {"curve": [{"tempC": 30, "speedPct": 30},
                                            {"tempC": 85, "speedPct": 95}],
                                    "zeroRpmSupported": True, "zeroRpm": True}
    result.gfx_get.return_value = {"antiLag": {"supported": True, "enabled": True}}
    result.display_list.return_value = [{"index": 0, "uniqueId": 12, "name": "Monitor"}]
    result.display_get.return_value = {"freeSync": {"supported": True, "enabled": True}}
    result.media_get.return_value = {"videoUpscale": {"supported": True, "enabled": False}}
    return result


def document(**sections):
    return {"format": profiles.PROFILE_FORMAT, "binding": copy.deepcopy(GPU), **sections}


def test_capture_all_round_trips_and_binds_gpu(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(profiles.paths, "profiles_dir", lambda: str(tmp_path))
    profile = profiles.capture(bridge, EngineConfig())
    assert set(profiles.SECTIONS) <= profile.keys()
    assert profile["binding"] == GPU
    assert profile["tuning"]["maxFreqMhz"] == -100
    assert profile["tuning"]["maxFreqMode"] == "offset"
    assert "minFreqMhz" not in profile["tuning"]
    path = profiles.save(profile, "Quiet gaming")
    assert profiles.load(path) == profile
    assert not list(tmp_path.glob("*.tmp"))
    bridge.reset_mock()
    log = profiles.apply(bridge, profile)
    bridge.set_core_clocks.assert_called_once_with(None, -100)
    assert any("engine configuration" in line for line in log)


@pytest.mark.parametrize("selection", [set(), {"unknown"}, "tuning"])
def test_capture_invalid_selection_never_reads_or_writes(bridge, selection):
    with pytest.raises(ValueError):
        profiles.capture(bridge, EngineConfig(), selection)
    assert not bridge.mock_calls


def test_capture_only_selected_section(bridge):
    result = profiles.capture(bridge, EngineConfig(), {"fans"})
    assert set(result) & set(profiles.SECTIONS) == {"fans"}
    bridge.tuning_get.assert_not_called()
    bridge.display_list.assert_not_called()


def test_capture_never_mistakes_absolute_voltage_for_offset(bridge):
    bridge.tuning_get.return_value["gfx"].update(
        interface="MGT2", voltageMv=1100, voltageRange={"min": 700, "max": 1200, "step": 1})
    result = profiles.capture(bridge, EngineConfig(), {"tuning"})
    assert "voltageMv" not in result["tuning"]


@pytest.mark.parametrize("bad", [
    [], {"format": True, "tuning": {}}, {"format": 42, "tuning": {}},
    {"format": 3, "tuning": {}}, document(), document(tuning=[]),
    document(tuning={"voltageMv": 1100}), document(tuning={"voltageMv": -201}),
    document(tuning={"vramMaxMhz": True}), document(tuning={"powerLimitPct": 1.5}),
    document(tuning={"maxFreqMhz": 1000, "minFreqMhz": 2000, "maxFreqMode": "absolute"}),
    document(tuning={"maxFreqMhz": -100, "maxFreqMode": "absolute"}),
    document(engine={"poll_interval_sec": float("nan")}),
    document(engine={"poll_interval_sec": 2**1024}),
    document(engine={"hysteresis_count": False}),
    document(engine={"thresholds": [{"clock_mhz": 1, "offset_mv": -10},
                                     {"clock_mhz": 1, "offset_mv": -20}]}),
    document(fans={"curve": [{"tempC": 80, "speedPct": 80}, {"tempC": 40, "speedPct": 40}]}),
    document(fans={"curve": [{"tempC": 40, "speedPct": 80}, {"tempC": 80, "speedPct": 40}]}),
    document(gfx={"antiLag": {"enabled": "false"}}),
    document(gfx={"chill": {"minFps": 144, "maxFps": 60}}),
    document(display=[{"uniqueId": [], "freeSync": {"enabled": True}}]),
    document(display=[{"uniqueId": 1}, {"uniqueId": "1"}]),
    document(media={"videoUpscale": {"sharpness": 101}}),
    document(media={"unknown": {"enabled": True}}),
])
def test_invalid_documents_fail_before_any_bridge_call(bridge, bad):
    with pytest.raises(ValueError):
        profiles.apply(bridge, bad)
    assert not bridge.mock_calls


def test_invalid_later_section_prevents_earlier_voltage_write(bridge):
    profile = document(tuning={"voltageMv": -100}, media={"videoUpscale": {"enabled": 1}})
    with pytest.raises(ValueError):
        profiles.apply(bridge, profile, {"tuning"})
    assert not bridge.mock_calls


@pytest.mark.parametrize("field,value", [("deviceId", "different"), ("revisionId", "different"),
                                         ("name", "Another GPU"), ("vramMb", 8192)])
def test_gpu_mismatch_prevents_writes(bridge, field, value):
    bridge.info.return_value[field] = value
    with pytest.raises(ValueError, match="GPU mismatch"):
        profiles.apply(bridge, document(tuning={"voltageMv": -100}))
    bridge.set_voltage_offset.assert_not_called()


def test_bios_mismatch_or_missing_bios_prevents_writes(bridge):
    bridge.info.return_value["bios"] = {}
    with pytest.raises(ValueError, match="VBIOS mismatch"):
        profiles.apply(bridge, document(tuning={"voltageMv": -100}))
    bridge.set_voltage_offset.assert_not_called()


def test_max_clock_semantic_mismatch_fails_before_voltage_write(bridge):
    bridge.tuning_get.return_value["gfx"]["maxFreqRange"]["min"] = 500
    with pytest.raises(ValueError, match="different units"):
        profiles.apply(bridge, document(tuning={"voltageMv": -100,
                                                "maxFreqMhz": -100, "maxFreqMode": "offset"}))
    bridge.set_voltage_offset.assert_not_called()


def test_legacy_signed_clock_profile_is_supported_with_warning(bridge):
    log = profiles.apply(bridge, {"format": 2, "gpu": GPU["name"],
                                  "tuning": {"minFreqMhz": 0, "maxFreqMhz": -100}})
    assert any("legacy profile" in line for line in log)
    bridge.set_core_clocks.assert_called_once_with(0, -100)


def test_engine_load_never_starts_or_writes_and_selected_engine_needs_no_gpu(bridge):
    profile = document(engine=EngineConfig().to_dict(), tuning={"voltageMv": -100})
    assert profiles.engine_config(profile).to_dict() == profile["engine"]
    assert "engine configuration" in profiles.apply(bridge, profile, {"engine"})[0]
    assert not bridge.mock_calls


@pytest.mark.parametrize("selection", [set(), {"media"}])
def test_no_matching_apply_selection_never_touches_bridge(bridge, selection):
    with pytest.raises(ValueError):
        profiles.apply(bridge, document(tuning={"voltageMv": -100}), selection)
    assert not bridge.mock_calls


def test_apply_only_selected_section_does_not_query_displays(bridge):
    profile = document(tuning={"voltageMv": -100}, media={"videoUpscale": {"enabled": False}})
    profiles.apply(bridge, profile, {"media"})
    bridge.set_voltage_offset.assert_not_called()
    bridge.display_list.assert_not_called()
    bridge.media_set.assert_called_once_with("videoUpscale", enabled=False)


@pytest.mark.parametrize("uid,connected", [(None, [{"index": 0, "uniqueId": None}]),
                                          (12, [{"index": 0, "uniqueId": 12}, {"index": 1, "uniqueId": 12}]),
                                          (99, [{"index": 0, "uniqueId": 12}])])
def test_missing_ambiguous_or_absent_display_identity_never_writes(bridge, uid, connected):
    bridge.display_list.return_value = connected
    log = profiles.apply(bridge, document(display=[{"uniqueId": uid, "freeSync": {"enabled": True}}]))
    assert any("not uniquely identified" in line for line in log)
    bridge.display_set.assert_not_called()


def test_display_string_id_matches_reindexed_monitor(bridge):
    bridge.display_list.return_value = [{"index": 2, "uniqueId": 12}]
    profiles.apply(bridge, document(display=[{"uniqueId": "12", "freeSync": {"enabled": False}}]))
    bridge.display_set.assert_called_once_with(2, "freeSync", enabled=False)


def test_driver_rejection_reported_without_claiming_application(bridge):
    bridge.set_voltage_offset.side_effect = BridgeError("Unsupported control")
    log = profiles.apply(bridge, document(tuning={"voltageMv": -100, "powerLimitPct": 0}))
    assert log == ["skipped: voltage offset (Unsupported control)", "applied: power limit"]


def test_failed_replace_preserves_old_profile_and_cleans_temporary(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles.paths, "profiles_dir", lambda: str(tmp_path))
    old = document(tuning={"voltageMv": -100})
    path = profiles.save(old, "quiet")
    monkeypatch.setattr(profiles.os, "replace", Mock(side_effect=OSError("locked")))
    with pytest.raises(OSError, match="locked"):
        profiles.save(document(tuning={"voltageMv": -50}), "quiet")
    assert profiles.load(path) == old
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("name", ["", "???", "CON", "lpt1", "a" * 121, "../../escape", "quiet?"])
def test_invalid_windows_names_fail_before_file_write(tmp_path, monkeypatch, name):
    monkeypatch.setattr(profiles.paths, "profiles_dir", lambda: str(tmp_path))
    with pytest.raises(ValueError):
        profiles.save(document(tuning={}), name)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("text", ['[]', '{"format":2,"tuning":{},"tuning":{}}',
                                  '{"format":2,"engine":{"poll_interval_sec":NaN}}',
                                  '{"format":2,"tuning":{"maxFreqMhz":Infinity}}'])
def test_file_and_editor_share_strict_json_validation(tmp_path, text):
    path = tmp_path / "import.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        profiles.load(str(path))
    with pytest.raises(ValueError):
        profiles.parse(text)


def test_oversized_profile_and_invalid_utf8_rejected(tmp_path):
    path = tmp_path / "import.json"
    path.write_bytes(b" " * (profiles.MAX_PROFILE_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        profiles.load(str(path))
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        profiles.load(str(path))


def test_listing_excludes_directories_and_accepts_uppercase_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles.paths, "profiles_dir", lambda: str(tmp_path))
    (tmp_path / "not-a-file.json").mkdir()
    path = tmp_path / "quiet.JSON"
    path.write_text("{}")
    assert profiles.list_profiles() == [str(path)]


def test_empty_hardware_sections_are_reported_as_skipped(bridge):
    assert profiles.apply(bridge, document(tuning={})) == [
        "skipped: selected sections contain no writable settings"]


@pytest.mark.parametrize("active", ["engine_running", "autotune_running", "governor_running", "appboost_active"])
def test_gui_apply_blocks_competing_controllers(active):
    from voltshift.gui.pages.profiles import ProfilesPage

    state = SimpleNamespace(connected=True, engine_running=False, autotune_running=False,
                            governor_running=False, appboost_active=False, log=Mock())
    setattr(state, active, True)
    page = SimpleNamespace(state=state, _edited_profile=Mock())
    ProfilesPage._apply(page)
    page._edited_profile.assert_not_called()
    assert "Stop the engine" in state.log.call_args.args[0]


def test_gui_load_engine_is_offline_and_does_not_start_it():
    from voltshift.gui.pages.profiles import ProfilesPage

    config = EngineConfig(idle_offset_mv=-75)
    state = SimpleNamespace(engine_config=EngineConfig(), engine_running=False,
                            save_settings=Mock(), log=Mock(), bridge=Mock(), start_engine=Mock())
    page = SimpleNamespace(state=state, _edited_profile=lambda: document(engine=config.to_dict()))
    ProfilesPage._load_engine(page)
    assert state.engine_config.idle_offset_mv == -75
    state.save_settings.assert_called_once()
    state.start_engine.assert_not_called()
    assert not state.bridge.mock_calls
