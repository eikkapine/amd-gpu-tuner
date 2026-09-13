"""AMD GPU Tuner command-line interface.

    voltshift autotune            find the best settings for what is running now
    voltshift benchtune           tune for maximum benchmark score (3DMark)
    voltshift adaptive            live governor: learn and adapt while you play
    voltshift verify              measure which tuning controls this GPU honours
    voltshift knowledge ...       inspect what has been learned about this card
    voltshift run                 run the dynamic voltage engine (Ctrl+C stops + resets)
    voltshift info                GPU + capability summary
    voltshift metrics [-w]        one-shot or watch live metrics
    voltshift tune ...            manual tuning (voltage/clocks/vram/power/fans)
    voltshift gfx ...             3D driver settings
    voltshift display ...         per-display settings
    voltshift profile ...         save / load / list profiles
    voltshift reset               restore AMD factory tuning
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import threading
import time

from . import APP_NAME, __version__, paths
from .bridgeclient import BridgeClient, BridgeError
from .crashlog import CrashLogger
from .engine import EngineConfig
from .runner import EngineRunner
from . import profiles as profile_store


class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"


_LEVEL_COLOR = {"info": C.DIM, "volt": C.GREEN, "warn": C.YELLOW, "error": C.RED}


def _log(msg: str, level: str = "info") -> None:
    ts = time.strftime("%H:%M:%S")
    color = _LEVEL_COLOR.get(level, "")
    label = {"volt": "VOLT", "warn": "WARN", "error": "ERR "}.get(level, "INFO")
    print(f"{C.DIM}[{ts}]{C.RESET} {color}{label}  {msg}{C.RESET}")


def _banner(subtitle: str) -> None:
    print(f"\n{C.CYAN}  {APP_NAME} {__version__}{C.RESET} — {subtitle}\n")


def _load_engine_config(path: str | None) -> EngineConfig:
    if not path:
        try:
            with open(paths.config_path(), encoding="utf-8") as f:
                return EngineConfig.from_dict(json.load(f).get("engine", {}))
        except (OSError, ValueError):
            return EngineConfig()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return EngineConfig.from_dict(data.get("engine", data))


# ── subcommands ───────────────────────────────────────────────────────────────

def cmd_run(args) -> int:
    config = _load_engine_config(args.config)
    _banner("dynamic voltage engine")
    print("  Thresholds:")
    for t in sorted(config.thresholds, key=lambda t: t.clock_mhz, reverse=True):
        print(f"    ≥ {t.clock_mhz:>5} MHz  →  {t.offset_mv:>+5} mV")
    print(f"    below        →  {config.idle_offset_mv:>+5} mV  (idle)")
    print(f"  Poll {config.poll_interval_sec}s · hysteresis {config.hysteresis_count}\n")

    with BridgeClient() as bridge:
        gpu_name = bridge.info().get("name", "unknown")
        _log(f"GPU: {gpu_name}")

        crash_logger = CrashLogger(config.to_dict())
        crash_logger.on_log_entry = _log
        runner = None
        runner_started = False
        previous_sigint = signal.getsignal(signal.SIGINT)
        handler_installed = False
        try:
            crash_logger.check_previous_session()
            crash_logger.start()
            crash_logger.write_session_header(gpu_name, config.to_dict())

            runner = EngineRunner(bridge, config, crash_logger)
            runner.on_log_entry = _log
            if not args.quiet:
                def show_sample(sample):
                    def number(value, spec=".0f"):
                        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                                or not math.isfinite(value)):
                            return "-"
                        return format(value, spec)

                    clock = number(sample.get("clockMhz"))
                    offset = number(sample.get("appliedOffsetMv"), "+.0f")
                    temperature = number(sample.get("tempC"))
                    power = number(sample.get("boardPowerW", sample.get("powerW")))
                    print(f"{C.DIM}  {clock:>5} MHz  {offset:>5} mV  "
                          f"{temperature:>3}°C  {power:>4} W{C.RESET}", end="\r")

                runner.on_sample = show_sample

            stop_requested = []

            def handle_sigint(_sig, _frame):
                stop_requested.append(True)

            signal.signal(signal.SIGINT, handle_sigint)
            handler_installed = True
            runner.start()
            runner_started = True
            _log("Engine running — Ctrl+C stops and restores factory tuning")

            while runner.running and not stop_requested:
                time.sleep(0.2)
            print()
        finally:
            try:
                if runner is not None:
                    runner.stop(reset_gpu=runner_started)
            finally:
                try:
                    crash_logger.stop()
                finally:
                    try:
                        crash_logger.write_session_footer(crash_logger.crash_count)
                    finally:
                        if handler_installed:
                            signal.signal(signal.SIGINT, previous_sigint)
        _log("Stopped cleanly")
    return 0


def cmd_info(args) -> int:
    with BridgeClient() as bridge:
        info = bridge.info()
        caps = bridge.caps()
        if args.json:
            print(json.dumps({"info": info, "caps": caps}, indent=2))
            return 0
        _banner("GPU information")
        print(f"  GPU        : {info.get('name')}")
        print(f"  VRAM       : {info.get('vramMb')} MB {info.get('vramType')}")
        print(f"  Device ID  : {info.get('deviceId')} rev {info.get('revisionId')}")
        bios = info.get("bios", {})
        print(f"  BIOS       : {bios.get('version')} ({bios.get('date')})")
        tuning = caps.get("tuning", {})
        gfx_ifc = tuning.get("gfxInterface", {})
        ifc_name = "MGT2_1" if gfx_ifc.get("mgt2_1") else \
                   "MGT2" if gfx_ifc.get("mgt2") else \
                   "MGT1" if gfx_ifc.get("mgt1") else "none"
        print(f"  Tuning     : gfx={tuning.get('manualGfx')} ({ifc_name}) "
              f"vram={tuning.get('manualVram')} fan={tuning.get('manualFan')} "
              f"power={tuning.get('manualPower')}")
        print(f"  At factory : {tuning.get('atFactory')}")
        print(f"  Displays   : {caps.get('displayCount')} · Eyefinity: {caps.get('eyefinity')}")
        print(f"  Bridge     : v{bridge.version}\n")
    return 0


def cmd_status(args) -> int:
    from .diagnostics import collect_report, format_summary, save_report

    with BridgeClient() as bridge:
        report = collect_report(bridge)
    if getattr(args, "output", None):
        print(save_report(report, args.output))
    elif args.json:
        print(json.dumps(report, indent=2, allow_nan=False))
    else:
        print(format_summary(report))
    return 1 if report["errors"].get("info") else 0


def cmd_metrics(args) -> int:
    with BridgeClient() as bridge:
        def show() -> None:
            m = bridge.metrics()
            if args.json:
                print(json.dumps(m))
                return
            print(f"  clock {m.get('clockMhz', '?'):>5} MHz · "
                  f"vram {m.get('vramClockMhz', '?'):>5} MHz · "
                  f"{m.get('tempC', 0):>3.0f}°C (hotspot {m.get('hotspotC', 0):.0f}°C) · "
                  f"{m.get('boardPowerW', m.get('powerW', 0)):>4.0f} W · "
                  f"fan {m.get('fanRpm', '?')} rpm · "
                  f"load {m.get('usagePct', 0):.0f}%", end="\r" if args.watch else "\n")

        if args.watch:
            try:
                while True:
                    show()
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print()
        else:
            show()
    return 0


def cmd_tune(args) -> int:
    with BridgeClient() as bridge:
        if args.tune_cmd == "get":
            print(json.dumps(bridge.tuning_get(), indent=2))
        elif args.tune_cmd == "voltage":
            result = bridge.set_voltage_offset(args.mv)
            _log(f"Voltage offset {result.get('appliedMv'):+d} mV "
                 f"({result.get('interface')})", "volt")
        elif args.tune_cmd == "clocks":
            result = bridge.set_core_clocks(args.min, args.max)
            _log(f"Core clocks applied: {result}", "volt")
        elif args.tune_cmd == "vram":
            result = bridge.set_vram_max(args.mhz)
            _log(f"VRAM max {result.get('appliedMhz')} MHz", "volt")
        elif args.tune_cmd == "timing":
            bridge.set_memory_timing(args.value)
            _log(f"Memory timing {args.value}", "volt")
        elif args.tune_cmd == "power":
            result = bridge.set_power_limit(args.pct)
            _log(f"Power limit {result.get('applied'):+d}%", "volt")
        elif args.tune_cmd == "fans":
            print(json.dumps(bridge.fans_get(), indent=2))
        elif args.tune_cmd == "fancurve":
            points = []
            for pair in args.points:
                temp, speed = pair.split(":")
                points.append({"tempC": int(temp), "speedPct": int(speed)})
            result = bridge.set_fan_curve(points)
            _log(f"Fan curve applied: {result['curve']}", "volt")
        elif args.tune_cmd == "zerorpm":
            bridge.set_zero_rpm(args.state == "on")
            _log(f"ZeroRPM {args.state}", "volt")
    return 0


def cmd_gfx(args) -> int:
    with BridgeClient() as bridge:
        if args.gfx_cmd == "get":
            print(json.dumps(bridge.gfx_get(), indent=2))
        elif args.gfx_cmd == "set":
            kwargs = json.loads(args.values)
            result = bridge.gfx_set(args.feature, **kwargs)
            _log(f"gfx {result.get('feature')} applied", "volt")
        elif args.gfx_cmd == "reset-shader-cache":
            bridge.reset_shader_cache()
            _log("Shader cache reset", "volt")
    return 0


def cmd_display(args) -> int:
    with BridgeClient() as bridge:
        if args.display_cmd == "list":
            for d in bridge.display_list():
                print(f"  [{d['index']}] {d['name']}  {d['width']}x{d['height']} "
                      f"@ {d['refreshHz']:.0f} Hz")
        elif args.display_cmd == "get":
            print(json.dumps(bridge.display_get(args.index), indent=2))
        elif args.display_cmd == "set":
            kwargs = json.loads(args.values)
            bridge.display_set(args.index, args.feature, **kwargs)
            _log(f"display[{args.index}] {args.feature} applied", "volt")
    return 0


def cmd_profile(args) -> int:
    if args.profile_cmd == "list":
        for path in profile_store.list_profiles():
            print(path)
        return 0
    if args.profile_cmd == "inspect":
        print(json.dumps(profile_store.load(args.path), indent=2))
        return 0
    profile = profile_store.load(args.path) if args.profile_cmd == "load" else None
    with BridgeClient() as bridge:
        if args.profile_cmd == "save":
            engine_config = _load_engine_config(None)
            profile = profile_store.capture(bridge, engine_config, sections=args.sections)
            path = profile_store.save(profile, args.name)
            _log(f"Profile saved → {path}")
        elif args.profile_cmd == "load":
            log = profile_store.apply(bridge, profile, sections=args.sections)
            for line in log:
                _log(line)
            failed = any(line.startswith("skipped:") and
                         not line.startswith("skipped: engine configuration") for line in log)
            return 1 if failed else 0
    return 0


def cmd_reset(_args) -> int:
    with BridgeClient() as bridge:
        bridge.tuning_reset()
        _log("GPU restored to AMD factory tuning", "volt")
    return 0


# ── closed loop ───────────────────────────────────────────────────────────────

def _with_stack(fn, verify: bool = True, auto_fetch: bool = True):
    """Build the auto-tuning stack, run `fn(stack)`, then tear it down.

    `verify` measures which controls the card actually honours before tuning
    starts. It writes to the GPU, so read-only commands pass verify=False.
    `auto_fetch` allows PresentMon to be installed on first use.
    """
    from . import autostack, gpuprofile

    with BridgeClient() as bridge:
        stack = autostack.build(bridge, on_log=_log, auto_fetch_frames=auto_fetch)
        try:
            recovery = autostack.recover_previous_session(stack)
            if recovery:
                _log(recovery, "error")
            if not stack.tunable:
                _log("this GPU exposes no manual tuning controls", "error")
                return 1

            note = gpuprofile.arch_note(stack.space)
            if note:
                _log(note)
            if verify:
                stack.verify_controls(on_log=_log)
                if not stack.tunable:
                    _log("none of this GPU's advertised tuning controls "
                         "actually respond to writes", "error")
                    return 1
                _log(f"tuning controls in use: {', '.join(stack.space.names)}")

            stack.hub.start()
            return fn(stack)
        finally:
            stack.close()


def cmd_verify(args) -> int:
    """Measure which tuning controls this GPU honours."""
    from . import autostack, gpuprofile

    _banner("verifying tuning controls")
    with BridgeClient() as bridge:
        stack = autostack.build(bridge, on_log=_log)
        try:
            if not stack.tunable:
                _log("this GPU exposes no manual tuning controls", "error")
                return 1
            note = gpuprofile.arch_note(stack.space)
            if note:
                print(f"  {note}\n")
            advertised = list(stack.space.names)
            checks = stack.verify_controls(force=args.force, on_log=_log)
            print()
            for check in checks:
                mark = "✓" if check.supported else "✗"
                colour = C.GREEN if check.supported else C.YELLOW
                print(f"  {colour}{mark} {check.name:<18}{C.RESET} {check.detail}")
            print()
            _log(f"advertised by ADLX : {', '.join(advertised)}")
            _log(f"actually usable    : {', '.join(stack.space.names) or 'none'}")
        finally:
            stack.close()
    return 0


def cmd_autotune(args) -> int:
    from .optimizer.session import AutoTuneSession, SessionConfig
    from .optimizer.objective import GOALS
    from .gameproc import detect_game

    _banner(f"auto-tune — {GOALS[args.goal].label}")

    def run(stack) -> int:
        _log(f"frame source — {stack.frame_source_status}")
        if stack.hub.frame_source.name == "none":
            _log("no frame source: tuning on power, clocks and thermals only. "
                 "Run scripts/fetch_presentmon.ps1 for frame-rate aware tuning.", "warn")

        game = detect_game(stack.hub.frame_source)
        exe = args.game or (game.exe if game else "desktop")
        _log(f"target workload: {exe}")

        session = AutoTuneSession(
            stack.hub, stack.applier, stack.space, stack.safeguard,
            stack.new_optimizer(),
            SessionConfig(goal=args.goal, trials=args.trials,
                          window_sec=args.window, pairs_per_trial=args.pairs),
            knowledge=stack.knowledge, watchdog=stack.watchdog,
            stability=stack.stability, gpu_key=stack.gpu_key, exe=exe)
        session.on_log = _log

        done = threading.Event()
        session.on_done = lambda report: done.set()

        def interrupt(_sig, _frame):
            _log("stopping — restoring baseline", "warn")
            session.stop()
            done.set()

        signal.signal(signal.SIGINT, interrupt)
        session.start()
        while not done.wait(0.5) and session.running:
            pass
        session.stop()

        report = session.report
        if report is None:
            return 1
        if report.best_config:
            print()
            _log(f"applied: {stack.space.describe(report.best_config)}", "volt")
            _log(f"result: {report.best_score.explain()}", "volt")
        else:
            _log(report.message)
        return 0

    return _with_stack(run, auto_fetch=not args.no_download)


def cmd_benchtune(args) -> int:
    """Tune against a benchmark score rather than a live gameplay window."""
    from .optimizer.benchsession import BenchmarkConfig, BenchmarkSession
    from . import benchmark as bm

    _banner("benchmark tuning — maximise score")

    def run(stack) -> int:
        history = bm.history(test=args.test)
        done = [r for r in history if not r.failed and r.objective]
        if done:
            best = max(r.objective for r in done)
            _log(f"{len(done)} previous {args.test or 'benchmark'} runs on record, "
                 f"best score {best:.0f}")

        session = BenchmarkSession(
            stack.hub, stack.applier, stack.space, stack.safeguard,
            stack.new_optimizer(),
            BenchmarkConfig(trials=args.trials, test=args.test,
                            run_timeout_sec=args.timeout,
                            min_gain_pct=args.min_gain,
                            seed_candidates=not args.no_seed),
            knowledge=stack.knowledge, watchdog=stack.watchdog,
            stability=stack.stability, gpu_key=stack.gpu_key,
            exe=(args.test or "benchmark").lower())
        session.on_log = _log

        def await_run(index: int, config: dict) -> None:
            label = "confirmation run" if index < 0 else (
                "baseline run" if index == 0 else f"run {index}")
            print(f"\n{C.CYAN}  ▶  {label}: start the benchmark in 3DMark now{C.RESET}")
            print(f"{C.DIM}     applied: {stack.space.describe(config)}{C.RESET}\n")

        session.on_await_run = await_run

        finished = threading.Event()
        session.on_done = lambda report: finished.set()

        def interrupt(_sig, _frame):
            _log("stopping — restoring baseline", "warn")
            session.stop()
            finished.set()

        signal.signal(signal.SIGINT, interrupt)
        session.start()
        while not finished.wait(0.5) and session.running:
            pass
        session.stop()

        report = session.report
        if report is None:
            return 1
        print()
        if report.best_config:
            _log(f"applied: {stack.space.describe(report.best_config)}", "volt")
            _log(f"score:   {report.message}", "volt")
        else:
            _log(report.message)
        return 0

    return _with_stack(run, auto_fetch=False)


def cmd_adaptive(args) -> int:
    from .adaptive import AdaptiveGovernor, ProbeBudget
    from .optimizer.objective import GOALS

    _banner(f"adaptive governor — {GOALS[args.goal].label}")

    def run(stack) -> int:
        _log(f"frame source — {stack.frame_source_status}")
        budget = ProbeBudget(max_probes=args.probes,
                             min_interval_sec=args.probe_interval)
        if args.probes == 0:
            _log("probing disabled — applying learned profiles only")
        else:
            _log(f"probe budget: {args.probes} per game, "
                 f"at most one every {args.probe_interval:.0f}s")

        governor = AdaptiveGovernor(
            stack.hub, stack.applier, stack.space, stack.safeguard,
            knowledge=stack.knowledge, watchdog=stack.watchdog,
            stability=stack.stability, gpu_key=stack.gpu_key,
            goal=args.goal, budget=budget)
        governor.on_log = _log

        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        governor.start()
        _log("running — Ctrl+C to stop and restore")
        try:
            while not stop.wait(1.0):
                pass
        finally:
            governor.stop(restore=True)
        return 0

    return _with_stack(run, auto_fetch=not args.no_download)


def cmd_knowledge(args) -> int:
    from .knowledge import KnowledgeStore, gpu_key

    with BridgeClient() as bridge:
        key = gpu_key(bridge.info())
    store = KnowledgeStore()
    try:
        if args.knowledge_cmd == "stats":
            stats = store.stats(key)
            _banner("learned GPU settings")
            print(f"  observations   {stats['observations']}")
            print(f"  games          {stats['games']}")
            print(f"  unsafe configs {stats['unsafe']}")
            print(f"  frontier bands {stats['frontier_bands']}")
            frontier = store.frontier(key)
            if frontier:
                print("\n  stability frontier (lowest voltage that misbehaved):")
                for band in frontier:
                    print(f"    ~{band['clock_mhz']:>5} MHz   {band['failed_mv']:+5d} mV"
                          f"   ({band['failures']} event(s))")
        elif args.knowledge_cmd == "games":
            games = store.known_games(key)
            if not games:
                _log("nothing learned yet — run `voltshift autotune` while playing")
            for entry in games:
                config = store.best_config(key, entry["exe"], entry["goal"])
                print(f"  {entry['exe']:<28} {entry['goal']:<12} "
                      f"score {entry['score']:+.3f}   {config}")
        elif args.knowledge_cmd == "forget":
            store.forget_game(key, args.game)
            _log(f"forgot everything learned about {args.game}")
        elif args.knowledge_cmd == "reset-frontier":
            removed = store.reset_frontier(key)
            _log(f"cleared the stability frontier ({removed} band(s)) and the "
                 f"unsafe set for this card", "volt")
            _log("AMD GPU Tuner will relearn the card's limits from scratch")
        elif args.knowledge_cmd == "export":
            print(json.dumps(store.export(), indent=2))
    finally:
        store.close()
    return 0


# ── parser ────────────────────────────────────────────────────────────────────

def positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amd-gpu-tuner", description=f"{APP_NAME} — AMD Radeon tuning suite")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run the dynamic voltage engine")
    run.add_argument("--config", help="engine config JSON path")
    run.add_argument("--quiet", action="store_true", help="suppress per-poll output")
    run.set_defaults(fn=cmd_run)

    info = sub.add_parser("info", help="GPU and capability summary")
    info.add_argument("--json", action="store_true")
    info.set_defaults(fn=cmd_info)

    status = sub.add_parser("status", help="read-only GPU, tuning and telemetry snapshot")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(fn=cmd_status)

    diagnostics = sub.add_parser("diagnostics", help="export a read-only hardware report")
    diagnostics.add_argument("--output", required=True, help="destination JSON file")
    diagnostics.set_defaults(fn=cmd_status, json=True)

    metrics = sub.add_parser("metrics", help="live metrics")
    metrics.add_argument("-w", "--watch", action="store_true")
    metrics.add_argument("--interval", type=positive_float, default=0.5)
    metrics.add_argument("--json", action="store_true")
    metrics.set_defaults(fn=cmd_metrics)

    tune = sub.add_parser("tune", help="manual tuning")
    tune_sub = tune.add_subparsers(dest="tune_cmd", required=True)
    tune_sub.add_parser("get", help="dump tuning state")
    p = tune_sub.add_parser("voltage", help="set voltage offset")
    p.add_argument("mv", type=int, help="offset in mV (e.g. -120)")
    p = tune_sub.add_parser("clocks", help="set core clock limits")
    p.add_argument("--min", type=int)
    p.add_argument("--max", type=int)
    p = tune_sub.add_parser("vram", help="set VRAM max clock")
    p.add_argument("mhz", type=int)
    p = tune_sub.add_parser("timing", help="set memory timing preset")
    p.add_argument("value", type=int)
    p = tune_sub.add_parser("power", help="set power limit %")
    p.add_argument("pct", type=int)
    tune_sub.add_parser("fans", help="dump fan state")
    p = tune_sub.add_parser("fancurve", help="set fan curve")
    p.add_argument("points", nargs="+", metavar="TEMP:SPEED",
                   help="e.g. 30:20 50:35 70:60 85:80 95:100")
    p = tune_sub.add_parser("zerorpm", help="toggle ZeroRPM")
    p.add_argument("state", choices=["on", "off"])
    tune.set_defaults(fn=cmd_tune)

    gfx = sub.add_parser("gfx", help="3D driver settings")
    gfx_sub = gfx.add_subparsers(dest="gfx_cmd", required=True)
    gfx_sub.add_parser("get", help="dump all 3D settings")
    p = gfx_sub.add_parser("set", help="set a feature")
    p.add_argument("feature")
    p.add_argument("values", help='JSON args, e.g. \'{"enabled":true}\'')
    gfx_sub.add_parser("reset-shader-cache", help="reset the shader cache")
    gfx.set_defaults(fn=cmd_gfx)

    display = sub.add_parser("display", help="per-display settings")
    display_sub = display.add_subparsers(dest="display_cmd", required=True)
    display_sub.add_parser("list", help="list displays")
    p = display_sub.add_parser("get", help="dump one display's settings")
    p.add_argument("index", type=int, nargs="?", default=0)
    p = display_sub.add_parser("set", help="set a display feature")
    p.add_argument("index", type=int)
    p.add_argument("feature")
    p.add_argument("values", help='JSON args, e.g. \'{"enabled":true}\'')
    display.set_defaults(fn=cmd_display)

    profile = sub.add_parser("profile", help="profiles")
    profile_sub = profile.add_subparsers(dest="profile_cmd", required=True)
    profile_sub.add_parser("list", help="list saved profiles")
    p = profile_sub.add_parser("save", help="capture current state")
    p.add_argument("name")
    p.add_argument("--sections", nargs="+", choices=profile_store.SECTIONS,
                   help="capture only these sections")
    p = profile_sub.add_parser("load", help="apply a profile file")
    p.add_argument("path")
    p.add_argument("--sections", nargs="+", choices=profile_store.SECTIONS,
                   help="apply only these sections (engine config does not auto-start)")
    p = profile_sub.add_parser("inspect", help="validate and print a profile without GPU access")
    p.add_argument("path")
    profile.set_defaults(fn=cmd_profile)

    reset = sub.add_parser("reset", help="restore AMD factory tuning")
    reset.set_defaults(fn=cmd_reset)

    from .optimizer.objective import DEFAULT_GOAL, GOALS

    auto = sub.add_parser("autotune",
                          help="find the best settings for the running workload")
    auto.add_argument("--goal", choices=sorted(GOALS), default=DEFAULT_GOAL,
                      help="what to optimise for (default: %(default)s)")
    auto.add_argument("--trials", type=positive_int, default=14,
                      help="how many configurations to try (default: %(default)s)")
    auto.add_argument("--window", type=positive_float, default=8.0,
                      help="seconds measured per window (default: %(default)s)")
    auto.add_argument("--pairs", type=positive_int, default=2,
                      help="candidate/baseline pairs per trial (default: %(default)s)")
    auto.add_argument("--game", help="attribute results to this exe name")
    auto.add_argument("--no-download", action="store_true",
                      help="do not fetch PresentMon if it is missing")
    auto.set_defaults(fn=cmd_autotune)

    adaptive = sub.add_parser("adaptive",
                              help="run the live governor while you play")
    adaptive.add_argument("--goal", choices=sorted(GOALS), default=DEFAULT_GOAL)
    adaptive.add_argument("--probes", type=nonnegative_int, default=8,
                          help="in-game experiments allowed per game, 0 to disable")
    adaptive.add_argument("--probe-interval", type=positive_float, default=120.0,
                          help="minimum seconds between probes (default: %(default)s)")
    adaptive.add_argument("--no-download", action="store_true",
                          help="do not fetch PresentMon if it is missing")
    adaptive.set_defaults(fn=cmd_adaptive)

    knowledge = sub.add_parser("knowledge", help="inspect learned GPU settings")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_cmd", required=True)
    knowledge_sub.add_parser("stats", help="summary and stability frontier")
    knowledge_sub.add_parser("games", help="best configuration per game")
    knowledge_sub.add_parser("export", help="dump everything as JSON")
    knowledge_sub.add_parser(
        "reset-frontier",
        help="forget this card's learned voltage limits and unsafe set")
    p = knowledge_sub.add_parser("forget", help="erase what was learned for a game")
    p.add_argument("game", help="exe name, e.g. cyberpunk2077.exe")
    knowledge.set_defaults(fn=cmd_knowledge)

    bench = sub.add_parser(
        "benchtune", help="tune for maximum benchmark score (3DMark, Time Spy…)")
    bench.add_argument("--test", default="TimeSpy",
                       help="benchmark to tune for (default: %(default)s)")
    bench.add_argument("--trials", type=positive_int, default=20,
                       help="configurations to try (default: %(default)s)")
    bench.add_argument("--timeout", type=positive_float, default=1200.0,
                       help="seconds to wait for each run (default: %(default)s)")
    bench.add_argument("--min-gain", type=positive_float, default=0.4,
                       help="percent gain that counts as real, not run-to-run "
                            "noise (default: %(default)s)")
    bench.add_argument("--no-seed", action="store_true",
                       help="skip the known-good opening candidates and search "
                            "from scratch")
    bench.set_defaults(fn=cmd_benchtune)

    verify = sub.add_parser(
        "verify", help="measure which tuning controls this GPU actually honours")
    verify.add_argument("--force", action="store_true",
                        help="re-test even if this card was already verified")
    verify.set_defaults(fn=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (BridgeError, ValueError, OSError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
