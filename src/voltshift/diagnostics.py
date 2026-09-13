"""Read-only, shareable hardware reports. Never constructs a tuning stack."""

from __future__ import annotations

import json
import os
import platform
import tempfile
from datetime import datetime, timezone

from . import APP_NAME, __version__
from .bridgeclient import BridgeClient, BridgeError


def collect_report(bridge: BridgeClient) -> dict:
    """Collect supported readbacks; unavailable sections carry an explicit error.

    Local paths, profiles, process lists, crash logs and learned game history
    are deliberately outside this hardware report.
    """
    report = {
        "schemaVersion": 1,
        "app": APP_NAME,
        "appVersion": __version__,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "system": {"os": platform.system(), "release": platform.release(),
                   "python": platform.python_version(), "machine": platform.machine()},
        "readOnly": True,
        "errors": {},
    }
    for section, reader in (("info", bridge.info), ("caps", bridge.caps),
                            ("tuning", bridge.tuning_get), ("fans", bridge.fans_get),
                            ("metrics", bridge.metrics)):
        try:
            value = reader()
            if not isinstance(value, dict):
                raise BridgeError("Expected an object in the bridge response")
            report[section] = {k: v for k, v in value.items() if k != "driverPath"}
        except BridgeError as exc:
            # A failed call can contain a local executable path. Keep the report
            # portable and give full connection errors to the caller at startup.
            report[section] = {}
            report["errors"][section] = "Readback unavailable"
    report["bridgeVersion"] = bridge.version
    return report


def format_summary(report: dict) -> str:
    info = report.get("info", {})
    metrics = report.get("metrics", {})
    tuning = report.get("tuning", {})
    caps = report.get("caps", {}).get("tuning", {})

    def value(data, key, unit=""):
        item = data.get(key)
        if item is None:
            return "unavailable"
        return f"{item:g}{unit}" if isinstance(item, (float, int)) else f"{item}{unit}"

    supported = [label for key, label in (("manualGfx", "core"), ("manualVram", "memory"),
                                         ("manualFan", "fans"), ("manualPower", "power"))
                 if caps.get(key) is True]
    lines = [f"{APP_NAME} {__version__} | read-only snapshot",
             f"GPU: {info.get('name', 'unavailable')}",
             f"VBIOS: {info.get('bios', {}).get('version') or 'unavailable'}",
             f"Bridge: {report.get('bridgeVersion') or 'unavailable'}",
             f"Supported tuning: {', '.join(supported) or 'none reported'}",
             f"Core clock: {value(metrics, 'clockMhz', ' MHz')}",
             f"Temperature: {value(metrics, 'tempC', ' C')}",
             f"Hotspot: {value(metrics, 'hotspotC', ' C')}",
             f"Measured board power: {value(metrics, 'boardPowerW', ' W')}",
             f"Configured power limit: {value(tuning.get('power', {}), 'powerLimit', '%')}"]
    if report.get("errors"):
        lines.append("Unavailable sections: " + ", ".join(report["errors"]))
    return "\n".join(lines)


def save_report(report: dict, destination: str) -> str:
    """Write an export atomically, leaving an existing file intact on failure."""
    path = os.path.abspath(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                         prefix=".diagnostics-", suffix=".tmp", delete=False) as out:
            temporary = out.name
            json.dump(report, out, indent=2, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return path
