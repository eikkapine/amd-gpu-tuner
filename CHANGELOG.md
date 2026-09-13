# Changelog

User-facing changes are recorded here. Hardware support remains dependent on the GPU, VBIOS and installed AMD driver.

## 2.1.0 — 2026-09-13

### Changed

- Renamed the public project to **AMD GPU Tuner**. Existing `voltshift` Python imports, bridge naming and runtime data remain compatible.
- Reworked the README and added a practical user guide covering control semantics, immediate actions, persistence, tuning workflows and troubleshooting.
- Added contribution guidance, a security reporting policy, and structured issue and pull-request templates.
- Separated pending tuning edits from current readback, with interface-aware labels and controls driven by reported support and step sizes.
- Deferred GUI recovery until an explicit automatic-tuning start, keeping initial monitoring from changing tuning settings.

### Added

- Installable `amd-gpu-tuner` and `amd-gpu-tuner-gui` commands, alongside the compatibility entry points.
- Read-only status output and locally saved JSON diagnostics for support and scripting.
- Validated format-3 profiles with GPU/VBIOS binding, section selection, offline inspection and editing, and compatibility handling for older profiles.
- Windows Python test and native-bridge build automation, with a pinned ADLX revision for fresh builds and dependency notices for local packaging.

### Fixed

- Verification now checks that test writes are restored and aborts when restoration fails instead of caching a successful control test.
- Improved handling of failed tuning writes, cancellation and baseline restoration in automatic tuning.
- Preserved App Boost recovery state after partial writes and failed restores, with retries before further application.
- Kept missing telemetry values distinct from real zero readings in measurement and scoring.
- Restricted Dynamic Voltage to validated `MGT2_1` offset ranges and exact driver steps, rejecting interfaces where repeated writes could accumulate voltage changes.
- Fixed a GUI widget-name collision that caused recursive errors during shutdown.

## 2.0.0

The preceding VoltShift series introduced the automatic tuning stack: paired workload measurements, benchmark-result tuning, an adaptive governor, control verification, local learned settings and recovery journals. It also retained manual tuning, profiles, graphics and display controls, fan curves and Dynamic Voltage from the earlier tool.

This historical summary describes the implemented feature groups; it is not a claim that every control was validated on every GPU or that automatic tuning can guarantee stability.
