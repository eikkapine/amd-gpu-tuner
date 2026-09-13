"""VoltShift crash logger — post-mortem flight recorder.

When a GPU TDR fires, Windows kills every process with a GPU context —
including VoltShift — so no in-process watcher can catch the moment itself.
Instead:

  Phase 1 (live): every poll cycle appends telemetry to an on-disk ring
    buffer, and a heartbeat file marks the session as alive.

  Phase 2 (post-mortem): on the next startup, a leftover heartbeat means the
    previous session died. The Windows Event Log is queried for TDR/crash
    events during that session and a report is reconstructed from the saved
    telemetry.

Read-only by design: this module never writes GPU state.
"""

from __future__ import annotations

import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

from . import paths

TELEMETRY_FILE = "voltshift_telemetry.json"
HEARTBEAT_FILE = "voltshift_session.json"
MAX_TELEMETRY_ROWS = 300  # ~150 s at 0.5 s polling

try:
    import win32evtlog  # type: ignore

    _EVTLOG_OK = True
except ImportError:
    _EVTLOG_OK = False


# ── Windows Event Log ─────────────────────────────────────────────────────────

def _query_events(log_name: str, event_id: int, after_time: str = "",
                  max_events: int = 10) -> list[dict]:
    """Query the Event Log for one EventID, newest first.

    Tries a timediff(@SystemTime) XPath filter first (the documented way to
    time-bound a query), then a 24 h window, then no filter with manual
    post-filtering.
    """
    if not _EVTLOG_OK:
        return []

    flags = win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection

    ms_since = None
    if after_time:
        try:
            clean = after_time.rstrip("Z").split("+")[0]
            if "." in clean:
                head, frac = clean.split(".", 1)
                clean = f"{head}.{frac[:6]}"
            dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            ms_since = max(0, int((datetime.now(timezone.utc) - dt).total_seconds() * 1000))
        except Exception:
            ms_since = None

    queries = []
    if ms_since is not None:
        queries.append(f"*[System[(EventID={event_id}) and "
                       f"TimeCreated[timediff(@SystemTime) <= {ms_since}]]]")
    queries.append(f"*[System[(EventID={event_id}) and "
                   f"TimeCreated[timediff(@SystemTime) <= 86400000]]]")
    queries.append(f"*[System[(EventID={event_id})]]")

    for query in queries:
        try:
            handle = win32evtlog.EvtQuery(log_name, flags, query, None)
            raw_events = win32evtlog.EvtNext(handle, max_events, -1, 0)
            results = []
            for evt in raw_events:
                try:
                    xml_str = win32evtlog.EvtRender(evt, win32evtlog.EvtRenderEventXml)
                    parsed = _parse_event_xml(xml_str)
                    if parsed:
                        results.append(parsed)
                except Exception:
                    continue
            if results:
                if after_time and "timediff" not in query:
                    after_cmp = after_time.rstrip("Z").split("+")[0].split(".")[0]
                    results = [r for r in results
                               if r.get("TimeCreated", "")[:19] >= after_cmp]
                return results
        except Exception:
            continue
    return []


def _parse_event_xml(xml_str: str) -> dict:
    ns = "http://schemas.microsoft.com/win/2004/08/events/event"
    try:
        root = ET.fromstring(xml_str)
        d: dict = {}
        sys_el = root.find(f"{{{ns}}}System")
        if sys_el is not None:
            eid = sys_el.find(f"{{{ns}}}EventID")
            if eid is not None and eid.text:
                d["EventID"] = int(eid.text)
            tc = sys_el.find(f"{{{ns}}}TimeCreated")
            if tc is not None:
                d["TimeCreated"] = tc.get("SystemTime", "")
        ri = root.find(f"{{{ns}}}RenderingInfo")
        if ri is not None:
            m = ri.find(f"{{{ns}}}Message")
            if m is not None and m.text:
                d["Message"] = m.text.strip()
        if "Message" not in d:
            ed = root.find(f"{{{ns}}}EventData")
            if ed is not None:
                parts = [f"{c.get('Name', '')}: {c.text}" for c in ed if c.text]
                d["Message"] = " | ".join(parts)
        return d if "EventID" in d and "TimeCreated" in d else {}
    except Exception:
        return {}


def _find_crash_events(session_start_iso: str) -> list[dict]:
    events = []
    events += _query_events("System", 4101, after_time=session_start_iso, max_events=5)  # TDR
    events += _query_events("System", 41, after_time=session_start_iso, max_events=3)    # kernel power
    events += _query_events("System", 6008, after_time=session_start_iso, max_events=3)  # unexpected shutdown
    events.sort(key=lambda e: e.get("TimeCreated", ""))
    return events


# ── Crash analysis ────────────────────────────────────────────────────────────

def analyse_crash(telemetry: list[dict], volt_changes: list[dict],
                  crash_iso: str, event_msg: str) -> tuple[str, str]:
    """Classify a crash from telemetry. Returns (reason_code, explanation)."""
    if not telemetry:
        return "NO_TELEMETRY", ("No telemetry was recorded before this crash. "
                                "This can happen if AMD GPU Tuner was killed immediately on driver reset.")

    latest = telemetry[-1]
    clock = latest.get("clock_mhz", 0)
    volt = latest.get("voltage_mv") or 0
    temp = latest.get("temp_c")
    hot = latest.get("hotspot_c")

    if temp is not None and temp >= 90:
        return "THERMAL", (f"GPU temperature was {temp:.0f}°C at crash time. "
                           "Driver likely crashed due to thermal protection. "
                           "Check case airflow and thermal paste.")
    if hot is not None and hot >= 100:
        return "THERMAL_HOTSPOT", (f"GPU hotspot temperature was {hot:.0f}°C. "
                                   "Hotspot thermal shutdown is likely.")

    try:
        crash_ts = datetime.fromisoformat(crash_iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        crash_ts = time.time()

    recent_changes = [c for c in volt_changes if crash_ts - c.get("ts", 0) <= 15]

    if len(recent_changes) >= 3:
        return "RAPID_VOLTAGE_SWING", (
            f"{len(recent_changes)} voltage changes in the 15s before crash. "
            "Rapid voltage switching can destabilise the GPU. "
            "Increase hysteresis to 3-4 and space thresholds further apart.")

    if clock >= 3100 and volt <= -150:
        change_detail = ""
        if recent_changes:
            secs = crash_ts - recent_changes[-1].get("ts", crash_ts)
            change_detail = f" A voltage change occurred {secs:.1f}s before the crash."
        return "UNDERVOLT_TOO_AGGRESSIVE", (
            f"Clock was {clock} MHz at {volt} mV when crash occurred.{change_detail} "
            "The GPU needed more voltage at this frequency. "
            f"Raise the ≥{3100 if clock < 3200 else 3200} MHz threshold by 20-40 mV.")

    if clock >= 3200 and volt <= -130:
        return "VOLTAGE_INSUFFICIENT_FOR_CLOCK", (
            f"Peak boost ({clock} MHz) with {volt} mV — insufficient voltage for max clocks. "
            "Make the highest-clock threshold less aggressive.")

    window_20 = [p for p in telemetry if crash_ts - p.get("ts", 0) <= 20]
    if window_20:
        hi_clk = sum(1 for p in window_20 if p.get("clock_mhz", 0) >= 3100)
        lo_vlt = sum(1 for p in window_20 if (p.get("voltage_mv") or 0) <= -140)
        if hi_clk >= len(window_20) * 0.7 and lo_vlt >= len(window_20) * 0.7:
            return "SUSTAINED_HIGH_CLOCK_LOW_VOLTAGE", (
                "GPU sustained ≥3100 MHz for ~20s at ≤-140 mV. Under sustained load "
                "the silicon needs more voltage than during brief boosts. "
                "Reduce undervolt magnitude by 20-30 mV.")

    if recent_changes:
        secs = crash_ts - recent_changes[-1].get("ts", crash_ts)
        if secs <= 5:
            old_mv = recent_changes[-1].get("old_mv")
            new_mv = recent_changes[-1].get("new_mv", volt)
            return "CRASH_AFTER_VOLTAGE_CHANGE", (
                f"Voltage changed from {old_mv} mV to {new_mv} mV exactly "
                f"{secs:.1f}s before the crash. "
                "Increase hysteresis so voltage changes require more confirmation reads.")

    return "UNKNOWN", (
        f"No clear pattern. Clock={clock} MHz, Voltage={volt} mV at last reading. "
        "Review the telemetry below. Common causes: too aggressive undervolt, "
        "power delivery issue, or driver bug unrelated to undervolting.")


def _recommendations(code: str) -> list[str]:
    recs = {
        "UNDERVOLT_TOO_AGGRESSIVE": [
            "1. Raise the voltage offset for your highest-clock threshold by 20-40 mV.",
            "2. Test stability with a 20+ minute stress run at the new offset.",
            "3. Start at -80 mV and step down 10 mV at a time to find your stable floor.",
        ],
        "RAPID_VOLTAGE_SWING": [
            "1. Increase hysteresis from 2 to 4.",
            "2. Space threshold clock values further apart (100 MHz gaps minimum).",
            "3. Increase the poll interval to 0.75s to reduce sensitivity.",
        ],
        "VOLTAGE_INSUFFICIENT_FOR_CLOCK": [
            "1. Make the highest-clock threshold less aggressive (-80 to -100 mV).",
            "2. Consider not undervolting at peak boost at all — the savings are minimal.",
        ],
        "SUSTAINED_HIGH_CLOCK_LOW_VOLTAGE": [
            "1. Reduce undervolt by 20-30 mV on all thresholds above 3000 MHz.",
            "2. Ensure case airflow is adequate — heat increases voltage requirements.",
        ],
        "CRASH_AFTER_VOLTAGE_CHANGE": [
            "1. Increase hysteresis to 3 or 4.",
            "2. Space your threshold clock values further apart to reduce transitions.",
        ],
        "THERMAL": [
            "1. Improve case airflow.",
            "2. Check the GPU fan curve (AMD GPU Tuner Fans page or Adrenalin).",
            "3. Reapply thermal paste if the GPU is older.",
        ],
        "THERMAL_HOTSPOT": [
            "1. Check thermal pads on VRAM/VRM.",
            "2. Reduce the GPU power limit slightly.",
        ],
        "UNKNOWN": [
            "1. Raise all voltage offsets by 20 mV as a safe baseline and retest.",
            "2. Check Windows Event Viewer > System for additional context.",
            "3. Run AMD Adrenalin diagnostics and check for driver updates.",
        ],
        "NO_TELEMETRY": [
            "1. Ensure AMD GPU Tuner was running and monitoring when the crash occurred.",
            "2. The crash may have happened too quickly for any telemetry to be saved.",
        ],
    }
    return recs.get(code, recs["UNKNOWN"])


# ── Report writer ─────────────────────────────────────────────────────────────

def _write_crash_report(log_path: str, crash_no: int, event_id: int,
                        event_msg: str, event_time: str, reason_code: str,
                        explanation: str, telemetry: list[dict],
                        volt_changes: list[dict], config: dict,
                        postmortem: bool = False) -> None:
    sep = "═" * 72
    sep2 = "─" * 72
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "POST-MORTEM (detected on next startup)" if postmortem else "LIVE"

    def wrap(text: str, width: int = 68) -> list[str]:
        words, lines, cur = text.split(), [], ""
        for w in words:
            if len(cur) + len(w) + 1 <= width:
                cur = (cur + " " + w).lstrip()
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    lines = [
        "", sep,
        f"  VOLTSHIFT CRASH REPORT #{crash_no}  [{mode}]",
        f"  Detected  : {ts}",
        f"  Event     : Windows Event ID {event_id}",
        f"  Event msg : {event_msg[:80]}",
        f"  Event time: {event_time}",
        sep,
        "",
        f"  REASON CODE : {reason_code}",
        "",
        "  ANALYSIS",
        f"  {sep2}",
        *[f"  {l}" for l in wrap(explanation)],
        "",
        "  ACTIVE CONFIG AT CRASH TIME",
        f"  {sep2}",
        f"  Poll interval : {config.get('poll_interval_sec', '?')}s",
        f"  Hysteresis    : {config.get('hysteresis_count', '?')} reads",
        f"  Idle offset   : {config.get('idle_offset_mv', '?')} mV",
        "  Thresholds:",
    ]
    for t in sorted(config.get("thresholds", []),
                    key=lambda x: x.get("clock_mhz", 0), reverse=True):
        lines.append(f"    ≥ {t.get('clock_mhz', 0):>5} MHz → {t.get('offset_mv', 0):>+5} mV")

    lines += ["", "  VOLTAGE CHANGE HISTORY (pre-crash)", f"  {sep2}"]
    if volt_changes:
        for c in volt_changes[-20:]:
            ts_c = datetime.fromtimestamp(c.get("ts", 0)).strftime("%H:%M:%S")
            old_s = f"{c['old_mv']:+d}" if c.get("old_mv") is not None else "init"
            new_s = f"{c['new_mv']:+d}" if c.get("new_mv") is not None else "?"
            lines.append(f"  {ts_c}   {old_s} mV → {new_s} mV")
    else:
        lines.append("  No voltage changes recorded.")

    lines += [
        "",
        f"  TELEMETRY SNAPSHOT (last {min(len(telemetry), 30)} samples)",
        f"  {sep2}",
        f"  {'Time':>12}  {'Clock':>8}  {'Voltage':>9}  {'Temp':>6}  {'HotSpot':>8}  {'Power':>7}  {'Fan':>7}",
        f"  {sep2}",
    ]
    for p in telemetry[-30:]:
        t_str = datetime.fromtimestamp(p.get("ts", 0)).strftime("%H:%M:%S.%f")[:-3]
        clk = p.get("clock_mhz", 0)
        v = p.get("voltage_mv")
        temp = p.get("temp_c")
        hot = p.get("hotspot_c")
        pwr = p.get("power_w")
        fan = p.get("fan_rpm")
        v_s = f"{v:>+6} mV" if v is not None else "      ?"
        t_s = f"{temp:>5.0f}°" if temp is not None else "     ?"
        h_s = f"{hot:>6.0f}°" if hot is not None else "      ?"
        p_s = f"{pwr:>5.0f}W" if pwr is not None else "     ?"
        f_s = f"{fan:>4}rpm" if fan is not None else "      ?"
        lines.append(f"  {t_str:>12}  {clk:>6} MHz  {v_s}  {t_s}  {h_s}  {p_s}  {f_s}")

    lines += [
        "",
        "  RECOMMENDATIONS",
        f"  {sep2}",
        *[f"  {r}" for r in _recommendations(reason_code)],
        "",
        sep, "",
    ]

    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── CrashLogger ───────────────────────────────────────────────────────────────

class CrashLogger:
    def __init__(self, config: dict, log_path: Optional[str] = None):
        self.config = dict(config)
        self.log_path = log_path or paths.crash_log_path()
        base_dir = os.path.dirname(os.path.abspath(self.log_path))
        self._telem_path = os.path.join(base_dir, TELEMETRY_FILE)
        self._heartbeat_path = os.path.join(base_dir, HEARTBEAT_FILE)

        self._running = False
        self._crash_count = 0
        self._lock = threading.Lock()
        self._session_start_iso = ""

        self._flight: deque[dict] = deque(maxlen=MAX_TELEMETRY_ROWS)
        self._volt_changes: list[dict] = []

        self.on_crash_detected: Optional[Callable[[int, str, str], None]] = None
        self.on_log_entry: Optional[Callable[[str, str], None]] = None

    @property
    def eventlog_available(self) -> bool:
        return _EVTLOG_OK

    def _log(self, msg: str, level: str = "info") -> None:
        if self.on_log_entry:
            self.on_log_entry(msg, level)

    # ── telemetry I/O ────────────────────────────────────────────────────────

    def _flush_telemetry(self) -> None:
        try:
            data = {
                "session_start": self._session_start_iso,
                "telemetry": list(self._flight),
                "volt_changes": self._volt_changes[-50:],
                "config": self.config,
            }
            tmp = self._telem_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._telem_path)
        except Exception:
            pass

    def _load_last_telemetry(self) -> dict:
        try:
            with open(self._telem_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_heartbeat(self) -> None:
        try:
            with open(self._heartbeat_path, "w", encoding="utf-8") as f:
                json.dump({
                    "pid": os.getpid(),
                    "session_start": self._session_start_iso,
                    "ts": time.time(),
                }, f)
        except Exception:
            pass

    def _delete_heartbeat(self) -> None:
        try:
            if os.path.exists(self._heartbeat_path):
                os.remove(self._heartbeat_path)
        except Exception:
            pass

    def _was_last_session_unclean(self) -> bool:
        return os.path.exists(self._heartbeat_path)

    # ── post-mortem detection ────────────────────────────────────────────────

    def check_previous_session(self) -> None:
        """Call at startup, before this session's heartbeat is written."""
        if not self._was_last_session_unclean():
            return

        last = self._load_last_telemetry()
        if not last:
            self._log("Previous session ended unexpectedly (no telemetry saved)", "warn")
            self._delete_heartbeat()
            return

        session_start = last.get("session_start", "")
        telemetry = last.get("telemetry", [])
        volt_changes = last.get("volt_changes", [])
        config = last.get("config", self.config)

        self._log("Previous session ended unexpectedly — checking for crash events...", "warn")

        if not _EVTLOG_OK:
            self._log("pywin32 not available — cannot query Event Log (pip install pywin32)", "warn")
            self._delete_heartbeat()
            return

        crash_events = _find_crash_events(session_start)
        if not crash_events:
            self._crash_count += 1
            crash_iso = session_start
            if telemetry:
                last_ts = telemetry[-1].get("ts", 0)
                crash_iso = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.0000000Z")
            reason, explanation = analyse_crash(telemetry, volt_changes, crash_iso,
                                                "Driver/process crash")
            _write_crash_report(
                log_path=self.log_path, crash_no=self._crash_count, event_id=0,
                event_msg=("Process killed unexpectedly (no matching Event Log entry — "
                           "TDR may not have been logged)"),
                event_time=crash_iso, reason_code=reason, explanation=explanation,
                telemetry=telemetry, volt_changes=volt_changes, config=config,
                postmortem=True)
            self._log(f"Post-mortem crash report #{self._crash_count} written → {self.log_path}", "info")
            if self.on_crash_detected:
                self.on_crash_detected(self._crash_count, reason, explanation)
        else:
            for evt in crash_events:
                self._crash_count += 1
                event_time = evt.get("TimeCreated", session_start)
                event_msg = evt.get("Message", "Display driver reset")
                event_id = evt.get("EventID", 0)

                reason, explanation = analyse_crash(telemetry, volt_changes,
                                                    event_time, event_msg)
                self._log(f"CRASH #{self._crash_count} (post-mortem) — Event {event_id} — {reason}",
                          "error")
                _write_crash_report(
                    log_path=self.log_path, crash_no=self._crash_count,
                    event_id=event_id, event_msg=event_msg, event_time=event_time,
                    reason_code=reason, explanation=explanation, telemetry=telemetry,
                    volt_changes=volt_changes, config=config, postmortem=True)
                self._log(f"Post-mortem report #{self._crash_count} written: {self.log_path}", "info")
                if self.on_crash_detected:
                    self.on_crash_detected(self._crash_count, reason, explanation)

        self._delete_heartbeat()

    # ── live recording ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._session_start_iso = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.0000000Z")
        self._write_heartbeat()
        self._running = True
        self._log(f"Crash logger active — flight recorder running | Event Log: {_EVTLOG_OK}",
                  "info")

    def stop(self) -> None:
        self._running = False
        self._delete_heartbeat()

    def record(self, clock: int, voltage: Optional[int],
               temp: Optional[float] = None, hotspot: Optional[float] = None,
               power: Optional[float] = None, fan_rpm: Optional[int] = None) -> None:
        """Append one telemetry point; flushes the ring buffer to disk."""
        point = {
            "ts": time.time(),
            "clock_mhz": clock,
            "voltage_mv": voltage,
            "temp_c": temp,
            "hotspot_c": hotspot,
            "power_w": power,
            "fan_rpm": fan_rpm,
        }
        with self._lock:
            self._flight.append(point)
            cutoff = time.time() - 60.0
            self._volt_changes = [c for c in self._volt_changes if c.get("ts", 0) >= cutoff]
        self._flush_telemetry()

    def on_voltage_changed(self, old_mv: Optional[int], new_mv: int) -> None:
        with self._lock:
            self._volt_changes.append({"ts": time.time(), "old_mv": old_mv, "new_mv": new_mv})

    def update_config(self, config: dict) -> None:
        self.config = dict(config)

    @property
    def crash_count(self) -> int:
        return self._crash_count

    def write_session_header(self, gpu_name: str, config: dict) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        thresholds_str = ", ".join(
            f"≥{t['clock_mhz']}MHz→{t['offset_mv']}mV"
            for t in sorted(config.get("thresholds", []),
                            key=lambda x: x.get("clock_mhz", 0), reverse=True))
        header = (
            f"\n{'─' * 72}\n"
            f"  AMD GPU Tuner session started  {ts}\n"
            f"  GPU     : {gpu_name}\n"
            f"  Config  : poll={config.get('poll_interval_sec')}s  "
            f"hyst={config.get('hysteresis_count')}  "
            f"idle={config.get('idle_offset_mv')}mV\n"
            f"  Thresholds: {thresholds_str}\n"
            f"{'─' * 72}\n")
        os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(header)

    def write_session_footer(self, crash_count: int) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        footer = (f"  AMD GPU Tuner session ended  {ts}  —  {crash_count} crash(es) recorded\n"
                  f"{'─' * 72}\n")
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(footer)
