import json

from voltshift import cli, diagnostics
from voltshift.bridgeclient import BridgeError


class ReadOnlyBridge:
    version = "test"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def info(self):
        return {"name": "Test Radeon", "driverPath": "private-local-path",
                "bios": {"version": "test-bios"}}

    def caps(self):
        return {"tuning": {"manualGfx": True}}

    def tuning_get(self):
        return {"power": {"powerLimit": 5}}

    def fans_get(self):
        raise BridgeError("unsupported")

    def metrics(self):
        return {"boardPowerW": 100.5, "clockMhz": 2400}


def test_report_uses_only_readers_and_omits_private_path():
    report = diagnostics.collect_report(ReadOnlyBridge())
    assert report["readOnly"] is True
    assert report["errors"] == {"fans": "Readback unavailable"}
    assert "private-local-path" not in json.dumps(report)
    summary = diagnostics.format_summary(report)
    assert "Measured board power: 100.5 W" in summary
    assert "Configured power limit: 5%" in summary
    assert "unavailable" in summary


def test_status_and_info_emit_parseable_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "BridgeClient", ReadOnlyBridge)
    for command in ("status", "info"):
        assert cli.main([command, "--json"]) == 0
        output = capsys.readouterr()
        assert json.loads(output.out)["info"]["name"] == "Test Radeon"
        assert output.err == ""


def test_diagnostics_export(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "BridgeClient", ReadOnlyBridge)
    destination = tmp_path / "report.json"
    assert cli.main(["diagnostics", "--output", str(destination)]) == 0
    assert json.loads(destination.read_text())["readOnly"]
    assert str(destination) in capsys.readouterr().out


def test_atomic_export_preserves_old_file_on_failure(tmp_path, monkeypatch):
    destination = tmp_path / "report.json"
    destination.write_text("original")
    def fail(*args):
        raise OSError("disk error")
    monkeypatch.setattr(diagnostics.os, "replace", fail)
    import pytest
    with pytest.raises(OSError):
        diagnostics.save_report({"new": True}, str(destination))
    assert destination.read_text() == "original"
    assert list(tmp_path.iterdir()) == [destination]


def test_invalid_cli_numbers_rejected_before_bridge_start(monkeypatch):
    import pytest
    def fail():
        raise AssertionError("bridge must not start")
    monkeypatch.setattr(cli, "BridgeClient", fail)
    for args in (["metrics", "--interval", "nan"], ["autotune", "--trials", "0"],
                 ["autotune", "--window", "inf"], ["adaptive", "--probes", "-1"],
                 ["benchtune", "--timeout", "-3"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 2


def test_profile_inspection_does_not_start_bridge(monkeypatch, tmp_path, capsys):
    def fail():
        raise AssertionError("bridge must not start")
    monkeypatch.setattr(cli, "BridgeClient", fail)
    monkeypatch.setattr(cli.profile_store, "list_profiles", lambda: [])
    assert cli.main(["profile", "list"]) == 0
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"format": 2, "engine": {}}))
    assert cli.main(["profile", "inspect", str(profile)]) == 0
    assert json.loads(capsys.readouterr().out)["format"] == 2
