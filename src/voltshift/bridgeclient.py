"""Client for the persistent voltshift_bridge daemon.

Spawns the bridge once and speaks line-delimited JSON over its pipes.
Thread-safe: one request/response exchange at a time under a lock, so the
GUI poller, the dynamic voltage engine, and user actions can share a single
daemon without interleaving.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from typing import Any, Optional

from . import paths


class BridgeError(RuntimeError):
    """Raised when the bridge reports a command failure or dies."""


class BridgeClient:
    def __init__(self, exe_path: Optional[str] = None, start_timeout: float = 15.0):
        self._exe = exe_path or paths.bridge_path()
        self._start_timeout = self._validate_timeout(start_timeout)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._next_id = 0
        self.version: Optional[str] = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        self._stop_locked(graceful=False)
        if not os.path.isfile(self._exe):
            raise BridgeError(
                f"Bridge not found: {self._exe}\n"
                "Build it first — see the README build guide."
            )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                [self._exe],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=creationflags,
            )
            ready = self._read_line(timeout=self._start_timeout)
            if ready.get("event") != "ready":
                raise BridgeError(
                    f"Bridge failed to start: {ready.get('error', 'no ready handshake')}"
                )
            self.version = ready.get("version")
        except (OSError, BridgeError) as exc:
            self._stop_locked(graceful=False)
            if isinstance(exc, BridgeError):
                raise
            raise BridgeError(f"Bridge failed to start: {exc}") from exc

    def stop(self) -> None:
        with self._lock:
            self._stop_locked(graceful=True)

    def _stop_locked(self, *, graceful: bool) -> None:
        proc, self._proc = self._proc, None
        self.version = None
        if proc is None:
            return
        try:
            if graceful and proc.poll() is None:
                proc.stdin.write(json.dumps({"id": -1, "cmd": "quit"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout=3)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        finally:
            if proc.poll() is None:
                proc.kill()
            # Reap the child before closing stdout, which may still have a
            # reader blocked on it after a timeout.
            proc.wait()
            for pipe in (proc.stdin, proc.stdout):
                try:
                    if pipe is not None:
                        pipe.close()
                except (OSError, ValueError):
                    pass

    def __enter__(self) -> "BridgeClient":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ── request/response ─────────────────────────────────────────────────────

    def call(self, cmd: str, args: Optional[dict] = None, timeout: float = 10.0) -> dict:
        """Run one command; returns the data payload or raises BridgeError."""
        timeout = self._validate_timeout(timeout)
        if not isinstance(cmd, str) or not cmd:
            raise BridgeError("Bridge command must be a non-empty string")
        if args is not None and not isinstance(args, dict):
            raise BridgeError("Bridge arguments must be an object")
        with self._lock:
            if not self.running:
                self.start()
            self._next_id += 1
            request = {"id": self._next_id, "cmd": cmd, "args": args or {}}
            try:
                encoded = json.dumps(request, allow_nan=False) + "\n"
            except (TypeError, ValueError) as exc:
                raise BridgeError(f"Invalid bridge arguments: {exc}") from exc
            try:
                self._proc.stdin.write(encoded)
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._stop_locked(graceful=False)
                raise BridgeError(f"Bridge pipe broken: {exc}") from exc

            try:
                response = self._read_line(timeout=timeout)
                if type(response.get("id")) is not int or response["id"] != self._next_id:
                    raise BridgeError("Bridge protocol desync (unexpected response id)")
                if type(response.get("ok")) is not bool:
                    raise BridgeError("Invalid bridge response (missing boolean ok)")
                if response["ok"] and not isinstance(response.get("data", {}), dict):
                    raise BridgeError("Invalid bridge response (data must be an object)")
            except BridgeError:
                # Never reuse a stream after a timeout, parse failure, or
                # mismatched response, and never retry a possibly applied write.
                self._stop_locked(graceful=False)
                raise
            if not response["ok"]:
                raise BridgeError(str(response.get("error", "unknown bridge error")))
            return response.get("data", {})

    @staticmethod
    def _validate_timeout(timeout: float) -> float:
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise BridgeError("Bridge timeout must be a finite positive number")
        return float(timeout)

    def _read_line(self, timeout: float) -> dict:
        result: dict[str, Any] = {}
        # Capture this pipe before starting the thread. A later restart must
        # never let an old reader consume the new daemon's ready event.
        stdout = self._proc.stdout

        def reader() -> None:
            try:
                line = stdout.readline()
                if not line:
                    raise BridgeError("Bridge closed its output pipe")
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise BridgeError("Invalid bridge response (expected a JSON object)")
                result["response"] = parsed
            except (OSError, ValueError, BridgeError) as exc:
                result["error"] = exc

        t = threading.Thread(target=reader, daemon=True, name="bridge-response")
        t.start()
        t.join(timeout)
        if t.is_alive():
            self._stop_locked(graceful=False)
            t.join(timeout=1)
            raise BridgeError("Bridge did not respond (timed out)")
        if "error" in result:
            raise BridgeError(f"Bridge response failed: {result['error']}") from result["error"]
        return result["response"]

    # ── convenience wrappers ─────────────────────────────────────────────────

    def info(self) -> dict:
        return self.call("info")

    def caps(self) -> dict:
        return self.call("caps")

    def metrics(self) -> dict:
        return self.call("metrics")

    def tuning_get(self) -> dict:
        return self.call("tuning.get")

    def set_voltage_offset(self, mv: int) -> dict:
        return self.call("tuning.setVoltageOffset", {"mv": mv})

    def set_core_clocks(self, min_mhz: Optional[int] = None, max_mhz: Optional[int] = None) -> dict:
        args: dict[str, int] = {}
        if min_mhz is not None:
            args["minMhz"] = min_mhz
        if max_mhz is not None:
            args["maxMhz"] = max_mhz
        return self.call("tuning.setCoreClocks", args)

    def set_vram_max(self, mhz: int) -> dict:
        return self.call("tuning.setVramMax", {"mhz": mhz})

    def set_memory_timing(self, timing: int) -> dict:
        return self.call("tuning.setMemoryTiming", {"timing": timing})

    def set_power_limit(self, pct: int) -> dict:
        return self.call("tuning.setPowerLimit", {"pct": pct})

    def set_tdc(self, amps: int) -> dict:
        return self.call("tuning.setTdc", {"amps": amps})

    def fans_get(self) -> dict:
        return self.call("tuning.getFans")

    def set_fan_curve(self, curve: list[dict]) -> dict:
        return self.call("tuning.setFanCurve", {"curve": curve})

    def set_zero_rpm(self, enabled: bool) -> dict:
        return self.call("tuning.setZeroRpm", {"enabled": enabled})

    def tuning_reset(self) -> dict:
        return self.call("tuning.reset")

    def gfx_get(self) -> dict:
        return self.call("gfx.get")

    def gfx_set(self, feature: str, **kwargs) -> dict:
        return self.call("gfx.set", {"feature": feature, **kwargs})

    def reset_shader_cache(self) -> dict:
        return self.call("gfx.resetShaderCache")

    def display_list(self) -> list[dict]:
        return self.call("display.list").get("displays", [])

    def display_get(self, index: int = 0) -> dict:
        return self.call("display.get", {"index": index})

    def display_set(self, index: int, feature: str, **kwargs) -> dict:
        return self.call("display.set", {"index": index, "feature": feature, **kwargs})

    def media_get(self) -> dict:
        return self.call("media.get")

    def media_set(self, feature: str, **kwargs) -> dict:
        return self.call("media.set", {"feature": feature, **kwargs})

    def desktop_list(self) -> list[dict]:
        return self.call("desktop.list").get("desktops", [])

    def eyefinity(self, action: str = "status") -> dict:
        return self.call("desktop.eyefinity", {"action": action})
