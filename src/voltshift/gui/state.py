"""Shared application state for the GUI.

Owns the single BridgeClient, the discovered capabilities, the dynamic
voltage engine runner, the crash logger, and the app-boost watcher. Pages
read and mutate through here so there is one source of truth and one daemon.

Thread marshalling: the engine/crash/boost callbacks fire on worker threads;
`post` queues them for a main-thread drain so workers never block on Tk.
"""

from __future__ import annotations

import json
import os
import queue
from typing import Callable, Optional

from .. import autostack, paths
from ..appboost import AppBoostWatcher, BoostConfig
from ..bridgeclient import BridgeClient, BridgeError
from ..crashlog import CrashLogger
from ..engine import EngineConfig
from ..optimizer.objective import DEFAULT_GOAL
from ..runner import EngineRunner


class AppState:
    def __init__(self, tk_root):
        self._root = tk_root
        self._closing = False
        self._post_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._post_job = self._root.after(25, self._drain_posts)
        self.bridge = BridgeClient()
        self.info: dict = {}
        self.caps: dict = {}
        self.connected = False
        self.connect_error: Optional[str] = None

        self.engine_config = EngineConfig()
        self.boost_config = BoostConfig()
        self.runner: Optional[EngineRunner] = None
        self.crash_logger: Optional[CrashLogger] = None
        self.appboost: Optional[AppBoostWatcher] = None

        # Closed-loop stack (telemetry, optimiser, safety, memory). Built on
        # connect so pages can assume it exists whenever `connected` is True.
        self.stack: Optional[autostack.AutoStack] = None
        self.autotune = None
        self.governor = None
        self.goal = DEFAULT_GOAL
        self.recovery_notice: Optional[str] = None
        self._recovery_checked = False
        self.always_on_top = False
        # Frame-rate aware tuning installs itself on first run. Persisted so a
        # user who turns it off is not asked again by a later launch.
        self.auto_fetch_frames = True

        # Subscribers (all invoked on the Tk main thread).
        self.log_sinks: list[Callable[[str, str], None]] = []
        self.sample_sinks: list[Callable[[dict], None]] = []

        self._load_settings()

    # ── thread marshalling ───────────────────────────────────────────────────

    def post(self, fn: Callable, *args) -> None:
        """Queue work without calling Tk from a worker thread.

        Tk's after() can block a worker until the UI handles its request.
        That deadlocks when the UI is joining that worker during shutdown.
        Only the main-thread drain below calls Tk.
        """
        if self._closing:
            return
        self._post_queue.put((fn, args))

    def _drain_posts(self) -> None:
        self._post_job = None
        if self._closing:
            return
        try:
            # Bound each batch so sustained telemetry cannot starve UI input.
            for _ in range(100):
                try:
                    fn, args = self._post_queue.get_nowait()
                except queue.Empty:
                    break
                fn(*args)
        finally:
            if not self._closing:
                self._post_job = self._root.after(25, self._drain_posts)

    def log(self, msg: str, level: str = "info") -> None:
        def deliver() -> None:
            for sink in self.log_sinks:
                sink(msg, level)
        self.post(deliver)

    # ── connection ───────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self.bridge.start()
            self.info = self.bridge.info()
            self.caps = self.bridge.caps()
            self.connected = True
            self.connect_error = None
            self.crash_logger = CrashLogger(self.engine_config.to_dict())
            self.crash_logger.on_log_entry = self.log
            self.crash_logger.check_previous_session()
            self._build_stack()
            return True
        except BridgeError as exc:
            self.connected = False
            self.connect_error = str(exc)
            return False

    def _build_stack(self) -> None:
        """Wire telemetry, optimiser and safety, then start the one poller.

        Opening the app is passive. Recovery and control verification run
        only when the user explicitly starts an automatic tuning controller.
        """
        try:
            self.stack = autostack.build(self.bridge, on_log=self.log,
                                         auto_fetch_frames=self.auto_fetch_frames)
            self.stack.hub.subscribe(self._dispatch_hub_sample)
            self.stack.hub.start()
            self.log(f"telemetry — {self.stack.frame_source_status}")
        except Exception as exc:
            self.stack = None
            self.log(f"closed-loop features unavailable: {exc}", "warn")

    def _dispatch_hub_sample(self, sample) -> None:
        self._dispatch_sample(sample.as_dict())

    def gpu_name(self) -> str:
        return self.info.get("name", "No GPU")

    @property
    def has_stack(self) -> bool:
        return self.stack is not None and self.stack.tunable

    # ── dynamic voltage engine ───────────────────────────────────────────────

    def start_engine(self) -> None:
        if self.runner and self.runner.running:
            return
        if self.crash_logger:
            self.crash_logger.update_config(self.engine_config.to_dict())
            self.crash_logger.start()
            self.crash_logger.write_session_header(self.gpu_name(),
                                                   self.engine_config.to_dict())
        # Share the telemetry hub when it exists so the bridge is polled once
        # for the whole application rather than once per feature.
        hub = self.stack.hub if self.stack is not None else None
        self.runner = EngineRunner(self.bridge, self.engine_config,
                                   self.crash_logger, hub=hub)
        self.runner.on_log_entry = lambda m, lvl: self.log(m, lvl)
        # With a hub the samples already reach subscribers; without one the
        # runner is the only poller and must feed them itself.
        if hub is None:
            self.runner.on_sample = self._dispatch_sample
        self.runner.on_error = lambda m: self.log(m, "error")
        self.runner.start()

    def stop_engine(self) -> None:
        if self.runner:
            self.runner.stop(reset_gpu=True)
        if self.crash_logger:
            self.crash_logger.stop()
            self.crash_logger.write_session_footer(self.crash_logger.crash_count)

    @property
    def engine_running(self) -> bool:
        return self.runner is not None and self.runner.running

    def _dispatch_sample(self, sample: dict) -> None:
        def deliver() -> None:
            for sink in self.sample_sinks:
                sink(sample)
        self.post(deliver)

    # ── app boost ────────────────────────────────────────────────────────────

    def start_appboost(self) -> None:
        if self.appboost and self.appboost.active:
            return
        self.appboost = AppBoostWatcher(self.bridge, self.boost_config)
        self.appboost.on_log_entry = self.log
        self.appboost.start()

    def stop_appboost(self) -> None:
        if self.appboost:
            self.appboost.stop()

    @property
    def appboost_active(self) -> bool:
        return self.appboost is not None and self.appboost.active

    # ── settings persistence ─────────────────────────────────────────────────

    def _load_settings(self) -> None:
        try:
            with open(paths.config_path(), encoding="utf-8") as f:
                data = json.load(f)
            if "engine" in data:
                self.engine_config = EngineConfig.from_dict(data["engine"])
            if "boost" in data:
                self.boost_config = BoostConfig.from_dict(data["boost"])
            telemetry = data.get("telemetry", {})
            self.auto_fetch_frames = bool(
                telemetry.get("auto_fetch_presentmon", True))
            self.goal = data.get("goal", self.goal)
            self.always_on_top = bool(data.get("window", {}).get("always_on_top", False))
        except (OSError, ValueError):
            pass

    def save_settings(self) -> None:
        data = {"engine": self.engine_config.to_dict(),
                "boost": self.boost_config.to_dict(),
                "goal": self.goal,
                "window": {"always_on_top": self.always_on_top},
                "telemetry": {"auto_fetch_presentmon": self.auto_fetch_frames}}
        tmp = paths.config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, paths.config_path())

    # ── closed loop ──────────────────────────────────────────────────────────

    def verify_controls(self, force: bool = False) -> bool:
        """Narrow the search space to controls this card actually honours.

        Runs once per card and caches the result, because it writes to the
        GPU. Returns False when nothing the driver advertises turns out to
        respond, which is the one case where tuning cannot proceed.
        """
        if self.stack is None:
            return False
        if not self._recovery_checked:
            try:
                self.recovery_notice = autostack.recover_previous_session(self.stack)
                self._recovery_checked = True
                if self.recovery_notice:
                    self.log(self.recovery_notice, "warn")
            except Exception as exc:
                self.log(f"recovery failed; automatic tuning was not started: {exc}", "error")
                return False
        if self.stack.verified and not force:
            return self.stack.tunable
        try:
            self.stack.verify_controls(force=force, on_log=self.log)
        except Exception as exc:
            self.log(f"control verification failed: {exc}", "error")
            return False
        if not self.stack.tunable:
            self.log("none of this GPU's advertised tuning controls respond "
                     "to writes", "error")
            return False
        self.log(f"tuning controls in use: {', '.join(self.stack.space.names)}")
        return True

    def start_autotune(self, session_config, exe: str = "desktop"):
        """Begin an auto-tune session; returns it, or None if unavailable."""
        from ..optimizer.session import AutoTuneSession

        if not self.has_stack:
            self.log("no tunable controls on this GPU", "error")
            return None
        if self.autotune is not None and self.autotune.running:
            return self.autotune
        if not self.verify_controls():
            return None

        stack = self.stack
        self.autotune = AutoTuneSession(
            stack.hub, stack.applier, stack.space, stack.safeguard,
            stack.new_optimizer(), session_config,
            knowledge=stack.knowledge, watchdog=stack.watchdog,
            stability=stack.stability, gpu_key=stack.gpu_key, exe=exe)
        self.autotune.on_log = self.log
        self.autotune.start()
        return self.autotune

    def stop_autotune(self) -> None:
        if self.autotune is not None:
            self.autotune.stop()

    @property
    def autotune_running(self) -> bool:
        return self.autotune is not None and self.autotune.running

    def start_governor(self, budget=None):
        from ..adaptive import AdaptiveGovernor

        if not self.has_stack:
            self.log("no tunable controls on this GPU", "error")
            return None
        if self.governor is not None and self.governor.running:
            return self.governor
        if not self.verify_controls():
            return None

        stack = self.stack
        self.governor = AdaptiveGovernor(
            stack.hub, stack.applier, stack.space, stack.safeguard,
            knowledge=stack.knowledge, watchdog=stack.watchdog,
            stability=stack.stability, gpu_key=stack.gpu_key,
            goal=self.goal, budget=budget)
        self.governor.on_log = self.log
        self.governor.start()
        return self.governor

    def stop_governor(self) -> None:
        if self.governor is not None:
            self.governor.stop(restore=True)

    @property
    def governor_running(self) -> bool:
        return self.governor is not None and self.governor.running

    # ── teardown ─────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        self._closing = True
        if self._post_job is not None:
            try:
                self._root.after_cancel(self._post_job)
            except Exception:
                pass  # the window may already be destroyed; still stop workers
            self._post_job = None
        while not self._post_queue.empty():
            self._post_queue.get_nowait()
        # Order matters: stop anything that writes to the GPU before the
        # bridge goes away, so every restore path can still reach the driver.
        for stop in (self.stop_autotune, self.stop_governor,
                     self.stop_engine, self.stop_appboost):
            try:
                stop()
            except Exception:
                pass
        if self.stack is not None:
            try:
                self.stack.close()
            except Exception:
                pass
        self.bridge.stop()
