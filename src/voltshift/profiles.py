"""Validated, GPU-bound snapshots. Loading a file never writes to hardware.

Hardware application validates the entire document before the first write.
Driver rejections are reported per setting; applying is not transactional.
The dynamic engine configuration is loaded separately and never auto-starts.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

from . import APP_NAME, __version__, paths
from .bridgeclient import BridgeClient, BridgeError
from .engine import EngineConfig, MIN_OFFSET_MV, MAX_OFFSET_MV

PROFILE_FORMAT = 3  # format 2 had no GPU/VBIOS binding
SECTIONS = ("engine", "tuning", "fans", "gfx", "display", "media")
MAX_PROFILE_BYTES = 1024 * 1024
_DISPLAY_TOGGLES = ("freeSync", "vsr", "gpuScaling", "integerScaling", "variBright", "hdcp")
_GFX_FIELDS = {
    "antiLag": {"enabled", "level"},
    "chill": {"enabled", "minFps", "maxFps"},
    "boost": {"enabled", "minResolutionPct"},
    "imageSharpening": {"enabled", "sharpness"},
    "imageSharpenDesktop": {"enabled"},
    "enhancedSync": {"enabled"},
    "vsync": {"mode"},
    "frtc": {"enabled", "fps"},
    "rsr": {"enabled", "sharpness"},
    "afmf": {"enabled", "searchMode", "performanceMode", "fastMotionResponse", "algorithm"},
    "tessellation": {"mode", "level"},
    "antiAliasing": {"mode", "level", "method"},
    "morphologicalAA": {"enabled"},
    "anisotropicFiltering": {"enabled", "level"},
    "fsrUpgrade": {"enabled"},
    "frameGenUpgrade": {"enabled", "ratio"},
}
_COLOR_FIELDS = {"brightness", "contrast", "saturation", "hue", "temperature"}


def _object(value, label: str, allowed: set[str] | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} field names must be text")
    if allowed is not None and value.keys() - allowed:
        raise ValueError(f"Unknown fields in {label}: {', '.join(sorted(value.keys() - allowed))}")
    return value


def _integer(value, label: str, minimum: int = -(2**31), maximum: int = 2**31 - 1) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}")


def _boolean(value, label: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{label} must be true or false")


def _selection(sections, available=SECTIONS) -> set[str]:
    if sections is None:
        return set(available)
    if not isinstance(sections, (set, frozenset, list, tuple)) or any(
            not isinstance(s, str) for s in sections):
        raise ValueError("Sections must be a collection of section names")
    selected = set(sections)
    if not selected or selected - set(SECTIONS):
        raise ValueError("Select at least one known profile section")
    return selected & set(available)


def _feature_state(state, label: str, fields: set[str]) -> None:
    _object(state, label, fields)
    if not state:
        raise ValueError(f"{label} must contain a setting")
    for key, value in state.items():
        if key == "enabled":
            _boolean(value, f"{label}.{key}")
        else:
            _integer(value, f"{label}.{key}", 0)
        if key in {"sharpness", "minResolutionPct"} and value > 100:
            raise ValueError(f"{label}.{key} must not exceed 100")
    if "minFps" in state and "maxFps" in state and state["minFps"] > state["maxFps"]:
        raise ValueError(f"{label}.minFps must not exceed maxFps")


def validate(profile: dict) -> None:
    """Reject malformed or ambiguous data before any file or hardware write."""
    _object(profile, "Profile", set(SECTIONS) | {
        "app", "appVersion", "format", "created", "gpu", "binding"})
    if type(profile.get("format")) is not int or profile["format"] not in (2, PROFILE_FORMAT):
        raise ValueError(f"Unsupported profile format: {profile.get('format')!r}")
    if not set(SECTIONS) & profile.keys():
        raise ValueError("Profile contains no settings sections")
    for key in ("app", "appVersion", "created", "gpu"):
        if key in profile and not isinstance(profile[key], str):
            raise ValueError(f"{key} must be text")
    binding = profile.get("binding")
    if profile["format"] == PROFILE_FORMAT and not binding:
        raise ValueError("Profile is missing its GPU binding")
    if binding is not None:
        _object(binding, "binding", {"name", "vendorId", "deviceId", "revisionId", "vramMb", "bios"})
        if not isinstance(binding.get("name"), str) or not binding["name"].strip():
            raise ValueError("binding.name must identify the GPU")
        for key in ("vendorId", "deviceId", "revisionId"):
            if key in binding and not isinstance(binding[key], str):
                raise ValueError(f"binding.{key} must be text")
        if "vramMb" in binding:
            _integer(binding["vramMb"], "binding.vramMb", 0)
        if "bios" in binding:
            bios = _object(binding["bios"], "binding.bios", {"partNumber", "version", "date"})
            if any(not isinstance(v, str) for v in bios.values()):
                raise ValueError("binding.bios fields must be text")

    if "engine" in profile:
        engine = _object(profile["engine"], "engine", {
            "poll_interval_sec", "hysteresis_count", "idle_offset_mv", "thresholds"})
        poll = engine.get("poll_interval_sec", 0.5)
        try:
            valid_poll = type(poll) in (int, float) and math.isfinite(poll) and poll >= 0.1
        except OverflowError:
            valid_poll = False
        if not valid_poll:
            raise ValueError("engine.poll_interval_sec must be finite and at least 0.1")
        _integer(engine.get("hysteresis_count", 2), "engine.hysteresis_count", 1)
        _integer(engine.get("idle_offset_mv", -100), "engine.idle_offset_mv", MIN_OFFSET_MV, MAX_OFFSET_MV)
        thresholds = engine.get("thresholds", [])
        if not isinstance(thresholds, list):
            raise ValueError("engine.thresholds must be a list")
        clocks = set()
        for point in thresholds:
            _object(point, "engine threshold", {"clock_mhz", "offset_mv"})
            _integer(point.get("clock_mhz"), "threshold.clock_mhz", 0)
            _integer(point.get("offset_mv"), "threshold.offset_mv", MIN_OFFSET_MV, MAX_OFFSET_MV)
            if point["clock_mhz"] in clocks:
                raise ValueError("Engine thresholds must have distinct clocks")
            clocks.add(point["clock_mhz"])

    tuning = _object(profile.get("tuning", {}), "tuning", {
        "voltageMv", "minFreqMhz", "maxFreqMhz", "maxFreqMode", "vramMaxMhz",
        "memoryTiming", "powerLimitPct", "tdcAmps"})
    for key, value in tuning.items():
        if key == "maxFreqMode":
            if value not in ("absolute", "offset") or "maxFreqMhz" not in tuning:
                raise ValueError("tuning.maxFreqMode must describe a saved maximum clock as absolute or offset")
            continue
        if key in {"memoryTiming", "tdcAmps"} and value is None and profile["format"] == 2:
            continue  # older captures emitted null for an unreadable optional value
        if key == "voltageMv":
            _integer(value, "tuning.voltageMv (offset)", MIN_OFFSET_MV, MAX_OFFSET_MV)
        else:
            _integer(value, f"tuning.{key}", -(2**31) if key in {"powerLimitPct", "maxFreqMhz"} else 0)
    if tuning.get("maxFreqMode") == "absolute" and tuning["maxFreqMhz"] < 0:
        raise ValueError("An absolute maximum core clock cannot be negative")
    if (tuning.get("maxFreqMode") == "absolute" and "minFreqMhz" in tuning
            and tuning["minFreqMhz"] > tuning["maxFreqMhz"]):
        raise ValueError("Minimum core clock must not exceed maximum core clock")

    fans = _object(profile.get("fans", {}), "fans", {"curve", "zeroRpm"})
    if "zeroRpm" in fans:
        _boolean(fans["zeroRpm"], "fans.zeroRpm")
    curve = fans.get("curve", [])
    if not isinstance(curve, list):
        raise ValueError("fans.curve must be a list")
    previous = None
    for point in curve:
        _object(point, "fan point", {"tempC", "speedPct"})
        _integer(point.get("tempC"), "fan.tempC", 0, 150)
        _integer(point.get("speedPct"), "fan.speedPct", 0, 100)
        if previous and (point["tempC"] <= previous["tempC"] or point["speedPct"] < previous["speedPct"]):
            raise ValueError("Fan temperatures must increase and fan speeds must not decrease")
        previous = point

    for feature, state in _object(profile.get("gfx", {}), "gfx", set(_GFX_FIELDS)).items():
        _feature_state(state, f"gfx.{feature}", _GFX_FIELDS[feature])
    displays = profile.get("display", [])
    if not isinstance(displays, list):
        raise ValueError("display must be a list")
    identifiers = set()
    for saved in displays:
        _object(saved, "display entry", set(_DISPLAY_TOGGLES) | {
            "uniqueId", "name", "scalingMode", "colorDepth", "pixelFormat", "customColor"})
        uid = saved.get("uniqueId")
        if uid is not None and (type(uid) not in (str, int) or not str(uid).strip()):
            raise ValueError("Display uniqueId must be a nonempty string or integer")
        if uid is not None:
            if str(uid) in identifiers:
                raise ValueError("Display identifiers must be unique")
            identifiers.add(str(uid))
        if saved.get("name") is not None and not isinstance(saved["name"], str):
            raise ValueError("Display name must be text")
        for feature in _DISPLAY_TOGGLES:
            if feature in saved:
                _feature_state(saved[feature], f"display.{feature}", {"enabled"})
        for feature, field in (("scalingMode", "mode"), ("colorDepth", "value"), ("pixelFormat", "value")):
            if feature in saved:
                _feature_state(saved[feature], f"display.{feature}", {field})
        for channel, value in _object(saved.get("customColor", {}), "customColor", _COLOR_FIELDS).items():
            _integer(value, f"customColor.{channel}")
    for feature, state in _object(profile.get("media", {}), "media", {
            "videoUpscale", "videoSuperResolution"}).items():
        _feature_state(state, f"media.{feature}", {"enabled", "sharpness"})


def engine_config(profile: dict) -> EngineConfig:
    """Read engine settings for editing without touching or starting the GPU."""
    validate(profile)
    if "engine" not in profile:
        raise ValueError("This profile does not contain engine settings")
    return EngineConfig.from_dict(profile["engine"])


def _binding(info: dict) -> dict:
    result = {key: info[key] for key in ("name", "vendorId", "deviceId", "revisionId", "vramMb")
              if info.get(key) not in (None, "")}
    result.setdefault("name", "unknown")
    bios = {key: info.get("bios", {}).get(key) for key in ("partNumber", "version", "date")
            if info.get("bios", {}).get(key)}
    if bios:
        result["bios"] = bios
    return result


def _check_binding(bridge: BridgeClient, profile: dict) -> list[str]:
    binding = profile.get("binding")
    if binding is None:
        name = profile.get("gpu")
        if name and name != "unknown" and bridge.info().get("name") != name:
            raise ValueError(f"Profile was saved for a different GPU: {name}")
        return ["warning: legacy profile has no GPU/VBIOS binding; re-save it on this GPU"]
    current = _binding(bridge.info())
    for key, expected in binding.items():
        if key == "bios":
            for field, value in expected.items():
                if value and current.get("bios", {}).get(field) != value:
                    raise ValueError(f"Profile VBIOS mismatch ({field}); no settings applied")
        elif expected not in (None, "") and current.get(key) != expected:
            raise ValueError(f"Profile GPU mismatch ({key}); no settings applied")
    return []


def _capture_gfx(bridge: BridgeClient) -> dict:
    """Reduce gfx.get output to just the writable state (drop ranges/support)."""
    captured = {}
    for feature, state in bridge.gfx_get().items():
        if feature not in _GFX_FIELDS or not state.get("supported"):
            continue
        keep = {k: v for k, v in state.items()
                if k in _GFX_FIELDS[feature]}
        if keep:
            captured[feature] = keep
    return captured


def _capture_displays(bridge: BridgeClient) -> list[dict]:
    captured = []
    for display in bridge.display_list():
        index = display["index"]
        state = bridge.display_get(index)
        entry: dict = {"uniqueId": display.get("uniqueId"), "name": display.get("name")}
        for feature in ("freeSync", "vsr", "gpuScaling", "integerScaling", "variBright", "hdcp"):
            if state.get(feature, {}).get("supported"):
                entry[feature] = {"enabled": state[feature]["enabled"]}
        if state.get("scalingMode", {}).get("supported"):
            entry["scalingMode"] = {"mode": state["scalingMode"]["mode"]}
        if state.get("colorDepth", {}).get("supported"):
            entry["colorDepth"] = {"value": state["colorDepth"]["value"]}
        if state.get("pixelFormat", {}).get("supported"):
            entry["pixelFormat"] = {"value": state["pixelFormat"]["value"]}
        color = {}
        for channel, cstate in state.get("customColor", {}).items():
            if cstate.get("supported"):
                color[channel] = cstate["value"]
        if color:
            entry["customColor"] = color
        captured.append(entry)
    return captured


def capture(bridge: BridgeClient, engine_config: EngineConfig,
            sections: Optional[set[str]] = None) -> dict:
    """Snapshot current state into a profile dict."""
    sections = _selection(sections)
    info = bridge.info()
    profile: dict = {
        "app": APP_NAME,
        "appVersion": __version__,
        "format": PROFILE_FORMAT,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gpu": info.get("name", "unknown"),
        "binding": _binding(info),
    }

    if "engine" in sections:
        profile["engine"] = engine_config.to_dict()

    if "tuning" in sections:
        tuning = bridge.tuning_get()
        section: dict = {}
        gfx = tuning.get("gfx", {})
        if "unsupported" not in gfx:
            # A positive absolute voltage cannot be replayed through the
            # bridge's offset setter. Capture only a confirmed offset range.
            voltage_range = gfx.get("voltageRange", {})
            if ("voltageMv" in gfx and voltage_range.get("min", 0) < 0
                    and voltage_range.get("max", 1) <= 0):
                section["voltageMv"] = gfx["voltageMv"]
            if "minFreqMhz" in gfx and gfx.get("minFreqRange", {}).get("step", 1) > 0:
                section["minFreqMhz"] = gfx["minFreqMhz"]
            if "maxFreqMhz" in gfx and gfx.get("maxFreqRange", {}).get("step", 1) > 0:
                section["maxFreqMhz"] = gfx["maxFreqMhz"]
                if "min" in gfx.get("maxFreqRange", {}):
                    section["maxFreqMode"] = "absolute" if gfx["maxFreqRange"]["min"] > 0 else "offset"
        vram = tuning.get("vram", {})
        if "unsupported" not in vram:
            if "maxFreqMhz" in vram:
                section["vramMaxMhz"] = vram["maxFreqMhz"]
            if vram.get("timingSupported") and vram.get("timing") is not None:
                section["memoryTiming"] = vram["timing"]
        power = tuning.get("power", {})
        if "unsupported" not in power:
            if "powerLimit" in power:
                section["powerLimitPct"] = power["powerLimit"]
            if power.get("tdcSupported") and power.get("tdcLimit") is not None:
                section["tdcAmps"] = power["tdcLimit"]
        profile["tuning"] = section

    if "fans" in sections:
        try:
            fans = bridge.fans_get()
            section = {"curve": fans.get("curve", [])}
            if fans.get("zeroRpmSupported"):
                section["zeroRpm"] = fans.get("zeroRpm")
            profile["fans"] = section
        except BridgeError:
            pass

    if "gfx" in sections:
        profile["gfx"] = _capture_gfx(bridge)

    if "display" in sections:
        profile["display"] = _capture_displays(bridge)

    if "media" in sections:
        media = bridge.media_get()
        section = {}
        for feature in ("videoUpscale", "videoSuperResolution"):
            state = media.get(feature, {})
            if state.get("supported"):
                keep = {k: v for k, v in state.items() if k in ("enabled", "sharpness")}
                section[feature] = keep
        profile["media"] = section

    validate(profile)
    return profile


def apply(bridge: BridgeClient, profile: dict,
          sections: Optional[set[str]] = None) -> list[str]:
    """Apply selected hardware sections; validate everything before writing.

    Engine configuration is deliberately returned as a notice, never started
    or persisted here. Use engine_config() to load it for editing.
    """
    validate(profile)
    selected = _selection(sections, set(profile) & set(SECTIONS))
    if not selected:
        raise ValueError("None of the selected sections are present in this profile")
    log: list[str] = []
    if selected - {"engine"}:
        log.extend(_check_binding(bridge, profile))
    if "engine" in selected:
        log.append("skipped: engine configuration (use Load engine; starting the engine is a separate action)")
    active = {key: profile[key] for key in selected if key != "engine"}
    tuning = active.get("tuning", {})
    if "maxFreqMode" in tuning:
        clock_range = bridge.tuning_get().get("gfx", {}).get("maxFreqRange", {})
        if "min" not in clock_range:
            raise ValueError("Cannot verify the GPU's maximum clock semantics; no settings applied")
        current_mode = "absolute" if clock_range["min"] > 0 else "offset"
        if tuning["maxFreqMode"] != current_mode:
            raise ValueError("Profile maximum clock uses different units on this GPU; no settings applied")

    # Resolve display identities before writes, never using an absent ID as
    # a key: several monitors may report no identity or duplicate IDs.
    current: dict[str, list[int]] = {}
    if active.get("display"):
        for display in bridge.display_list():
            uid = display.get("uniqueId")
            if uid is not None and str(uid).strip():
                current.setdefault(str(uid), []).append(display["index"])

    def attempt(label: str, fn, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
            log.append(f"applied: {label}")
        except BridgeError as exc:
            log.append(f"skipped: {label} ({exc})")

    if "voltageMv" in tuning:
        attempt("voltage offset", bridge.set_voltage_offset, tuning["voltageMv"])
    if "minFreqMhz" in tuning or "maxFreqMhz" in tuning:
        attempt("core clocks", bridge.set_core_clocks,
                tuning.get("minFreqMhz"), tuning.get("maxFreqMhz"))
    if "vramMaxMhz" in tuning:
        attempt("VRAM max clock", bridge.set_vram_max, tuning["vramMaxMhz"])
    if tuning.get("memoryTiming") is not None:
        attempt("memory timing", bridge.set_memory_timing, tuning["memoryTiming"])
    if "powerLimitPct" in tuning:
        attempt("power limit", bridge.set_power_limit, tuning["powerLimitPct"])
    if tuning.get("tdcAmps") is not None:
        attempt("TDC limit", bridge.set_tdc, tuning["tdcAmps"])

    fans = active.get("fans", {})
    if fans.get("curve"):
        attempt("fan curve", bridge.set_fan_curve, fans["curve"])
    if fans.get("zeroRpm") is not None:
        attempt("ZeroRPM", bridge.set_zero_rpm, fans["zeroRpm"])

    for feature, state in active.get("gfx", {}).items():
        attempt(f"gfx {feature}", bridge.gfx_set, feature, **state)

    for saved in active.get("display", []):
        uid = saved.get("uniqueId")
        matches = current.get(str(uid), []) if uid is not None else []
        label = saved.get("name") or "display"
        if len(matches) != 1:
            log.append(f"skipped: display '{label}' is not uniquely identified or connected")
            continue
        index = matches[0]
        for feature in ("freeSync", "vsr", "gpuScaling", "integerScaling", "variBright", "hdcp"):
            if feature in saved:
                attempt(f"{label} {feature}", bridge.display_set, index, feature,
                        enabled=saved[feature]["enabled"])
        if "scalingMode" in saved:
            attempt(f"{label} scaling mode", bridge.display_set, index, "scalingMode",
                    mode=saved["scalingMode"]["mode"])
        if "colorDepth" in saved:
            attempt(f"{label} color depth", bridge.display_set, index, "colorDepth",
                    value=saved["colorDepth"]["value"])
        if "pixelFormat" in saved:
            attempt(f"{label} pixel format", bridge.display_set, index, "pixelFormat",
                    value=saved["pixelFormat"]["value"])
        if saved.get("customColor"):
            attempt(f"{label} custom color", bridge.display_set, index, "customColor",
                    **saved["customColor"])

    for feature, state in active.get("media", {}).items():
        attempt(f"media {feature}", bridge.media_set, feature, **state)

    if not any(line.startswith(("applied:", "skipped:")) for line in log):
        log.append("skipped: selected sections contain no writable settings")
    return log


# ── file I/O ──────────────────────────────────────────────────────────────────

def save(profile: dict, name: str) -> str:
    validate(profile)
    if not isinstance(name, str):
        raise ValueError("Profile name must be text")
    safe = "".join(c for c in name if c.isalnum() or c in " -_").strip()
    if not safe or len(safe) > 120 or safe != name.strip():
        raise ValueError("Enter a profile name with 1 to 120 letters, numbers, spaces, hyphens or underscores")
    if safe.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("This profile name is reserved by Windows")
    payload = json.dumps(profile, indent=2, allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError("Profile exceeds the 1 MiB size limit")
    path = os.path.join(paths.profiles_dir(), f"{safe}.json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=os.path.dirname(path), prefix=".profile-",
                                         suffix=".tmp", delete=False) as f:
            temporary = f.name
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.remove(temporary)
    return path


def parse(content: str) -> dict:
    """Parse file or editor JSON with identical duplicate-key and size checks."""
    if len(content.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError("Profile exceeds the 1 MiB size limit")

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"Duplicate JSON field: {key}")
            value[key] = item
        return value

    def invalid_constant(value):
        raise ValueError(f"Invalid JSON number: {value}")

    try:
        profile = json.loads(content, object_pairs_hook=pairs,
                             parse_constant=invalid_constant)
    except RecursionError as exc:
        raise ValueError("Profile JSON is nested too deeply") from exc
    validate(profile)
    return profile


def load(path: str) -> dict:
    """Read and validate a profile without any bridge access."""
    with open(path, "rb") as f:
        content = f.read(MAX_PROFILE_BYTES + 1)
    if len(content) > MAX_PROFILE_BYTES:
        raise ValueError("Profile exceeds the 1 MiB size limit")
    try:
        return parse(content.decode("utf-8-sig"))
    except UnicodeError as exc:
        raise ValueError("Profile is not a valid UTF-8 JSON document") from exc


def list_profiles() -> list[str]:
    directory = paths.profiles_dir()
    return sorted(
        os.path.join(directory, f) for f in os.listdir(directory)
        if f.lower().endswith(".json") and os.path.isfile(os.path.join(directory, f))
    )
