"""Tuning against a benchmark score.

Gameplay mode measures short interleaved windows and compares paired
differences, because a game's load drifts and there is no other way to tell a
real gain from the scenery changing. Inside a benchmark that method breaks:
consecutive windows land in different scenes, the paired difference measures
the scene rather than the setting, and every result is correctly thrown away
as noise. That is why gameplay mode reports "already well tuned" on Time Spy.

A benchmark offers something better. The unit of measurement becomes one
whole run, and the objective becomes the score itself — a single precise
number over a fixed workload.

Measured on this machine's own 3DMark history, back-to-back Time Spy runs
differ by 0.18% at the median and 1.17% at the worst, with several runs
scoring identically. That is quiet enough for one run per trial to resolve a
half-percent gain, so trials are not wasted repeating themselves.

Two consequences for how this differs from gameplay mode:

  * There is no significance shrinkage. A score is a measurement, not an
    estimate from a drifting window, so the optimiser maximises it directly
    and only the final winner has to clear a margin.
  * The first few trials are not random. Maximum score has well-understood
    directions — power limit first, then undervolt to buy boost headroom
    inside that power budget, then clock and memory offsets — so the search
    starts from those instead of sampling the corner of the space where the
    card is downclocked and power limited.
"""

from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..benchmark import BenchmarkResult, ResultWatcher, detect_running_benchmark
from ..stability import StabilityMonitor
from .session import SessionState
from .space import MAX_CLOCK, POWER_LIMIT, VOLTAGE, VRAM_CLOCK, SearchSpace

# Gain below this is treated as run-to-run noise rather than a result.
# Set from the measured 0.18% median / 1.17% worst-case spread.
DEFAULT_MIN_GAIN_PCT = 0.4

# Hotspot above this aborts the session outright, whatever the score says.
HOTSPOT_ABORT_C = 105.0


@dataclass
class BenchmarkConfig:
    trials: int = 20
    test: Optional[str] = None          # e.g. "TimeSpy"; inferred if omitted
    run_timeout_sec: float = 1200.0     # how long to wait for the user's run
    confirm_runs: int = 1
    min_gain_pct: float = DEFAULT_MIN_GAIN_PCT
    seed_candidates: bool = True
    results_dir: Optional[str] = None


@dataclass
class BenchmarkTrial:
    index: int
    config: dict
    result: Optional[BenchmarkResult]
    score: Optional[float]
    gain_pct: Optional[float]
    note: str = ""

    @property
    def failed(self) -> bool:
        return self.result is None or self.result.failed or self.score is None

    def describe(self) -> str:
        if self.failed:
            return "run failed"
        return f"{self.score:.0f} ({self.gain_pct:+.2f}%)"


@dataclass
class BenchmarkReport:
    state: SessionState
    baseline: dict
    baseline_score: Optional[float]
    best_config: Optional[dict]
    best_score: Optional[float]
    gain_pct: Optional[float]
    trials: list[BenchmarkTrial] = field(default_factory=list)
    message: str = ""


def seed_candidates(space: SearchSpace, baseline: dict) -> list[dict]:
    """Opening moves for maximum score, in order of expected payoff.

    Deliberately not random. On a modern Radeon the power limit is the
    dominant lever, an undervolt raises sustained boost inside whatever power
    budget is allowed, and clock and memory offsets add on top. Starting from
    a random corner of the space wastes runs that each cost minutes.
    """
    def at(**overrides) -> Optional[dict]:
        candidate = dict(baseline)
        for name, fraction in overrides.items():
            knob = space.knob(name)
            if knob is None:
                continue
            if fraction == "max":
                candidate[name] = knob.clamp(knob.high)
            elif fraction == "min":
                candidate[name] = knob.low
            else:
                base = baseline.get(name, knob.default or 0)
                candidate[name] = knob.clamp(base + fraction)
        return candidate if candidate != baseline else None

    proposals = [
        at(**{POWER_LIMIT: "max"}),
        at(**{POWER_LIMIT: "max", VOLTAGE: -40}),
        at(**{POWER_LIMIT: "max", VOLTAGE: -80}),
        at(**{POWER_LIMIT: "max", VOLTAGE: -60, MAX_CLOCK: 100}),
        at(**{POWER_LIMIT: "max", VOLTAGE: -60, VRAM_CLOCK: 80}),
        at(**{POWER_LIMIT: "max", VOLTAGE: -60, MAX_CLOCK: 150, VRAM_CLOCK: 120}),
    ]
    seen, out = [], []
    for candidate in proposals:
        if candidate and candidate not in seen:
            seen.append(candidate)
            out.append(candidate)
    return out


class BenchmarkSession:
    def __init__(self, hub, applier, space: SearchSpace, safeguard, optimizer,
                 config: Optional[BenchmarkConfig] = None,
                 knowledge=None, watchdog=None,
                 stability: Optional[StabilityMonitor] = None,
                 gpu_key: str = "", exe: str = "3dmark"):
        self._hub = hub
        self._applier = applier
        self._space = space
        self._safeguard = safeguard
        self._optimizer = optimizer
        self._config = config or BenchmarkConfig()
        self._knowledge = knowledge
        self._watchdog = watchdog
        self._stability = stability or StabilityMonitor()
        self._gpu_key = gpu_key
        self._exe = exe

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._watcher: Optional[ResultWatcher] = None
        self._seeds: list[dict] = []
        self._score_attr = "graphics"
        self._run_started = 0.0
        self._event_count = 0
        self._abort_reason = ""

        self.state = SessionState.IDLE
        self.baseline: dict = {}
        self.baseline_score: Optional[float] = None
        self.trials: list[BenchmarkTrial] = []
        self.report: Optional[BenchmarkReport] = None

        self.on_state: Optional[Callable[[SessionState], None]] = None
        self.on_trial: Optional[Callable[[BenchmarkTrial], None]] = None
        self.on_await_run: Optional[Callable[[int, dict], None]] = None
        self.on_log: Optional[Callable[[str, str], None]] = None
        self.on_done: Optional[Callable[[BenchmarkReport], None]] = None

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
        self._abort_reason = ""
        self._thread = threading.Thread(target=self._run, name="voltshift-bench",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=15)
            if not self._thread.is_alive():
                self._thread = None

    def _log(self, message: str, level: str = "info") -> None:
        if self.on_log:
            self.on_log(message, level)

    def _set_state(self, state: SessionState) -> None:
        self.state = state
        if self.on_state:
            self.on_state(state)

    # ── main loop ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._unsubscribe = self._hub.subscribe(self._stability.feed)
            self._execute()
        except Exception as exc:
            self._log(f"benchmark tuning failed: {exc}", "error")
            try:
                self._revert()
            except Exception as recovery_exc:
                self._log(f"recovery failed; journal retained: {recovery_exc}", "error")
                exc = RuntimeError(f"{exc}; recovery failed: {recovery_exc}")
            self._finish(SessionState.FAILED, str(exc))
        finally:
            if self._unsubscribe:
                self._unsubscribe()
                self._unsubscribe = None
            self._stability.reset()

    def _execute(self) -> None:
        self.baseline = self._applier.read_current()
        if any(self.baseline.get(name) is None for name in self._space.names):
            return self._finish(SessionState.FAILED,
                                "cannot read a complete baseline; no tuning applied")
        if self._watchdog is not None:
            self._watchdog.set_known_good(self.baseline)

        self._watcher = ResultWatcher(self._config.results_dir, self._config.test)
        running = detect_running_benchmark()
        if running:
            self._log(f"{running} is running")

        # Baseline: one run at whatever the card is set to now.
        self._set_state(SessionState.BASELINE)
        self._log(f"baseline: {self._space.describe(self.baseline)}")
        self._await("run the benchmark now to establish a baseline", 0, self.baseline)
        baseline_result = self._wait_for_run()
        if baseline_result is None:
            return self._finish(SessionState.ABORTED,
                                "no benchmark result appeared — nothing to tune against")
        if baseline_result.failed:
            return self._finish(SessionState.FAILED,
                                "the baseline run failed; fix that before tuning")

        self._score_attr = "graphics" if baseline_result.graphics is not None else "overall"
        self.baseline_score = self._result_score(baseline_result)
        if self.baseline_score is None:
            return self._finish(SessionState.FAILED,
                                "the baseline has no positive finite score")
        if self._config.test is None:
            self._config.test = baseline_result.test
            self._watcher.test = baseline_result.test
        self._log(f"baseline {baseline_result.describe()} — "
                  f"a gain must beat {self._config.min_gain_pct:.2f}% to count", "volt")

        self._optimizer.observe(self.baseline, self.baseline_score)
        if self._config.seed_candidates:
            self._seeds = [c for c in seed_candidates(self._space, self.baseline)
                           if self._safeguard.check(c).ok]
            if self._seeds:
                self._log(f"{len(self._seeds)} opening candidates queued "
                          f"before the model takes over")

        self._set_state(SessionState.EXPLORING)
        for index in range(self._config.trials):
            if self._stop.is_set():
                break
            if not self._run_trial(index):
                break

        self._commit_best()

    def _next_config(self) -> Optional[dict]:
        current = self._applier.read_current()
        if self._seeds:
            return self._seeds.pop(0)
        candidate = self._optimizer.suggest(reject=self._safeguard.rejects())
        return candidate if self._safeguard.check(candidate).ok else None

    def _run_trial(self, index: int) -> bool:
        candidate = self._next_config()
        if candidate is None:
            self._log("no safe configuration left to try", "warn")
            return False

        if self._watchdog is not None:
            self._watchdog.journal(candidate, f"benchmark trial {index + 1}")
        self._applier.apply(candidate)
        self._stability.note_change(candidate)

        self._log(f"trial {index + 1}/{self._config.trials}: "
                  f"{self._space.describe(candidate)}")
        self._await(f"run the benchmark again (trial {index + 1})", index + 1, candidate)

        result = self._wait_for_run()
        if result is None:
            self._stop.set()
            if self._abort_reason:
                self._safeguard.mark_unsafe(candidate, "stability")
            self._log("timed out waiting for a benchmark result", "warn")
            return False

        trial = self._score(index, candidate, result)
        self.trials.append(trial)
        if self.on_trial:
            self.on_trial(trial)
        return True

    def _score(self, index: int, candidate: dict,
               result: BenchmarkResult) -> BenchmarkTrial:
        # A failed run right after a settings change is a stability signal as
        # real as a driver reset, and is treated as one.
        if result.failed:
            self._log(f"trial {index + 1}: run FAILED — marking unsafe", "error")
            self._safeguard.mark_unsafe(candidate, "benchmark_failure")
            if self._knowledge is not None and self._gpu_key:
                latest = self._hub.latest
                self._knowledge.record_failure(
                    self._gpu_key, candidate.get(VOLTAGE),
                    latest.clock_mhz if latest else None)
            self._revert()
            # Penalise well below baseline so the model avoids the region.
            self._optimizer.observe(candidate, (self.baseline_score or 1.0) * 0.5)
            return BenchmarkTrial(index, candidate, result, None, None, "run failed")

        score = self._result_score(result)
        if score is None:
            self._revert()
            return BenchmarkTrial(index, candidate, result, None, None, "no score")

        gain = (score - self.baseline_score) / self.baseline_score * 100.0
        self._optimizer.observe(candidate, score)
        if self._knowledge is not None and self._gpu_key:
            self._knowledge.record_observation(
                self._gpu_key, self._exe, "benchmark", candidate, gain, stable=True)

        level = "volt" if gain > self._config.min_gain_pct else "info"
        self._log(f"trial {index + 1}: {score:.0f} ({gain:+.2f}% vs baseline)", level)
        return BenchmarkTrial(index, candidate, result, score, gain)

    # ── waiting on the user's run ────────────────────────────────────────────

    def _await(self, message: str, index: int, config: dict) -> None:
        self._run_started = time.monotonic()
        self._event_count = len(self._stability.critical_events)
        if self.on_await_run:
            self.on_await_run(index, config)
        self._log(message)

    def _wait_for_run(self) -> Optional[BenchmarkResult]:
        result = self._watcher.wait(self._config.run_timeout_sec,
                                    should_stop=self._run_should_stop)
        if self._run_should_stop():
            return None
        return result

    def _run_should_stop(self) -> bool:
        if self._stop.is_set():
            return True
        samples = [s for s in self._hub.history(600) if s.t >= self._run_started]
        hottest = max((s.hotspot_c or 0 for s in samples), default=0)
        if hottest >= HOTSPOT_ABORT_C:
            self._abort_reason = f"hotspot reached {hottest:.0f}C during the run"
        elif len(self._stability.critical_events) > self._event_count:
            self._abort_reason = str(self._stability.critical_events[-1])
        if self._abort_reason:
            self._log(self._abort_reason, "error")
            self._stop.set()
        return self._stop.is_set()

    def _result_score(self, result: BenchmarkResult) -> Optional[float]:
        if self._config.test and result.test.lower() != self._config.test.lower():
            return None
        value = getattr(result, self._score_attr)
        return value if value is not None and math.isfinite(value) and value > 0 else None

    # ── commit ───────────────────────────────────────────────────────────────

    def _revert(self) -> None:
        try:
            self._applier.apply(self.baseline, skip_unchanged=False)
        except Exception:
            self._applier.reset()
        if self._watchdog is not None:
            self._watchdog.abandon()

    def _commit_best(self) -> None:
        if self._stop.is_set():
            self._revert()
            return self._finish(SessionState.ABORTED,
                                f"{self._abort_reason or 'stopped'}; baseline restored")
        scored = [t for t in self.trials if not t.failed and t.score is not None]
        if not scored:
            self._revert()
            return self._finish(SessionState.DONE,
                                "no completed runs to compare — baseline restored")

        best = max(scored, key=lambda t: t.score)
        if best.gain_pct is None or best.gain_pct < self._config.min_gain_pct:
            self._revert()
            return self._finish(
                SessionState.DONE,
                f"best was {best.gain_pct:+.2f}%, within run-to-run noise "
                f"(±{self._config.min_gain_pct:.2f}%) — baseline kept")

        self._set_state(SessionState.CONFIRMING)
        if self._watchdog is not None:
            self._watchdog.journal(best.config, "benchmark confirmation")
        self._applier.apply(best.config)
        self._stability.note_change(best.config)
        self._log(f"confirming best: {self._space.describe(best.config)} "
                  f"({best.gain_pct:+.2f}%)")

        confirmed = best.score
        for _ in range(self._config.confirm_runs):
            if self._stop.is_set():
                self._revert()
                return self._finish(SessionState.ABORTED, "stopped; baseline restored")
            self._await("run the benchmark once more to confirm the result",
                        -1, best.config)
            result = self._wait_for_run()
            if result is None:
                self._revert()
                return self._finish(SessionState.ABORTED,
                                    "confirmation incomplete; baseline restored")
            if result.failed:
                self._safeguard.mark_unsafe(best.config, "benchmark_failure")
                self._revert()
                return self._finish(SessionState.DONE,
                                    "the best configuration failed on re-test — "
                                    "baseline restored")
            score = self._result_score(result)
            if score is None:
                self._revert()
                return self._finish(SessionState.FAILED,
                                    "confirmation has no comparable score; baseline restored")
            confirmed = min(confirmed, score)

        if self._stop.is_set():
            self._revert()
            return self._finish(SessionState.ABORTED, "stopped; baseline restored")

        gain = (confirmed - self.baseline_score) / self.baseline_score * 100.0
        if gain < self._config.min_gain_pct:
            self._revert()
            return self._finish(SessionState.DONE,
                                f"did not hold up on re-test ({gain:+.2f}%) — "
                                f"baseline restored")

        self._set_state(SessionState.APPLYING)
        if self._stop.is_set():
            self._revert()
            return self._finish(SessionState.ABORTED, "stopped; baseline restored")
        if self._watchdog is not None:
            self._watchdog.journal(best.config, "benchmark result")
        self._applier.apply(best.config)
        if self._stop.is_set():
            self._revert()
            return self._finish(SessionState.ABORTED, "stopped; baseline restored")
        if self._watchdog is not None:
            self._watchdog.verify(force=True)
        if self._knowledge is not None and self._gpu_key:
            self._knowledge.record_best(self._gpu_key, self._exe, "benchmark",
                                        best.config, gain)

        self.report = BenchmarkReport(
            SessionState.DONE, self.baseline, self.baseline_score, best.config,
            confirmed, gain, self.trials,
            f"{self.baseline_score:.0f} → {confirmed:.0f} ({gain:+.2f}%)")
        self._set_state(SessionState.DONE)
        self._log(f"applied {self._space.describe(best.config)} — "
                  f"{self.baseline_score:.0f} → {confirmed:.0f} ({gain:+.2f}%)", "volt")
        if self.on_done:
            self.on_done(self.report)

    def _finish(self, state: SessionState, message: str) -> None:
        self.report = BenchmarkReport(state, self.baseline, self.baseline_score,
                                      None, None, None, self.trials, message)
        self._set_state(state)
        self._log(message)
        if self.on_done:
            self.on_done(self.report)
