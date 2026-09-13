"""License data for local PyInstaller builds, from the installed distributions."""

from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import json
import sys


def license_data(repo_root):
    data = []
    versions = {"Python": sys.version.split()[0]}
    for package in ("customtkinter", "darkdetect", "packaging", "psutil", "pywin32",
                    "numpy", "pillow", "pyinstaller"):
        try:
            installed = distribution(package)
        except PackageNotFoundError:
            if package == "pillow":
                continue  # optional image support, absent from some clean installs
            raise
        versions[package] = installed.version
        for entry in installed.files or ():
            path = Path(entry)
            if path.suffix.lower() in {".py", ".pyc"}:
                continue
            if not any(word in path.name.lower() for word in ("license", "copying", "notice")):
                continue
            source = Path(installed.locate_file(entry)).resolve()
            if source.is_file():
                relative = Path(*(part for part in path.parent.parts if part not in {"..", "."}))
                data.append((str(source), str(Path("licenses") / package / relative)))
    python_root = Path(sys.base_prefix)
    for source in [python_root / "LICENSE.txt", *list((python_root / "tcl").glob("*/license.terms"))]:
        if source.is_file():
            data.append((str(source), str(Path("licenses") / "python" / source.parent.name)))
    root = Path(repo_root)
    manifest = root / "build" / "work" / "dependency-versions.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(versions, indent=2) + "\n", encoding="utf-8")
    data.append((str(manifest), "licenses"))
    sdk_license = root / "third_party" / "ADLX" / "ADLX SDK License Agreement.pdf"
    if not sdk_license.is_file():
        raise SystemExit("ADLX SDK license agreement is missing")
    data.append((str(sdk_license), "licenses/ADLX"))
    for source in (root / "docs" / "licenses").glob("*"):
        if source.is_file():
            data.append((str(source), "licenses"))
    return data
