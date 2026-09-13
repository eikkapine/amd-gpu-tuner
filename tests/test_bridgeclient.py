"""Exercise the pipe protocol against real child processes, without ADLX."""

from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import threading

import pytest

from voltshift.bridgeclient import BridgeClient, BridgeError


DAEMON = r'''
import json, sys, time
mode = sys.argv[1]
if mode == "startup_timeout":
    time.sleep(60)
elif mode == "startup_error":
    print(json.dumps({"ok": False, "error": "ADLX unavailable"}), flush=True)
elif mode == "startup_invalid":
    print("[]", flush=True)
else:
    print(json.dumps({"event": "ready", "version": "test"}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    cmd = request["cmd"]
    if cmd == "quit":
        break
    if cmd == "timeout":
        time.sleep(60)
    if cmd == "eof":
        break
    if cmd == "malformed":
        print("not JSON", flush=True)
        continue
    if cmd == "array":
        print("[]", flush=True)
        continue
    if cmd == "empty":
        print("{}", flush=True)
        continue
    response = {"id": request["id"], "ok": True, "data": request["args"]}
    if cmd == "wrong_id":
        response["id"] += 1
    if cmd == "bool_id":
        response["id"] = True
    if cmd == "bad_ok":
        response["ok"] = "yes"
    if cmd == "bad_data":
        response["data"] = []
    if cmd == "error":
        response = {"id": request["id"], "ok": False, "error": "unsupported"}
    print(json.dumps(response), flush=True)
'''


@pytest.fixture
def bridge_factory(tmp_path, monkeypatch):
    daemon = tmp_path / "daemon.py"
    daemon.write_text(DAEMON, encoding="utf-8")
    real_popen = subprocess.Popen
    processes = []
    clients = []
    mode = "normal"

    def spawn(_argv, **kwargs):
        proc = real_popen([sys.executable, "-u", str(daemon), mode], **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr("voltshift.bridgeclient.subprocess.Popen", spawn)

    def create(startup="normal", timeout=2):
        nonlocal mode
        mode = startup
        client = BridgeClient(sys.executable, start_timeout=timeout)
        clients.append(client)
        return client, processes

    yield create
    for client in clients:
        client.stop()
    for proc in processes:
        assert proc.poll() is not None, "bridge child leaked"
        assert proc.stdin.closed and proc.stdout.closed, "bridge pipes leaked"


def test_concurrent_start_and_requests_share_one_process(bridge_factory):
    client, processes = bridge_factory()

    def echo(value):
        client.start()
        return client.call("echo", {"value": value})["value"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(echo, range(24))) == list(range(24))
    assert len(processes) == 1
    assert client.version == "test"
    client.stop()
    assert client.version is None
    assert not client.running


def test_command_failure_does_not_discard_healthy_daemon(bridge_factory):
    client, processes = bridge_factory()
    with pytest.raises(BridgeError, match="unsupported"):
        client.call("error")
    assert client.call("echo", {"healthy": True}) == {"healthy": True}
    assert len(processes) == 1


@pytest.mark.parametrize("cmd", ["malformed", "array", "empty", "wrong_id", "bool_id", "bad_ok", "bad_data", "eof"])
def test_bad_response_reaps_child_and_restart_is_clean(bridge_factory, cmd):
    client, processes = bridge_factory()
    with pytest.raises(BridgeError):
        client.call(cmd)
    assert not client.running
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed
    assert client.call("echo", {"restarted": True}) == {"restarted": True}
    assert len(processes) == 2


def test_timeout_reaps_process_without_retrying(bridge_factory):
    client, processes = bridge_factory()
    with pytest.raises(BridgeError, match="timed out"):
        client.call("timeout", timeout=0.05)
    assert len(processes) == 1
    assert not client.running
    assert not any(t.name == "bridge-response" for t in threading.enumerate())
    assert client.call("echo") == {}


@pytest.mark.parametrize("mode, message", [
    ("startup_timeout", "timed out"),
    ("startup_error", "ADLX unavailable"),
    ("startup_invalid", "JSON object"),
])
def test_failed_start_reaps_child(bridge_factory, mode, message):
    client, processes = bridge_factory(mode, timeout=0.2 if mode == "startup_timeout" else 2)
    with pytest.raises(BridgeError, match=message):
        client.start()
    assert not client.running
    assert client.version is None
    assert processes[0].poll() is not None


def test_broken_stdin_reaps_child(bridge_factory):
    client, processes = bridge_factory()
    client.start()
    processes[0].stdin.close()
    with pytest.raises(BridgeError, match="pipe broken"):
        client.call("echo")
    assert not client.running


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), "1"])
def test_invalid_timeout_does_not_spawn(bridge_factory, timeout):
    client, processes = bridge_factory()
    with pytest.raises(BridgeError, match="finite positive"):
        client.call("echo", timeout=timeout)
    assert not processes
    with pytest.raises(BridgeError, match="finite positive"):
        BridgeClient(start_timeout=timeout)


def test_invalid_requests_leave_protocol_usable(bridge_factory):
    client, processes = bridge_factory()
    for cmd, args in [("", {}), ("echo", []), ("echo", {"number": float("nan")}), ("echo", {"object": object()})]:
        with pytest.raises(BridgeError):
            client.call(cmd, args)
    assert client.call("echo", {"valid": True}) == {"valid": True}
    assert len(processes) == 1


def test_spawn_os_error_is_a_bridge_error(monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("voltshift.bridgeclient.subprocess.Popen", fail)
    client = BridgeClient(sys.executable)
    with pytest.raises(BridgeError, match="failed to start.*denied"):
        client.start()
    assert not client.running


def test_missing_executable_has_build_guidance(tmp_path):
    client = BridgeClient(str(tmp_path / "missing.exe"))
    with pytest.raises(BridgeError, match="Build it first"):
        client.start()
