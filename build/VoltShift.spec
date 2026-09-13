# PyInstaller specification for local AMD GPU Tuner builds.
#
# Build from the repo root:
#   py -3.12 -m PyInstaller build/VoltShift.spec
# or use build/build_exe_py312.bat, which also builds the bridge first.
#
# The C++ bridge (voltshift_bridge.exe) is bundled next to the GUI so
# paths.bridge_path() finds it at runtime. PresentMon is fetched separately.

import os
import runpy
from PyInstaller.utils.hooks import collect_data_files

repo_root = os.path.abspath(os.getcwd())
src_dir = os.path.join(repo_root, "src")
bridge_exe = os.path.join(repo_root, "bridge", "build", "Release", "voltshift_bridge.exe")
icon = os.path.join(repo_root, "assets", "icon.ico")
version_info = os.path.join(repo_root, "build", "version_info.txt")

binaries = []
if not os.path.isfile(bridge_exe):
    raise SystemExit("Build the native bridge first: scripts/build_bridge.ps1")
binaries.append((bridge_exe, "."))
app_data = collect_data_files("customtkinter") + [
    (os.path.join(repo_root, "assets", "icon.ico"), "assets"),
    (os.path.join(repo_root, "LICENSE"), "."),
    (os.path.join(repo_root, "docs", "user-guide.md"), "docs"),
    (os.path.join(repo_root, "docs", "THIRD_PARTY_NOTICES.md"), "docs"),
]
app_data += runpy.run_path(os.path.join(repo_root, "scripts", "collect_licenses.py"))["license_data"](repo_root)

a = Analysis(
    [os.path.join(src_dir, "voltshift_gui.py")],
    pathex=[src_dir],
    binaries=binaries,
    datas=app_data,
    hiddenimports=[
        "customtkinter", "win32evtlog", "win32timezone", "psutil",
        # Foreground-window game detection.
        "win32gui", "win32process",
        # numpy backs the Gaussian-process optimiser; sqlite3 backs the
        # knowledge store. Both are required by Auto-Tune, so neither may be
        # excluded from the frozen build.
        "numpy", "sqlite3",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "PIL.ImageQt", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AMD-GPU-Tuner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,           # windowed GUI app
    icon=icon if os.path.isfile(icon) else None,
    version=version_info,
    uac_admin=True,          # voltage writes need elevation
)

# Read-only CLI commands can run without elevating the console executable.
cli_analysis = Analysis(
    [os.path.join(src_dir, "voltshift_cli.py")], pathex=[src_dir],
    binaries=[], datas=[], hiddenimports=["numpy", "sqlite3", "win32evtlog", "win32timezone",
                                         "win32gui", "win32process", "psutil"],
    excludes=["matplotlib", "pytest"], noarchive=False,
)
cli_exe = EXE(
    PYZ(cli_analysis.pure), cli_analysis.scripts, [], exclude_binaries=True,
    name="amd-gpu-tuner-cli", console=True, upx=False,
    icon=icon if os.path.isfile(icon) else None,
    version=version_info,
)

coll = COLLECT(
    exe,
    cli_exe,
    a.binaries,
    a.datas,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=True,
    name="AMD-GPU-Tuner",
)
