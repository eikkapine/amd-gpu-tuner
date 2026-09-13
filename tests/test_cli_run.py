"""CLI dynamic-run cleanup and output, without drivers or real signals."""

from types import SimpleNamespace

import pytest

from voltshift import cli
from voltshift.bridgeclient import BridgeError
from voltshift.engine import EngineConfig


@pytest.fixture
def command(monkeypatch):
    events = []
    state = SimpleNamespace(start_error=None, stop_error=None, footer_error=None,
                            header_error=None, sample=None, running=False)
    previous_handler = object()
    handlers = [previous_handler]

    class Bridge:
        def __enter__(self):
            events.append("bridge.start")
            return self

        def __exit__(self, *args):
            events.append("bridge.stop")

        def info(self):
            return {"name": "simulated GPU"}

    class Logger:
        crash_count = 0

        def check_previous_session(self):
            pass

        def start(self):
            events.append("logger.start")

        def write_session_header(self, *args):
            events.append("logger.header")
            if state.header_error:
                raise state.header_error

        def stop(self):
            events.append("logger.stop")

        def write_session_footer(self, *args):
            events.append("logger.footer")
            if state.footer_error:
                raise state.footer_error

    class Runner:
        on_sample = None

        @property
        def running(self):
            return state.running

        def start(self):
            events.append("runner.start")
            if state.start_error:
                raise state.start_error
            if self.on_sample and state.sample is not None:
                self.on_sample(state.sample)

        def stop(self, *, reset_gpu):
            events.append(("runner.stop", reset_gpu))
            if state.stop_error:
                raise state.stop_error

    runner = Runner()
    monkeypatch.setattr(cli, "BridgeClient", Bridge)
    monkeypatch.setattr(cli, "CrashLogger", lambda *args: Logger())
    monkeypatch.setattr(cli, "EngineRunner", lambda *args: runner)
    monkeypatch.setattr(cli, "_load_engine_config", lambda *args: EngineConfig())
    monkeypatch.setattr(cli.signal, "getsignal", lambda *args: handlers[-1])

    def install(sig, handler):
        handlers.append(handler)
        events.append("signal.restore" if handler is previous_handler else "signal.install")

    monkeypatch.setattr(cli.signal, "signal", install)
    return state, events, handlers, previous_handler


def test_rejected_start_cleans_up_without_resetting_gpu(command):
    state, events, handlers, previous = command
    state.start_error = BridgeError("unsupported offset semantics")
    with pytest.raises(BridgeError, match="unsupported offset"):
        cli.cmd_run(SimpleNamespace(config=None, quiet=True))
    assert events[-5:] == [("runner.stop", False), "logger.stop", "logger.footer",
                          "signal.restore", "bridge.stop"]
    assert handlers[-1] is previous


def test_normal_stop_resets_and_restores_previous_handler(command):
    _, events, handlers, previous = command
    assert cli.cmd_run(SimpleNamespace(config=None, quiet=True)) == 0
    assert ("runner.stop", True) in events
    assert events.index("logger.stop") < events.index("logger.footer") < events.index("signal.restore")
    assert handlers[-1] is previous


@pytest.mark.parametrize("failure", ["stop_error", "footer_error", "header_error"])
def test_cleanup_continues_when_another_cleanup_or_header_fails(command, failure):
    state, events, handlers, previous = command
    setattr(state, failure, RuntimeError(failure))
    with pytest.raises(RuntimeError, match=failure):
        cli.cmd_run(SimpleNamespace(config=None, quiet=True))
    assert "logger.stop" in events
    assert "logger.footer" in events
    assert events[-1] == "bridge.stop"
    assert handlers[-1] is previous


def test_loop_failure_still_resets_and_cleans_up(command, monkeypatch):
    state, events, handlers, previous = command
    state.running = True

    def fail(_):
        raise RuntimeError("loop interrupted")

    monkeypatch.setattr(cli.time, "sleep", fail)
    with pytest.raises(RuntimeError, match="loop interrupted"):
        cli.cmd_run(SimpleNamespace(config=None, quiet=True))
    assert ("runner.stop", True) in events
    assert "logger.footer" in events
    assert handlers[-1] is previous


def test_ctrl_c_leaves_through_cleanup(command, monkeypatch):
    state, events, handlers, previous = command
    state.running = True
    monkeypatch.setattr(cli.time, "sleep", lambda _: handlers[-1](cli.signal.SIGINT, None))
    assert cli.cmd_run(SimpleNamespace(config=None, quiet=True)) == 0
    assert ("runner.stop", True) in events
    assert handlers[-1] is previous


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), "unknown"])
def test_unknown_sample_values_are_dashes(command, capsys, missing):
    state, _, _, _ = command
    state.sample = {"clockMhz": missing, "appliedOffsetMv": missing,
                    "tempC": missing, "boardPowerW": missing}
    cli.cmd_run(SimpleNamespace(config=None, quiet=False))
    sample_line = next(line for line in capsys.readouterr().out.splitlines() if "°C" in line)
    assert "- MHz" in sample_line
    assert "- mV" in sample_line
    assert "-°C" in sample_line
    assert "- W" in sample_line
    assert "+0" not in sample_line


def test_zero_is_known_and_signed_in_sample_output(command, capsys):
    state, _, _, _ = command
    state.sample = {"clockMhz": 3100, "appliedOffsetMv": 0, "tempC": 55.5, "powerW": 150}
    cli.cmd_run(SimpleNamespace(config=None, quiet=False))
    sample_line = next(line for line in capsys.readouterr().out.splitlines() if "°C" in line)
    assert "+0 mV" in sample_line
    assert "3100 MHz" in sample_line
    assert "150 W" in sample_line
