"""Benchmark result parsing and score-driven tuning.

Gameplay tuning compares paired short windows; inside a fixed benchmark run
that method measures the scene changing rather than the setting, which is why
it reports that nothing beat the baseline. Benchmark mode replaces the window
with a whole run and the paired delta with the score itself.
"""

import os
import time
import zipfile

import pytest

from voltshift import benchmark as bm
from voltshift.optimizer import RecordingApplier, Safeguard, SearchSpace, make_optimizer
from voltshift.optimizer.benchsession import (BenchmarkConfig, BenchmarkSession,
                                              seed_candidates)
from voltshift.optimizer.session import SessionState
from voltshift.optimizer.space import MAX_CLOCK, POWER_LIMIT, VOLTAGE, VRAM_CLOCK

RESULT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<benchmark><results>
  <result>
    <passIndex>-1</passIndex>
    <TimeSpyPerformance3DMarkScore>{overall}</TimeSpyPerformance3DMarkScore>
    <TimeSpyPerformanceCPUScore>{cpu}</TimeSpyPerformanceCPUScore>
    <TimeSpyPerformanceGraphicsScore>{graphics}</TimeSpyPerformanceGraphicsScore>
  </result>
  <result>
    <passIndex>0</passIndex>
    <TimeSpyPerformanceGraphicsScoreForPass>999</TimeSpyPerformanceGraphicsScoreForPass>
  </result>
</results></benchmark>"""


def write_result(directory, test="TimeSpy", overall=27178, graphics=30664,
                 cpu=16532, failed=False, stamp=None):
    stamp = stamp or time.strftime("%Y%m%d%H%M%S")
    label = "FAILED" if failed else str(overall)
    path = os.path.join(directory, f"3DMark-{test}-{label}-{stamp}.3dmark-result")
    with zipfile.ZipFile(path, "w") as archive:
        if not failed:
            archive.writestr("Result.xml", RESULT_XML.format(
                overall=overall, graphics=graphics, cpu=cpu))
        else:
            archive.writestr("Result.xml", "<benchmark><results/></benchmark>")
    return path


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parses_scores_from_the_archive(tmp_path):
    result = bm.parse_result(write_result(str(tmp_path)))
    assert result.test == "TimeSpy"
    assert result.graphics == 30664
    assert result.overall == 27178
    assert result.cpu == 16532
    assert not result.failed


def test_graphics_score_is_the_objective(tmp_path):
    """The overall score mixes in a CPU score VoltShift cannot influence."""
    result = bm.parse_result(write_result(str(tmp_path)))
    assert result.objective == result.graphics


def test_per_pass_duplicates_are_ignored(tmp_path):
    # ...ForPass entries repeat the totals and must not be mistaken for them.
    result = bm.parse_result(write_result(str(tmp_path)))
    assert result.graphics == 30664


def test_failed_runs_are_recognised(tmp_path):
    result = bm.parse_result(write_result(str(tmp_path), failed=True))
    assert result.failed
    assert result.objective is None


def test_a_run_with_no_score_counts_as_failed(tmp_path):
    path = os.path.join(str(tmp_path), "3DMark-TimeSpy-123-20260101000000.3dmark-result")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("other.txt", "nothing useful")
    result = bm.parse_result(path)
    # The filename still carries a score, so that is used rather than guessing.
    assert result.overall == 123


def test_corrupt_archive_does_not_raise(tmp_path):
    path = os.path.join(str(tmp_path), "3DMark-TimeSpy-500-20260101000000.3dmark-result")
    with open(path, "wb") as f:
        f.write(b"not a zip")
    result = bm.parse_result(path)
    assert result is not None
    assert result.overall == 500  # recovered from the filename


def test_history_filters_by_test(tmp_path):
    write_result(str(tmp_path), test="TimeSpy", stamp="20260101000001")
    write_result(str(tmp_path), test="SteelNomad", stamp="20260101000002")
    assert len(bm.history(str(tmp_path))) == 2
    assert len(bm.history(str(tmp_path), test="TimeSpy")) == 1


# ── watching ──────────────────────────────────────────────────────────────────

def test_watcher_ignores_results_that_predate_it(tmp_path):
    write_result(str(tmp_path), stamp="20260101000001")
    watcher = bm.ResultWatcher(str(tmp_path))
    assert watcher.poll() is None, "a trial must not be scored against an old run"


def test_watcher_reports_a_new_result(tmp_path):
    watcher = bm.ResultWatcher(str(tmp_path))
    write_result(str(tmp_path), graphics=31000, stamp="20260101000002")
    found = watcher.poll()
    assert found is not None and found.graphics == 31000
    assert watcher.poll() is None, "the same result must not be returned twice"


def test_watcher_filters_by_test(tmp_path):
    watcher = bm.ResultWatcher(str(tmp_path), test="TimeSpy")
    write_result(str(tmp_path), test="SteelNomad", stamp="20260101000003")
    assert watcher.poll() is None


# ── seed candidates ───────────────────────────────────────────────────────────

@pytest.fixture
def space():
    return SearchSpace.from_tuning({
        "gfx": {"interface": "MGT2_1", "voltageMv": 0, "maxFreqMhz": 0,
                "voltageRange": {"min": -200, "max": 0, "step": 5},
                "maxFreqRange": {"min": -500, "max": 1000, "step": 10}},
        "vram": {"maxFreqMhz": 2518,
                 "maxFreqRange": {"min": 2518, "max": 3000, "step": 2}},
        "power": {"powerLimit": 0, "powerLimitRange": {"min": -30, "max": 10}},
    })


def test_seeds_open_with_the_power_limit(space):
    """Power limit is the dominant lever for score; it should go first."""
    baseline = {VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0}
    seeds = seed_candidates(space, baseline)
    assert seeds, "there should be opening candidates"
    assert seeds[0][POWER_LIMIT] == 10, "first move is maximum power"


def test_seeds_explore_undervolt_and_offsets(space):
    baseline = {VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0}
    seeds = seed_candidates(space, baseline)
    assert any(s[VOLTAGE] < 0 for s in seeds), "undervolt buys boost headroom"
    assert any(s[MAX_CLOCK] > 0 for s in seeds), "positive clock offset for score"
    assert all(s[POWER_LIMIT] == 10 for s in seeds), "never give power back"


def test_seeds_are_unique(space):
    baseline = {VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518, POWER_LIMIT: 0}
    seeds = seed_candidates(space, baseline)
    assert len(seeds) == len({tuple(sorted(s.items())) for s in seeds})


# ── the session ───────────────────────────────────────────────────────────────

class FakeHub:
    latest = None

    def subscribe(self, callback):
        return lambda: None

    def history(self, seconds=None):
        return []

    def set_applied_offset(self, mv):
        pass


class ScriptedBenchmark:
    """Stands in for the user running 3DMark: writes a result on cue.

    Score responds to the applied configuration, so the session has something
    real to optimise: more power and a mild undervolt help, and going below
    the cliff makes the run fail.
    """

    def __init__(self, directory, applier, cliff_mv=-150):
        self.directory = directory
        self.applier = applier
        self.cliff_mv = cliff_mv
        self.runs = 0

    def run(self, index, config):
        self.runs += 1
        current = self.applier.current
        voltage = current.get(VOLTAGE, 0)
        power = current.get(POWER_LIMIT, 0)
        if voltage <= self.cliff_mv:
            write_result(self.directory, failed=True,
                         stamp=f"2026010100{self.runs:04d}")
            return
        score = 30000 + power * 60 - abs(voltage + 80) * 4
        write_result(self.directory, overall=int(score * 0.88), graphics=int(score),
                     stamp=f"2026010100{self.runs:04d}")


def _session(tmp_path, space, **overrides):
    applier = RecordingApplier({VOLTAGE: 0, MAX_CLOCK: 0, VRAM_CLOCK: 2518,
                                POWER_LIMIT: 0})
    config = BenchmarkConfig(trials=6, test="TimeSpy", run_timeout_sec=8.0,
                             results_dir=str(tmp_path), **overrides)
    session = BenchmarkSession(
        FakeHub(), applier, space, Safeguard(space), make_optimizer(space, seed=5),
        config, gpu_key="sim", exe="timespy")
    bench = ScriptedBenchmark(str(tmp_path), applier)
    session.on_await_run = bench.run
    return session, applier, bench


def _run(session, timeout=60):
    session.start()
    deadline = time.monotonic() + timeout
    while session.running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not session.running, "session did not finish"
    return session.report


def test_session_establishes_a_baseline_score(tmp_path, space):
    session, _, _ = _session(tmp_path, space)
    report = _run(session)
    assert report is not None
    assert session.baseline_score is not None


def test_session_finds_a_higher_scoring_configuration(tmp_path, space):
    session, applier, _ = _session(tmp_path, space)
    report = _run(session)
    assert report.best_config is not None, report.message
    assert report.gain_pct > 0
    assert report.best_score > report.baseline_score


def test_session_pushes_the_power_limit_up_for_score(tmp_path, space):
    session, _, _ = _session(tmp_path, space)
    report = _run(session)
    assert report.best_config is not None, report.message
    assert report.best_config[POWER_LIMIT] > 0


def test_session_stays_above_the_failure_cliff(tmp_path, space):
    session, _, _ = _session(tmp_path, space)
    report = _run(session)
    assert report.best_config is not None, report.message
    assert report.best_config[VOLTAGE] > -150


def test_a_failed_run_marks_the_configuration_unsafe(tmp_path, space):
    applier = RecordingApplier(space.default_config())
    guard = Safeguard(space)
    config = BenchmarkConfig(trials=1, test="TimeSpy", run_timeout_sec=6.0,
                             results_dir=str(tmp_path), seed_candidates=False)
    session = BenchmarkSession(FakeHub(), applier, space, guard,
                               make_optimizer(space, seed=1), config,
                               gpu_key="sim")

    state = {"n": 0}

    def always_fail(index, cfg):
        state["n"] += 1
        # Baseline must succeed, or there is nothing to compare against.
        write_result(str(tmp_path), failed=state["n"] > 1,
                     stamp=f"2026010100{state['n']:04d}")

    session.on_await_run = always_fail
    _run(session)
    assert any(t.failed for t in session.trials)


def test_gains_inside_the_noise_floor_are_not_committed(tmp_path, space):
    """A 0.1% "win" on a benchmark whose runs vary by 0.18% is not a win."""
    applier = RecordingApplier(space.default_config())
    config = BenchmarkConfig(trials=2, test="TimeSpy", run_timeout_sec=6.0,
                             results_dir=str(tmp_path), seed_candidates=False,
                             min_gain_pct=0.4)
    session = BenchmarkSession(FakeHub(), applier, space, Safeguard(space),
                               make_optimizer(space, seed=2), config, gpu_key="sim")

    scores = iter([30000, 30020, 30015])  # +0.07%, +0.05% — noise

    def tiny_gain(index, cfg):
        write_result(str(tmp_path), graphics=next(scores),
                     stamp=f"202601010{index + 100:05d}")

    session.on_await_run = tiny_gain
    report = _run(session)
    assert report.best_config is None
    assert "noise" in report.message


def test_missing_baseline_run_aborts_cleanly(tmp_path, space):
    applier = RecordingApplier(space.default_config())
    config = BenchmarkConfig(trials=2, test="TimeSpy", run_timeout_sec=1.0,
                             results_dir=str(tmp_path), seed_candidates=False)
    session = BenchmarkSession(FakeHub(), applier, space, Safeguard(space),
                               make_optimizer(space, seed=1), config, gpu_key="sim")
    session.on_await_run = lambda index, cfg: None  # user never runs anything
    report = _run(session, timeout=20)
    assert report.state == SessionState.ABORTED
    assert applier.current[VOLTAGE] == 0


@pytest.mark.parametrize("baseline", [{}, {VOLTAGE: 0, POWER_LIMIT: 0}])
def test_incomplete_tuning_baseline_fails_before_any_write(space, monkeypatch, baseline):
    from voltshift.optimizer import benchsession

    applier = RecordingApplier(baseline)

    def unexpected(*args, **kwargs):
        pytest.fail("incomplete readback must fail before journaling or waiting for a benchmark")

    class Watchdog:
        set_known_good = unexpected

    monkeypatch.setattr(benchsession, "ResultWatcher", unexpected)
    session = BenchmarkSession(FakeHub(), applier, space, Safeguard(space),
                               make_optimizer(space, seed=1), watchdog=Watchdog())
    session._execute()
    assert session.report.state == SessionState.FAILED
    assert "complete baseline" in session.report.message
    assert session.report.best_config is None
    assert applier.history == []
    assert applier.reset_count == 0
