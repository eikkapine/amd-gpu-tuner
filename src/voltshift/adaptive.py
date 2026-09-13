"""The live governor — tuning that keeps working while you play.

Auto-tune is a session you start. This is the part that runs on its own:
notice which game is in front, load whatever was learned about it, apply that
gradually, and keep watching. When the workload settles into a steady phase
the governor may spend part of a probe budget on a small experiment — one
knob, one step, measured against the config it replaced, reverted the instant
anything looks wrong.

In-game probing is a deliberate choice, and it is the riskiest thing
VoltShift does, so it is fenced in on every side:

  * probes only fire in a phase that has been stable for a while, never
    during loading, menus, or a phase change;
  * one knob moves, by at most one capped step;
  * the change is journalled before it is written, so a hang is attributed
    correctly on the next launch;
  * any stability signal reverts immediately, marks the config unsafe
    forever, and spends the rest of the probe budget;
  * the budget is small by default — a probe every couple of minutes at most.

Set `probe_budget=0` and the governor still does everything else: it just
applies what it already knows and never experiments.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .gameproc import GameProcess, detect_game
from .optimizer.objective import DEFAULT_GOAL, score_trial
from .optimizer.space import VOLTAGE, SearchSpace
from .stability import StabilityMonitor
from .telemetry.sample import Sample
from .telemetry.window import WindowStats

# Ticks a phase must persist before the governor acts on it. At the default
# one-second tick that is five seconds of agreement, which is long enough to
# ignore a cutscene transition and short enough to react to a real change.
PHASE_CONFIRM_TICKS = 5


class _ProbeCancelled(Exception):
    """An experiment was interrupted before its result could be accepted."""


class Phase(Enum):
    IDLE = "idle"
    MENU = "menu"
    LOADING = "loading"
    LIGHT = "light"
    HEAVY = "heavy"

    @property
    def tunable(self) -> bool:
        """Phases whose measurements mean anything for tuning."""
        return self in (Phase.LIGHT, Phase.HEAVY)


def classify(sample: Sample, recent: list[Sample]) -> Phase:
    """Label the current workload from telemetry alone.

    Rules, not a learned model: the categories are few, the signals are
    obvious, and a rule you can read beats a classifier you cannot when the
    output decides whether to touch voltage.
    """
    util = sample.gpu_util_pct
    frames = sample.frames

    if util is None:
        return Phase.IDLE
    if util < 12:
        return Phase.IDLE

    # Loading screens: the GPU thrashes between busy and idle while frame
    # pacing falls apart, usually with heavy VRAM movement.
    if len(recent) >= 4:
        utils = [s.gpu_util_pct for s in recent[-6:] if s.gpu_util_pct is not None]
        if len(utils) >= 4:
            spread = max(utils) - min(utils)
            if spread > 55 and (frames is None or frames.stutter_ratio > 0.15):
                return Phase.LOADING

    if frames is not None:
        # A menu renders trivially fast: very high frame rate at low load.
        if frames.fps_avg > 180 and util < 55:
            return Phase.MENU
        if util >= 85:
            return Phase.HEAVY
        return Phase.LIGHT if util >= 25 else Phase.MENU

    if util >= 85:
        return Phase.HEAVY
    if util >= 25:
        return Phase.LIGHT
    return Phase.MENU


@dataclass
class ProbeBudget:
    """How much experimentation is allowed while playing."""

    max_probes: int = 8                 # per game session
    min_interval_sec: float = 120.0     # between probes
    settle_sec: float = 3.0
    window_sec: float = 6.0
    stop_after_failures: int = 1        # spend it all on the first real fault

    spent: int = 0
    failures: int = 0
    last_probe_t: float = 0.0

    @property
    def exhausted(self) -> bool:
        return (self.spent >= self.max_probes
                or self.failures >= self.stop_after_failures)

    def ready(self, now: float) -> bool:
        if self.exhausted:
            return False
        return now - self.last_probe_t >= self.min_interval_sec


@dataclass
class GovernorStatus:
    game: Optional[str] = None
    phase: Phase = Phase.IDLE
    applied: dict = None            # type: ignore[assignment]
    target: Optional[dict] = None
    probes_spent: int = 0
    probes_left: int = 0
    last_note: str = ""
    learned: bool = False


class AdaptiveGovernor:
    def __init__(self, hub, applier, space: SearchSpace, safeguard,
                 knowledge=None, watchdog=None,
                 stability: Optional[StabilityMonitor] = None,
                 gpu_key: str = "", goal: str = DEFAULT_GOAL,
                 budget: Optional[ProbeBudget] = None,
                 tick_sec: float = 1.0):
        self._hub = hub
        self._applier = applier
        self._space = space
        self._safeguard = safeguard
        self._knowledge = knowledge
        self._watchdog = watchdog
        self._stability = stability or StabilityMonitor()
        self._gpu_key = gpu_key
        self._goal = goal
        self._budget = budget or ProbeBudget()
        self._tick = tick_sec

        self._thread: Optional[threading.Thread] = None
        self._probe_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._probe_cancel = threading.Event()
        self._probe_faulted = False
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._probe_lock = threading.Lock()
        # Serialise writes and their journals, never measurement windows.
        self._state_lock = threading.RLock()

        self._game: Optional[GameProcess] = None
        self._phase = Phase.IDLE
        self._pending_phase: Optional[Phase] = None
        self._pending_ticks = 0
        self._phase_since = 0.0
        self._target: Optional[dict] = None
        self._desktop_config: dict = {}
        self._note = ""
        self._learned = False

        self.on_log: Optional[Callable[[str, str], None]] = None
        self.on_status: Optional[Callable[[GovernorStatus], None]] = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._probe_cancel.clear()
        self._probe_faulted = False
        self._desktop_config = self._applier.read_current()
        if self._watchdog is not None:
            self._watchdog.set_known_good(self._desktop_config)
        self._unsubscribe = self._hub.subscribe(self._stability.feed)
        self._stability.on_event = self._on_stability_event
        self._thread = threading.Thread(target=self._run, name="voltshift-governor",
                                        daemon=True)
        self._thread.start()
        self._log("adaptive governor started")

    def stop(self, restore: bool = True) -> None:
        self._stop.set()
        self._probe_cancel.set()
        current_thread = threading.current_thread()
        if self._thread and self._thread is not current_thread:
            self._thread.join()
            self._thread = None
        if self._probe_thread and self._probe_thread is not current_thread:
            self._probe_thread.join()
            self._probe_thread = None
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        self._stability.on_event = None
        if restore:
            with self._state_lock:
                self._revert(self._desktop_config, "desktop configuration restored")
        self._log("adaptive governor stopped")

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _log(self, message: str, level: str = "info") -> None:
        self._note = message
        if self.on_log:
            try:
                self.on_log(message, level)
            except Exception:
                # A closed GUI or broken observer must never interrupt recovery.
                pass

    def status(self) -> GovernorStatus:
        return GovernorStatus(
            game=self._game.exe if self._game else None,
            phase=self._phase,
            applied=self._applier.last_applied or {},
            target=self._target,
            probes_spent=self._budget.spent,
            probes_left=max(0, self._budget.max_probes - self._budget.spent),
            last_note=self._note,
            learned=self._learned,
        )

    def _emit_status(self) -> None:
        if self.on_status:
            self.on_status(self.status())

    def _on_stability_event(self, event) -> None:
        """Fired from the telemetry thread the moment something looks wrong."""
        if not event.critical:
            return
        with self._state_lock:
            self._probe_cancel.set()
            self._probe_faulted = True
            self._budget.failures += 1
            self._target = None
            config = event.config or self._applier.last_applied or {}
            try:
                self._safeguard.mark_unsafe(config, event.kind)
                if self._knowledge is not None and self._gpu_key:
                    latest = self._hub.latest
                    self._knowledge.record_failure(self._gpu_key, config.get(VOLTAGE),
                                                   latest.clock_mhz if latest else None)
            finally:
                # Persistence errors must never prevent the emergency restore.
                self._revert(self._desktop_config,
                             f"instability ({event.kind}) — reverted: {event.detail}")
                self._stability.reset()

    # ── main loop ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.wait(self._tick):
            try:
                self._step()
            except Exception as exc:
                self._log(f"governor error: {exc}", "error")

    def _step(self) -> None:
        with self._state_lock:
            if not self._stop.is_set():
                self._step_locked()

    def _step_locked(self) -> None:
        sample = self._hub.latest
        if sample is None:
            return

        self._track_game()
        self._track_phase(sample)

        if self._watchdog is not None and not self._probe_lock.locked():
            self._watchdog.verify()

        if self._target is not None:
            self._ramp_toward_target()
        elif self._phase.tunable and self._game is not None:
            self._maybe_probe(sample)

        self._emit_status()

    # ── game tracking ────────────────────────────────────────────────────────

    def _track_game(self) -> None:
        found = detect_game(self._hub.frame_source)
        previous = self._game

        if found is None:
            if previous is not None:
                self._probe_cancel.set()
                self._log(f"{previous.exe} closed — restoring desktop configuration")
                self._game = None
                self._target = dict(self._desktop_config)
                self._budget.spent = 0
                self._budget.failures = 0
                self._learned = False
                self._stability.watch_process(None)
            return

        if previous is not None and previous.exe == found.exe:
            return

        self._probe_cancel.set()
        self._game = found
        self._budget.spent = 0
        self._budget.failures = 0
        self._stability.watch_process(found.pid)
        self._load_profile(found)

    def _load_profile(self, game: GameProcess) -> None:
        """Pick a starting configuration for a game we just noticed."""
        if self._knowledge is None or not self._gpu_key:
            self._log(f"{game.exe} detected")
            return

        learned = self._knowledge.best_config(self._gpu_key, game.exe, self._goal)
        if learned:
            self._learned = True
            self._target = self._safeguard.sanitise(learned)
            self._log(f"{game.exe}: applying learned profile "
                      f"({self._space.describe(self._target)})")
            return

        # Nothing for this game. Borrow from the best result on this card for
        # the same goal — silicon behaviour transfers even when workloads do
        # not — and let the probe loop refine from there.
        known = self._knowledge.known_games(self._gpu_key)
        transfer = next((entry for entry in known if entry["goal"] == self._goal), None)
        if transfer:
            borrowed = self._knowledge.best_config(self._gpu_key, transfer["exe"],
                                                   self._goal)
            if borrowed:
                self._learned = False
                self._target = self._safeguard.sanitise(borrowed)
                self._log(f"{game.exe}: no history — starting from the profile "
                          f"learned for {transfer['exe']}")
                return

        self._learned = False
        self._log(f"{game.exe}: no history — measuring before changing anything")

    # ── phase tracking ───────────────────────────────────────────────────────

    def _track_phase(self, sample: Sample) -> None:
        recent = self._hub.history(8.0)
        phase = classify(sample, recent)

        if phase == self._phase:
            self._pending_phase = None
            self._pending_ticks = 0
            return

        if phase == self._pending_phase:
            self._pending_ticks += 1
        else:
            self._pending_phase = phase
            self._pending_ticks = 1

        if self._pending_ticks >= PHASE_CONFIRM_TICKS:
            self._probe_cancel.set()
            self._phase = phase
            self._phase_since = time.monotonic()
            self._pending_phase = None
            self._pending_ticks = 0
            if phase.tunable and sample.clock_mhz:
                self._stability.set_reference_clock(sample.clock_mhz)

    # ── gradual application ──────────────────────────────────────────────────

    def _ramp_toward_target(self) -> None:
        """Move one capped step toward the target configuration per tick.

        Stepping rather than jumping means a profile that turns out to be
        wrong for this card is discovered part-way there, at a value the card
        can still recover from.
        """
        with self._state_lock:
            if self._stop.is_set():
                return
            if self._probe_lock.locked():
                self._probe_cancel.set()
                return
            try:
                self._ramp_step()
            except Exception:
                self._target = None
                self._budget.failures += 1
                self._revert(self._desktop_config, "ramp failed — desktop restored")
                raise

    def _ramp_step(self) -> None:
        target = self._target
        if target is None:
            return
        current = self._applier.read_current()
        step = self._space.limit_step(target, current)

        verdict = self._safeguard.check(step, current,
                                        self._hub.latest.clock_mhz if self._hub.latest else None)
        if not verdict.ok:
            self._log(f"target rejected: {verdict.reason}", "warn")
            self._target = None
            return

        if step == current:
            self._target = None
            self._log(f"settled: {self._space.describe(current)}")
            return

        if self._watchdog is not None:
            self._watchdog.journal(step, "governor ramp")
        self._applier.apply(step)
        self._stability.note_change(step)

        # If this step landed on the target there is nothing left to ramp
        # toward; clearing now saves a tick and a redundant hardware read.
        if all(step.get(k.name) == target.get(k.name)
               for k in self._space.knobs if k.name in target):
            self._target = None
            self._log(f"settled: {self._space.describe(step)}")

    # ── probing ──────────────────────────────────────────────────────────────

    def _maybe_probe(self, sample: Sample) -> None:
        with self._state_lock:
            if self._stop.is_set():
                return
            self._start_probe_if_ready(sample)

    def _start_probe_if_ready(self, sample: Sample) -> None:
        now = time.monotonic()
        if not self._budget.ready(now):
            return
        # Only probe from a phase that has held for at least as long as the
        # measurement itself will take, or the phase will change mid-probe and
        # the comparison will be meaningless.
        needed = 2 * (self._budget.settle_sec + self._budget.window_sec)
        if now - self._phase_since < needed:
            return
        if not self._probe_lock.acquire(blocking=False):
            return
        self._probe_cancel.clear()
        self._probe_faulted = False
        self._probe_thread = threading.Thread(target=self._run_probe,
                                               name="voltshift-probe", daemon=True)
        try:
            self._probe_thread.start()
        except Exception:
            self._probe_lock.release()
            raise

    def _run_probe(self) -> None:
        """One paired micro-experiment, then keep it or put it back."""
        baseline = None
        changed = False
        try:
            with self._state_lock:
                self._check_probe_active()
                baseline = self._applier.read_current()
                candidate = self._propose_probe(baseline)
                if candidate is None or candidate == baseline:
                    return

                self._budget.spent += 1
                self._budget.last_probe_t = time.monotonic()
                self._log(f"probe {self._budget.spent}/{self._budget.max_probes}: "
                          f"{self._space.describe(candidate)}")
                start_phase, start_game = self._phase, self._game
                # A failed apply may still have written some of the knobs.
                changed = True
                self._apply_probe(candidate, "governor probe")
            candidate_window = self._measure()

            with self._state_lock:
                self._check_probe_context(start_phase, start_game)
                self._apply_probe(baseline, "governor probe baseline")
            baseline_window = self._measure()

            with self._state_lock:
                self._check_probe_context(start_phase, start_game)
                score = score_trial([candidate_window], [baseline_window], self._goal)
                if self._knowledge is not None and self._gpu_key and self._game:
                    self._knowledge.record_observation(
                        self._gpu_key, self._game.exe, self._goal, candidate,
                        score.value, stable=not score.unstable,
                        fps_avg=candidate_window.fps_avg, fps_p1=candidate_window.fps_p1,
                        board_w=candidate_window.board_w,
                        hotspot_c=candidate_window.hotspot_c)

                if score.unstable or score.value <= 0:
                    self._log(f"probe rejected ({score.explain()})")
                    return

                self._apply_probe(candidate, "governor probe accepted")
                self._log(f"probe accepted: {score.explain()}")
                if self._knowledge is not None and self._gpu_key and self._game:
                    self._knowledge.record_best(self._gpu_key, self._game.exe,
                                                self._goal, candidate, score.value)
        except _ProbeCancelled:
            with self._state_lock:
                # The stability callback owns the emergency desktop restore.
                # Never overwrite it with the experiment's previous profile.
                if changed and not self._probe_faulted:
                    self._revert(baseline, "probe abandoned — interrupted")
        except Exception as exc:
            self._log(f"probe failed: {exc}", "error")
            with self._state_lock:
                self._budget.failures += 1
                self._target = None
                if changed and not self._probe_faulted:
                    try:
                        self._revert(baseline, "probe failed — restored previous configuration")
                    except Exception as recovery_exc:
                        self._log(f"probe recovery failed: {recovery_exc}", "error")
        finally:
            try:
                self._probe_lock.release()
            except RuntimeError:
                pass

    def _check_probe_active(self) -> None:
        if self._stop.is_set() or self._probe_cancel.is_set():
            raise _ProbeCancelled()

    def _check_probe_context(self, phase: Phase, game: Optional[GameProcess]) -> None:
        self._check_probe_active()
        if self._phase != phase or self._game != game:
            raise _ProbeCancelled()

    def _apply_probe(self, config: dict, reason: str) -> None:
        self._check_probe_active()
        if self._watchdog is not None:
            self._watchdog.journal(config, reason)
        self._applier.apply(config, skip_unchanged=False)
        self._stability.note_change(config)

    def _propose_probe(self, current: dict) -> Optional[dict]:
        """A single small step, biased toward the knob with room to move.

        Deliberately not a full optimiser call: during gameplay the useful
        move is one conservative step from something already known to work,
        not a jump to wherever a model finds interesting.
        """
        candidates = []
        for knob in self._space.knobs:
            have = current.get(knob.name)
            if have is None:
                continue
            step = max(knob.step, (knob.max_delta or knob.step) // 2)
            for direction in (-1, 1):
                proposal = dict(current)
                proposal[knob.name] = knob.clamp(have + direction * step)
                if proposal[knob.name] == have:
                    continue
                candidates.append(proposal)

        latest = self._hub.latest
        clock = latest.clock_mhz if latest else None
        allowed = [c for c in candidates
                   if self._safeguard.check(c, current, clock).ok]
        if not allowed:
            return None

        # Prefer whichever direction the accumulated knowledge likes, falling
        # back to undervolting, which is the move that usually pays.
        undervolts = [c for c in allowed
                      if VOLTAGE in c and c[VOLTAGE] < current.get(VOLTAGE, 0)]
        pool = undervolts or allowed
        return pool[self._budget.spent % len(pool)]

    def _measure(self) -> WindowStats:
        for duration in (self._budget.settle_sec, self._budget.window_sec):
            if self._probe_cancel.wait(duration):
                raise _ProbeCancelled()
            self._check_probe_active()
        return WindowStats.from_samples(self._hub.history(self._budget.window_sec))

    def _revert(self, baseline: dict, reason: str) -> None:
        try:
            if baseline:
                self._applier.apply(baseline, skip_unchanged=False)
            else:
                self._applier.reset()
        except Exception:
            self._applier.reset()
        # Preserve the pending journal when both restoration paths fail.
        if self._watchdog is not None:
            self._watchdog.abandon()
        self._log(reason, "warn")
