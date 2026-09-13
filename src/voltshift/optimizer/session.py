"""The auto-tune session — one click, then a closed loop.

Each trial is a paired comparison. The candidate configuration runs for a
measurement window, then the baseline runs for an identical window, and that
alternation repeats. Scoring the *differences* between paired windows removes
the drift that makes naive before/after benchmarking of a live game
worthless: if the player walks into a heavier area halfway through, both
members of the pair get heavier together.

Every candidate write is journalled first, so a configuration that hangs the
machine is identified on the next launch rather than being silently retried.
Every stability signal aborts the trial, reverts immediately, and teaches both
the session's tabu set and the card's permanent stability frontier.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from ..stability import StabilityEvent, StabilityMonitor
from ..telemetry.window import WindowStats
from .objective import DEFAULT_GOAL, Score, score_trial
from .space import VOLTAGE, SearchSpace


class SessionState(Enum):
    IDLE = "idle"
    BASELINE = "measuring baseline"
    EXPLORING = "exploring"
    CONFIRMING = "confirming best"
    APPLYING = "applying result"
    DONE = "done"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass
class TrialResult:
    index: int
    config: dict
    score: Score
    candidate_windows: list[WindowStats] = field(default_factory=list)
    baseline_windows: list[WindowStats] = field(default_factory=list)
    events: list[StabilityEvent] = field(default_factory=list)

    @property
    def stable(self) -> bool:
        return not self.score.unstable

    @property
    def value(self) -> float:
        return self.score.value


@dataclass
class SessionConfig:
    goal: str = DEFAULT_GOAL
    trials: int = 14
    pairs_per_trial: int = 2
    window_sec: float = 8.0
    settle_sec: float = 3.0
    confirm_pairs: int = 3
    min_samples_per_window: int = 4


@dataclass
class SessionReport:
    state: SessionState
    baseline: dict
    best_config: Optional[dict]
    best_score: Optional[Score]
    trials: list[TrialResult]
    message: str = ""

    @property
    def improved(self) -> bool:
        return (self.best_score is not None and self.best_score.value > 0
                and not self.best_score.unstable)


class AutoTuneSession:
    def __init__(self, hub, applier, space: SearchSpace, safeguard, optimizer,
                 config: Optional[SessionConfig] = None,
                 knowledge=None, watchdog=None,
                 stability: Optional[StabilityMonitor] = None,
                 gpu_key: str = "", exe: str = "desktop"):
        self._hub = hub
        self._applier = applier
        self._space = space
        self._safeguard = safeguard
        self._optimizer = optimizer
        self._config = config or SessionConfig()
        self._knowledge = knowledge
        self._watchdog = watchdog
        self._stability = stability or StabilityMonitor()
        self._gpu_key = gpu_key
        self._exe = exe

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._critical_seen = 0

        self.state = SessionState.IDLE
        self.baseline: dict = {}
        self.trials: list[TrialResult] = []
        self.report: Optional[SessionReport] = None

        # Callbacks, invoked on the session thread.
        self.on_state: Optional[Callable[[SessionState], None]] = None
        self.on_trial: Optional[Callable[[TrialResult], None]] = None
        self.on_progress: Optional[Callable[[str, float], None]] = None
        self.on_log: Optional[Callable[[str, str], None]] = None
        self.on_done: Optional[Callable[[SessionReport], None]] = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.trials = []
        self.report = None
        self._critical_seen = 0
        self._thread = threading.Thread(target=self._run, name="voltshift-autotune",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Abort and restore the baseline configuration."""
        self._stop.set()
        if self._thread:
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=self._config.window_sec + 10)
            if not self._thread.is_alive():
                self._thread = None

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _log(self, message: str, level: str = "info") -> None:
        if self.on_log:
            self.on_log(message, level)

    def _set_state(self, state: SessionState) -> None:
        self.state = state
        if self.on_state:
            self.on_state(state)

    def _progress(self, label: str, fraction: float) -> None:
        if self.on_progress:
            self.on_progress(label, max(0.0, min(1.0, fraction)))

    def _sleep(self, seconds: float) -> bool:
        """Wait, returning False if the session was asked to stop."""
        return not self._stop.wait(seconds)

    def _collect_window(self) -> tuple[WindowStats, list[StabilityEvent]]:
        """Settle, then measure for one window, watching for instability."""
        self._stability.note_change(self._applier.last_applied)
        if not self._sleep(self._config.settle_sec):
            return WindowStats.from_samples([]), []

        if not self._sleep(self._config.window_sec):
            return WindowStats.from_samples([]), self._stability.critical_events

        # The hub already bounds history by age, so asking it for the window
        # length is the whole filter. Re-filtering against a timestamp taken
        # before the sleep would depend on the platform clock's granularity
        # being finer than the window, which is not guaranteed.
        samples = self._hub.history(self._config.window_sec)
        events = self._stability.critical_events
        fresh = events[self._critical_seen:]
        self._critical_seen = len(events)
        return WindowStats.from_samples(samples), fresh

    def _apply(self, config: dict, journal_reason: Optional[str] = None) -> None:
        if self._watchdog is not None and journal_reason:
            self._watchdog.journal(config, journal_reason)
        self._applier.apply(config)
        if self._hub is not None and VOLTAGE in config:
            self._hub.set_applied_offset(config[VOLTAGE])

    def _revert_to_baseline(self) -> None:
        try:
            self._applier.apply(self.baseline, skip_unchanged=False)
        except Exception as exc:
            self._log(f"revert failed, forcing factory reset: {exc}", "error")
            self._applier.reset()
        if self._watchdog is not None:
            self._watchdog.abandon()

    # ── main loop ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._unsubscribe = self._hub.subscribe(self._stability.feed)
            self._execute()
        except Exception as exc:  # a crashed tuner must still restore the GPU
            self._log(f"auto-tune failed: {exc}", "error")
            try:
                self._revert_to_baseline()
            except Exception as recovery_exc:
                self._log(f"recovery failed; journal retained: {recovery_exc}", "error")
                exc = RuntimeError(f"{exc}; recovery failed: {recovery_exc}")
            self._set_state(SessionState.FAILED)
            self.report = SessionReport(SessionState.FAILED, self.baseline, None,
                                        None, self.trials, str(exc))
            if self.on_done:
                self.on_done(self.report)
        finally:
            if self._unsubscribe:
                self._unsubscribe()
                self._unsubscribe = None
            self._stability.reset()

    def _execute(self) -> None:
        self.baseline = self._applier.read_current()
        if any(name not in self.baseline for name in self._space.names):
            return self._finish(SessionState.FAILED,
                                "cannot read a complete baseline; no tuning applied")
        self._log(f"baseline: {self._space.describe(self.baseline)}")

        # Baseline is safe by definition — it is what the machine is already
        # running — so it becomes the rollback target for the whole session.
        if self._watchdog is not None:
            self._watchdog.set_known_good(self.baseline)

        self._set_state(SessionState.BASELINE)
        self._progress("measuring baseline", 0.0)
        baseline_window, baseline_events = self._collect_window()
        if self._stop.is_set():
            return self._finish(SessionState.ABORTED, "stopped before any trial ran")
        if not baseline_window.is_usable(self._config.min_samples_per_window):
            return self._finish(SessionState.FAILED, "no telemetry — is the bridge alive?")
        if baseline_events:
            return self._finish(SessionState.FAILED,
                                "baseline is unstable; no tuning applied")

        self._stability.set_reference_clock(baseline_window.clock_mhz)
        if baseline_window.has_frames:
            self._log(f"frame data present — {baseline_window.fps_avg:.0f} fps baseline")
        else:
            self._log("no frame source: tuning on power, clocks and thermals only", "warn")

        self._seed_from_knowledge()

        self._set_state(SessionState.EXPLORING)
        for index in range(self._config.trials):
            if self._stop.is_set():
                break
            self._progress(f"trial {index + 1} of {self._config.trials}",
                           index / max(1, self._config.trials))
            result = self._run_trial(index)
            if result is None:
                break
            self.trials.append(result)
            if self.on_trial:
                self.on_trial(result)

        self._revert_to_baseline()
        if self._stop.is_set():
            return self._finish(SessionState.ABORTED, "stopped")

        self._commit_best()

    def _seed_from_knowledge(self) -> None:
        if self._knowledge is None or not self._gpu_key:
            return
        from .gp import Observation

        priors = self._knowledge.prior_observations(self._gpu_key, self._exe,
                                                    self._config.goal)
        if not priors:
            return
        self._optimizer.seed_prior([
            Observation(p.config, p.score, p.weight) for p in priors])
        own = sum(1 for p in priors if p.exe == self._exe)
        self._log(f"warm start: {len(priors)} prior observations "
                  f"({own} from this game, {len(priors) - own} transferred)")

    def _run_trial(self, index: int) -> Optional[TrialResult]:
        current = self._applier.read_current()
        latest = self._hub.latest
        clock = latest.clock_mhz if latest else None

        candidate = self._optimizer.suggest(
            reject=self._safeguard.rejects(current=current, clock_mhz=clock))
        verdict = self._safeguard.check(candidate, current, clock)
        if not verdict.ok:
            candidate = self._safeguard.sanitise(candidate, current)
            if not self._safeguard.check(candidate, current, clock).ok:
                self._log(f"trial {index + 1} skipped: {verdict.reason}", "warn")
                return None

        self._log(f"trial {index + 1}: {self._space.describe(candidate)}")

        candidate_windows: list[WindowStats] = []
        baseline_windows: list[WindowStats] = []
        events: list[StabilityEvent] = []
        unstable_reason: Optional[str] = None

        for pair in range(self._config.pairs_per_trial):
            if self._stop.is_set():
                break

            self._apply(candidate, f"auto-tune trial {index + 1}")
            window, fired = self._collect_window()
            if fired:
                events.extend(fired)
                unstable_reason = str(fired[0])
                self._log(f"instability: {unstable_reason}", "error")
                break
            candidate_window = window

            self._apply(self.baseline)
            window, fired = self._collect_window()
            if fired:
                self._revert_to_baseline()
                raise RuntimeError(f"baseline became unstable: {fired[0]}")
            if (candidate_window.is_usable(self._config.min_samples_per_window)
                    and window.is_usable(self._config.min_samples_per_window)):
                candidate_windows.append(candidate_window)
                baseline_windows.append(window)

        self._revert_to_baseline()

        score = score_trial(candidate_windows, baseline_windows,
                            self._config.goal, unstable_reason)

        if unstable_reason:
            self._safeguard.mark_unsafe(candidate, events[0].kind if events else "unknown")
            if self._knowledge is not None and self._gpu_key:
                self._knowledge.record_failure(self._gpu_key, candidate.get(VOLTAGE),
                                               self._hub.latest.clock_mhz
                                               if self._hub.latest else None)

        self._optimizer.observe(candidate, score.value)
        self._record(candidate, score, candidate_windows)
        self._log(f"trial {index + 1}: {score.value:+.3f}  {score.explain()}")

        return TrialResult(index, candidate, score, candidate_windows,
                           baseline_windows, events)

    def _record(self, config: dict, score: Score,
                windows: list[WindowStats]) -> None:
        if self._knowledge is None or not self._gpu_key:
            return
        summary = WindowStats.from_samples([]) if not windows else windows[0]
        self._knowledge.record_observation(
            self._gpu_key, self._exe, self._config.goal, config, score.value,
            stable=not score.unstable,
            fps_avg=summary.fps_avg, fps_p1=summary.fps_p1,
            board_w=summary.board_w, hotspot_c=summary.hotspot_c)

    # ── commit ───────────────────────────────────────────────────────────────

    def _commit_best(self) -> None:
        if self._stop.is_set():
            self._revert_to_baseline()
            return self._finish(SessionState.ABORTED, "stopped; baseline restored")
        stable = [t for t in self.trials if t.stable and t.value > 0]
        if not stable:
            return self._finish(SessionState.DONE,
                                "no configuration beat the baseline — "
                                "the card is already well tuned for this workload")

        best = max(stable, key=lambda t: t.value)

        # Re-measure the winner before committing. Bayesian optimisation
        # naturally over-selects whatever got lucky, so the best score of a
        # search is an optimistic estimate; confirming it costs one more
        # measurement and stops a fluke from being written to disk.
        self._set_state(SessionState.CONFIRMING)
        self._progress("confirming best result", 0.9)
        confirm_candidate: list[WindowStats] = []
        confirm_baseline: list[WindowStats] = []
        unstable_reason = None

        for _ in range(self._config.confirm_pairs):
            if self._stop.is_set():
                break
            self._apply(best.config, "auto-tune confirmation")
            window, fired = self._collect_window()
            if fired:
                unstable_reason = str(fired[0])
                break
            candidate_window = window
            self._apply(self.baseline)
            window, fired = self._collect_window()
            if fired:
                self._revert_to_baseline()
                raise RuntimeError(f"baseline became unstable: {fired[0]}")
            if (candidate_window.is_usable(self._config.min_samples_per_window)
                    and window.is_usable(self._config.min_samples_per_window)):
                confirm_candidate.append(candidate_window)
                confirm_baseline.append(window)

        if self._stop.is_set():
            self._revert_to_baseline()
            return self._finish(SessionState.ABORTED, "stopped; baseline restored")
        if not unstable_reason and len(confirm_candidate) < self._config.confirm_pairs:
            self._revert_to_baseline()
            return self._finish(SessionState.DONE,
                                "confirmation telemetry was incomplete; baseline restored")

        confirmed = score_trial(confirm_candidate, confirm_baseline,
                                self._config.goal, unstable_reason)

        if confirmed.unstable or confirmed.value <= 0:
            self._revert_to_baseline()
            if confirmed.unstable:
                self._safeguard.mark_unsafe(best.config, "confirmation")
            return self._finish(SessionState.DONE,
                                "the best candidate did not hold up on re-test; "
                                "baseline restored")

        self._set_state(SessionState.APPLYING)
        if self._stop.is_set():
            self._revert_to_baseline()
            return self._finish(SessionState.ABORTED, "stopped; baseline restored")
        self._apply(best.config, "auto-tune result")
        if self._stop.is_set():
            self._revert_to_baseline()
            return self._finish(SessionState.ABORTED, "stopped; baseline restored")
        if self._watchdog is not None:
            self._watchdog.verify(force=True)
        if self._knowledge is not None and self._gpu_key:
            self._knowledge.record_best(self._gpu_key, self._exe,
                                        self._config.goal, best.config,
                                        confirmed.value)

        self.report = SessionReport(SessionState.DONE, self.baseline, best.config,
                                    confirmed, self.trials,
                                    f"applied: {confirmed.explain()}")
        self._set_state(SessionState.DONE)
        self._progress("done", 1.0)
        self._log(f"auto-tune complete — {self._space.describe(best.config)} "
                  f"({confirmed.explain()})")
        if self.on_done:
            self.on_done(self.report)

    def _finish(self, state: SessionState, message: str) -> None:
        self.report = SessionReport(state, self.baseline, None, None,
                                    self.trials, message)
        self._set_state(state)
        self._progress(message, 1.0)
        self._log(message)
        if self.on_done:
            self.on_done(self.report)
