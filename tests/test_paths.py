from pathlib import Path

from voltshift import paths


def test_data_override_keeps_legacy_names(tmp_path, monkeypatch):
    target = tmp_path / "portable data"
    monkeypatch.setenv("AMD_GPU_TUNER_DATA_DIR", str(target))
    assert Path(paths.config_path()) == target / "voltshift_config.json"
    assert Path(paths.profiles_dir()) == target / "profiles"
    assert target.is_dir()


def test_bridge_override_is_authoritative(tmp_path, monkeypatch):
    target = tmp_path / "missing-bridge.exe"
    monkeypatch.setenv("AMD_GPU_TUNER_BRIDGE", str(target))
    assert Path(paths.bridge_path()) == target


def test_installed_package_uses_user_data_not_site_packages(tmp_path, monkeypatch):
    monkeypatch.delenv("AMD_GPU_TUNER_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "__file__", str(tmp_path / "site-packages" / "voltshift" / "paths.py"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert Path(paths.app_dir()) == tmp_path / "local" / "AMD GPU Tuner"
