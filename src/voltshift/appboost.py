"""Per-app boost — apply a tuning boost while chosen games/apps are running.

A watcher thread polls the process list (psutil). While any watched
executable is alive, the boost values (power limit and optionally core max
clock) are applied; when the last one exits, the previous values are
restored. VoltShift's answer to RadeonTuner's PowerBoost, implemented
host-side instead of in the driver bridge.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import psutil

from .bridgeclient import BridgeClient, BridgeError


@dataclass
class BoostConfig:
    apps: list[str] = field(default_factory=list)  # exe names, case-insensitive
    power_limit_pct: Optional[int] = None
    max_clock_mhz: Optional[int] = None
    poll_interval_sec: float = 3.0

    def to_dict(self) -> dict:
        return {
            "apps": self.apps,
            "power_limit_pct": self.power_limit_pct,
            "max_clock_mhz": self.max_clock_mhz,
            "poll_interval_sec": self.poll_interval_sec,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BoostConfig":
        return cls(
            apps=[str(a) for a in data.get("apps", [])],
            power_limit_pct=data.get("power_limit_pct"),
            max_clock_mhz=data.get("max_clock_mhz"),
            poll_interval_sec=float(data.get("poll_interval_sec", 3.0)),
        )


def running_watched_apps(watched: list[str]) -> set[str]:
    """Which of the watched exe names currently have a live process."""
    targets = {name.lower() for name in watched if name.strip()}
    if not targets:
        return set()
    found = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info["name"] or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in targets:
            found.add(name)
    return found


class AppBoostWatcher:
    def __init__(self, bridge: BridgeClient, config: BoostConfig):
        self._bridge = bridge
        self.config = config
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._boosted = False
        self._restore_pending = False
        self._saved_power: Optional[int] = None
        self._saved_max_clock: Optional[int] = None
        self.on_log_entry: Optional[Callable[[str, str], None]] = None

    def _log(self, msg: str, level: str = "info") -> None:
        if self.on_log_entry:
            try:
                self.on_log_entry(msg, level)
            except Exception:
                pass  # An observer must never prevent hardware restoration.

    @property
    def active(self) -> bool:
        return self._restore_pending or (self._thread is not None and self._thread.is_alive())

    @property
    def boosted(self) -> bool:
        return self._boosted

    def start(self) -> None:
        with self._lock:
            if self.active:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="voltshift-appboost",
                                            daemon=True)
            self._thread.start()
        self._log(f"App boost watching: {', '.join(self.config.apps) or '(no apps)'}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.config.poll_interval_sec + 2)
            if not self._thread.is_alive():
                self._thread = None
        # Wait for an in-flight write before restoring. A queued writer checks
        # the stop flag while holding this same lock and cannot write afterward.
        with self._lock:
            if self._boosted or self._restore_pending:
                self._restore()

    def _run(self) -> None:
        while not self._stop.wait(self.config.poll_interval_sec):
            try:
                running = running_watched_apps(self.config.apps)
                if self._restore_pending:
                    self._restore()
                elif running and not self._boosted:
                    self._apply_boost(running)
                elif not running and self._boosted:
                    self._restore()
            except BridgeError as exc:
                self._log(f"App boost bridge error: {exc}", "error")

    def _apply_boost(self, running: set[str]) -> None:
        with self._lock:
            if self._stop.is_set() or self._restore_pending or self._boosted:
                return
            power, clock = self.config.power_limit_pct, self.config.max_clock_mhz
            if power is None and clock is None:
                return
            if any(value is not None and type(value) is not int for value in (power, clock)):
                raise BridgeError("App boost values must be integers")
            tuning = self._bridge.tuning_get()
            saved_power = tuning.get("power", {}).get("powerLimit") if power is not None else None
            saved_clock = tuning.get("gfx", {}).get("maxFreqMhz") if clock is not None else None
            if ((power is not None and type(saved_power) is not int)
                    or (clock is not None and type(saved_clock) is not int)):
                raise BridgeError("Cannot read a complete app boost baseline; no settings applied")
            self._saved_power, self._saved_max_clock = saved_power, saved_clock
            # Even a rejected driver call can have partially changed hardware.
            self._boosted = True
            try:
                if power is not None:
                    self._bridge.set_power_limit(power)
                if clock is not None and not self._stop.is_set():
                    self._bridge.set_core_clocks(max_mhz=clock)
                if self._stop.is_set():
                    self._restore()
                    return
            except Exception:
                self._restore()
                raise
            self._log(f"Boost ON ({', '.join(sorted(running))})", "volt")

    def _restore(self) -> bool:
        with self._lock:
            self._restore_pending = True
            for attribute, setter in (
                    ("_saved_power", self._bridge.set_power_limit),
                    ("_saved_max_clock", lambda value: self._bridge.set_core_clocks(max_mhz=value))):
                value = getattr(self, attribute)
                if value is None:
                    continue
                try:
                    setter(value)
                except Exception as exc:
                    self._log(f"Boost restore failed: {exc}", "error")
                else:
                    setattr(self, attribute, None)
            self._restore_pending = self._saved_power is not None or self._saved_max_clock is not None
            self._boosted = self._restore_pending
            if not self._restore_pending:
                self._log("Boost OFF — previous settings restored", "volt")
            return not self._restore_pending
