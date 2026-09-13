# Contributing to AMD GPU Tuner

Useful contributions include reproducible bug reports, hardware capability reports, clearer documentation and focused fixes. For a substantial feature, open an issue describing the user problem and the ADLX interface that could support it before building a large change.

## Development setup

Use a Windows source checkout with Python 3.12. Native changes also need CMake and Visual Studio's C++ build tools. See the [README](README.md#install-from-source) for installation.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,build]"
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\build_bridge.ps1
```

The Python package remains `voltshift`; public commands use `amd-gpu-tuner`. Preserve existing settings filenames and profile compatibility unless a change includes an explicit migration.

## Repository map

| Path | Responsibility |
| --- | --- |
| `src/voltshift/bridgeclient.py` | Persistent native-bridge connection and typed command helpers |
| `bridge/src/` | C++ ADLX commands; newline-delimited JSON transport |
| `src/voltshift/gui/` | CustomTkinter desktop interface |
| `src/voltshift/cli.py` | CLI parsing and command workflows |
| `src/voltshift/optimizer/` | Search, objectives, validation and tuning sessions |
| `src/voltshift/telemetry/` | Hardware samples and frame collectors |
| `src/voltshift/watchdog.py`, `stability.py`, `knowledge.py` | Recovery records, fault signals and learned state |
| `tests/` | Unit and integration tests using simulated hardware |

## Change expectations

- Keep hardware writes explicit. Read-only commands must not trigger tuning, recovery, control verification or downloads.
- Use ADLX capability and range information. Handle unsupported controls and failed reads without substituting plausible-looking data.
- Keep absolute targets distinct from offsets across ADLX interface generations. Test repeated application and partial failure for write-path changes.
- Keep rollback and stop behavior observable. A failed restore must not be reported as successful.
- Add focused regression coverage for changed behavior. Fake bridge tests should assert what was written and what was restored, not merely that a method was called.
- Keep documentation grounded in implemented behavior. A successful driver write or a benchmark sample is not a stability guarantee.
- Preserve upstream license notices and record third-party sources. A public repository without a license does not grant permission to copy its implementation or assets.

## Validation

Run the relevant tests while iterating, then the complete Python suite before opening a pull request. Compile the native bridge for C++ changes. For GUI changes, include an actual screenshot of the affected state and inspect it for clipping, readable labels and unsupported-control behavior.

Read-only hardware checks such as `amd-gpu-tuner status --json` can establish connectivity and reported capabilities. They do not establish that tuning writes, recovery or stability work across GPU families. State exactly which GPU and driver were tested and whether any writes were exercised. Do not run tuning experiments on another person's active system without authorization.

Do not commit personal profiles, configuration, logs, knowledge databases, downloaded executables, ADLX checkouts or generated build output. Review diagnostic attachments for identifying paths and hardware IDs.

## Pull requests and bug reports

Use the repository templates. Explain the observed problem, the resulting behavior and the checks actually run. Note limitations plainly, especially if hardware coverage is unavailable. Keep unrelated refactors out of a focused fix.

For security issues, use [SECURITY.md](SECURITY.md). For ordinary bugs, include the app version, GPU, driver, Windows version, reproduction steps and a reviewed diagnostics report when useful.

Original contributions are provided under the project's [MIT license](LICENSE). AMD SDK components and other dependencies retain their own licenses; consult [third-party notices](docs/THIRD_PARTY_NOTICES.md) before redistributing a compiled bridge.
