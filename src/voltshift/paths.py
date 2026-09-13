"""Filesystem locations for VoltShift runtime artifacts.

Everything lives next to the executable (frozen build) or the repo root
(running from source), so a portable install keeps its config, profiles,
and crash logs together.
"""

from __future__ import annotations

import os
import sys


def app_dir() -> str:
    override = os.environ.get("AMD_GPU_TUNER_DATA_DIR")
    if override:
        directory = os.path.abspath(os.path.expanduser(override))
        os.makedirs(directory, exist_ok=True)
        return directory
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # src/voltshift/paths.py -> repo root
    source_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if os.path.isfile(os.path.join(source_root, "src", "voltshift_gui.py")):
        return source_root
    directory = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                             "AMD GPU Tuner")
    os.makedirs(directory, exist_ok=True)
    return directory


def bridge_path() -> str:
    """Locate voltshift_bridge.exe.

    Checks, in order: PyInstaller's bundle dir (`_internal/` in a onedir
    build, via sys._MEIPASS), next to the app, and the dev CMake output.
    """
    candidates = []
    override = os.environ.get("AMD_GPU_TUNER_BRIDGE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "voltshift_bridge.exe"))
    candidates += [
        os.path.join(os.path.dirname(sys.executable), "voltshift_bridge.exe"),
        os.path.join(app_dir(), "voltshift_bridge.exe"),
        os.path.join(app_dir(), "_internal", "voltshift_bridge.exe"),
        os.path.join(app_dir(), "bridge", "build", "Release", "voltshift_bridge.exe"),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "bridge",
                                     "build", "Release", "voltshift_bridge.exe")),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]


def config_path() -> str:
    return os.path.join(app_dir(), "voltshift_config.json")


def profiles_dir() -> str:
    path = os.path.join(app_dir(), "profiles")
    os.makedirs(path, exist_ok=True)
    return path


def crash_log_path() -> str:
    return os.path.join(app_dir(), "voltshift_crashes.log")
