# AMD GPU Tuner user guide

This guide covers version 2.1.0. AMD GPU Tuner was previously named VoltShift; the Python package, native bridge and existing settings filenames retain `voltshift` for compatibility.

## Installation and first launch

Follow the [source installation steps](../README.md#install-from-source). Run commands from the repository root. Activate the virtual environment with `.\.venv\Scripts\Activate.ps1`, or use the full `.\.venv\Scripts\amd-gpu-tuner.exe` path if PowerShell prevents activation.

Start with these commands:

```powershell
amd-gpu-tuner --version
amd-gpu-tuner status
amd-gpu-tuner status --json
```

Status reads the selected GPU, driver capabilities, current tuning and telemetry. It does not start a tuning session, verify controls, restore a recovery journal or download a frame collector. A read-only query may still fail if the driver or native bridge is unavailable.

For the GUI, run `amd-gpu-tuner-gui` from an elevated terminal. Opening the app starts monitoring without applying tuning or restoring a recovery journal. Recovery and control verification are deferred until you explicitly start an automatic tuning controller. The GUI can fetch a missing frame collector unless automatic fetching is disabled.

### Building a local executable

The repository includes a PyInstaller build script:

```powershell
.\build\build_exe.ps1
```

Use Python 3.12 and the same native build prerequisites as the source installation. The output is `build\dist\AMD-GPU-Tuner\`, containing the GUI `AMD-GPU-Tuner.exe` and console `amd-gpu-tuner-cli.exe` alongside their dependencies. The console executable can run read-only commands without requesting elevation. Keep the complete directory together when moving it.

Local builds are unsigned and the automatic tuning features are experimental. The [third-party notices](THIRD_PARTY_NOTICES.md) describe separate AMD SDK terms that must be addressed before distributing a compiled bridge. Building locally does not establish hardware stability or imply a published portable release.

## Compatibility and control meaning

The application asks ADLX which interfaces, controls and ranges are available. One supported control does not establish support for the others. A disabled control may reflect the GPU, driver, VBIOS or display connection.

The bridge selects the first discrete AMD GPU reported by ADLX, or the first reported GPU if no discrete adapter is available. The name in Dashboard and `status` identifies the target. There is no user-facing GPU selector. Per-display selection is available separately.

Tuning values are not measured sensor values. A power limit is a configured budget; board power is consumption at the sampling instant. A target clock can differ from the live clock because workload, power and thermal limits affect operation.

The ADLX interface also changes the meaning of some fields:

| Interface | Meaning used by this app |
| --- | --- |
| `MGT2_1` | GPU voltage and maximum core frequency are offsets from stock. Zero represents no offset. Minimum core frequency is not exposed by this interface. |
| `MGT2` | GPU voltage readback is an absolute value. The application converts absolute targets for the bridge's delta-based write path. |
| `MGT1` | Legacy voltage/frequency-curve interface. Available controls and voltage behavior differ; use reported capabilities instead of assuming the newer interface's semantics. |

The bridge's low-level `tune voltage` command takes an offset; use `tune get` to inspect the interface before writing. Do not copy raw voltage or clock values from another GPU, or treat an accepted range as a stability guarantee.

### Control verification writes to the GPU

```powershell
amd-gpu-tuner verify
amd-gpu-tuner verify --force
```

Verification makes a small test change, reads it back and attempts to restore the previous value, checking the restoration readback. A failed or ignored restore aborts verification rather than caching success. Verification determines whether the driver responds to a control; it is not a stress test. Results are cached for the card. Use `--force` to retest after a driver or hardware change when needed.

Automatic tuning and the adaptive governor use verification before their first tuning session. Use `status` or `diagnostics` when you only want to inspect support.

## What applies immediately

| Action | Behavior |
| --- | --- |
| Tuning sliders and memory-timing selection | Edit pending values; the relevant **Apply** button writes them. |
| Fan curve points | Edit pending points; **Apply curve** writes them. **Reload** reads the current curve again. |
| ZeroRPM toggle | Writes the setting immediately. |
| Graphics and Display controls | Toggles, menus and parameter changes write the corresponding driver setting. |
| Save profile | Captures current driver readback and the engine configuration. Does not apply pending tuning or fan-curve edits. |
| Apply profile / `profile load` | Writes the saved settings immediately and reports applied or skipped sections. |
| Start Auto-Tune, Adaptive, Dynamic Voltage or App Boost | Starts a controller that can write tuning settings. |
| `verify` | Writes small test changes and attempts to restore them. |
| `reset` / **Reset all tuning to factory defaults** | Immediately asks ADLX to restore factory tuning. |

Use one active tuning controller at a time. A profile or manual change made while a controller runs can be replaced by that controller's next write. Another GPU tuning application can also change the same driver state.

### Stopping, closing and recovery

Resetting *tuning* is distinct from resetting all graphics and display preferences.

| Workflow | Normal completion or stop |
| --- | --- |
| Manual tuning and profile application | Settings remain applied until changed or reset; do not depend on app exit to undo them. |
| Auto-Tune / benchmark tuning | A confirmed winning configuration can remain applied. Cancellation or a rejected search attempts to restore its captured baseline. |
| Adaptive | Stopping requests restoration of the governor's captured baseline. |
| Dynamic Voltage | Stopping requests factory tuning reset. |
| App Boost | When the watched apps exit or the watcher stops, it attempts to restore the power/clock values it saved. |
| GUI close | Stops active controllers before disconnecting the bridge. Each controller follows its own restoration behavior. |

A system hang, driver failure or forced termination can prevent cleanup. Recovery journals help identify interrupted automatic tuning and attempt restoration at the next tuning-capable startup; they are not a guarantee that a driver reset can be intercepted. If tuning remains applied, use `amd-gpu-tuner reset` or **AMD Software → Performance → Tuning → Reset**. Review the current driver state after a reboot or driver update.

## Manual tuning and fans

1. Save a baseline profile and record the workload you will compare.
2. Open **Tuning**. Confirm whether the voltage and clock controls are absolute values or offsets for this interface.
3. Change one supported setting by a small amount, then use that section's **Apply** button.
4. Check **Logs** for an error and read the setting again. Test a repeatable workload before making the next change.
5. Inspect frame pacing, temperature, hotspot and power; a lower requested voltage or higher clock alone does not establish a better result.

**Fans** edits the curve exposed by the AMD driver. Its point count and ranges come from the driver. This is not mVolt's software fan-curve controller. ZeroRPM is separate and only available when supported. Reload the fan state after applying to inspect the result.

Read-only CLI inspection:

```powershell
amd-gpu-tuner tune get
amd-gpu-tuner tune fans
amd-gpu-tuner gfx get
amd-gpu-tuner display list
amd-gpu-tuner display get 0
```

Use each command's `--help` for the setter syntax. Display indexes come from `display list`; do not assume an index stays fixed after reconnecting a monitor.

## Profiles

Profiles are JSON snapshots of the current supported settings: tuning, fans, graphics, displays and multimedia, together with an engine-configuration section.

```powershell
amd-gpu-tuner profile save "baseline"
amd-gpu-tuner profile list
amd-gpu-tuner profile inspect .\profiles\baseline.json
amd-gpu-tuner profile load .\profiles\baseline.json
amd-gpu-tuner profile save "cooling" --sections fans
amd-gpu-tuner profile load .\profiles\cooling.json --sections fans
```

Saving reads the driver's current state. Pending slider edits are not captured. `profile list` and `profile inspect` work without a GPU connection; inspect validates the document and prints its JSON without applying it. The GUI also offers profile preview and section selection.

New format-3 profiles record GPU and VBIOS identity. A mismatch is rejected before settings are applied. Legacy format-2 profiles remain supported with identity checks where available and a warning to re-save them. Entire documents are validated before the first write, but applying is not transactional: driver errors can still leave only some settings applied.

Applying changes only the selected, present sections; omitted settings remain unchanged. Unavailable settings and disconnected displays are reported as skipped. CLI loading returns a nonzero exit code when settings were skipped. Check the result log and driver readback rather than assuming that the complete profile was applied.

The GUI's **Open** action loads a profile into **Review & edit**; **Save edits** validates and saves the document without applying it. **Load engine** updates the Dynamic Voltage editor without starting its controller. **Apply selected** writes only the selected hardware sections. Applying a driver profile does not start the Dynamic Voltage engine; use its page or `run --config` to start it explicitly. Displays are matched using the saved display identity rather than only their list position.

After changing GPU or driver, inspect a profile before applying it. A profile is a configuration record, not proof that its settings remain stable.

## Auto-Tune

Use Auto-Tune with a reasonably steady running workload. Keep resolution, frame caps, scene, background load and cooling conditions as consistent as practical.

1. Start the workload and check that frame data is associated with the intended application.
2. Open **Auto-Tune** and choose a goal.
3. Start the session. Control verification may briefly write and restore test values before trials begin.
4. Keep the workload running and inspect the trial log. Stop if the workload changes enough to make the comparison unrepresentative.
5. Read the final report. Test any retained settings over longer sessions and additional workloads.

| Goal | Preference |
| --- | --- |
| `balanced` | Frame rate and pacing, with power and thermal costs included |
| `max_fps` | Frame rate and lows, with less emphasis on efficiency |
| `efficiency` | Frames per watt, allowing some frame-rate tradeoff |
| `silent` | Lower power, temperature and fan speed while considering frame pacing |

```powershell
amd-gpu-tuner autotune --goal balanced --no-download
amd-gpu-tuner autotune --goal efficiency --trials 10 --window 10 --pairs 2 --no-download
```

Candidates come from driver-reported controls, subject to the optimizer's range, movement and learned-failure checks. Each trial alternates candidate and baseline measurement windows. The winner is measured again before it is retained. These checks reduce some measurement errors; scene changes and short windows can still produce misleading results.

Without usable frame data the score can use hardware readings instead. Such a result cannot demonstrate improved FPS or frame pacing. **Max Benchmark Score** is also exposed as a goal preset; for complete 3DMark result-file comparisons, use the separate `benchtune` workflow below.

## Adaptive mode and App Boost

The adaptive governor associates frame-presenting applications with locally learned settings. It can ramp toward a learned configuration and optionally try small changes during steady workload phases.

```powershell
amd-gpu-tuner adaptive --goal balanced --probes 0 --no-download
```

`--probes 0` disables live experiments, but it still permits learned-profile application and startup control verification. A nonzero probe budget permits tuning changes while the application is running. Stop with Ctrl+C in the CLI or the page's stop control in the GUI.

**App Boost** is a separate, simpler process watcher. It applies configured power and optional maximum-clock settings while an executable from your list is running, then attempts to restore the previous values. It does not evaluate whether those changes improved the game.

## Benchmark tuning

```powershell
amd-gpu-tuner benchtune --test TimeSpy
```

The command applies a candidate, then asks you to start a 3DMark run manually. It watches `Documents\3DMark` for a new `.3dmark-result` file and reads the supported score fields. The objective prefers graphics score over a combined CPU/GPU score.

Use the same benchmark test, preset and settings throughout a session. Export or save each result where the watcher expects it. A missing result is not evidence of poor GPU performance; inspect the command's timeout and result-file handling. The default minimum accepted gain is 0.4%, configurable with `--min-gain`; this threshold is not a measured noise guarantee for your system. The selected winner is run again for confirmation.

The application does not purchase, install or automatically launch 3DMark. Use `benchtune --help` for trial count, timeout, gain threshold and seed options.

## Dynamic Voltage

Dynamic Voltage uses live core-clock thresholds to choose a voltage offset. It checks thresholds from highest to lowest and uses the first one reached; below every threshold it uses the idle offset. Hysteresis requires the new target to appear in several consecutive polls before a change is requested.

This controller requires ADLX's `MGT2_1` interface and a valid driver-reported voltage-offset range with an upper bound of zero or less. It rejects `MGT1` and `MGT2`: repeatedly applying their relative voltage writes could accumulate changes. Other tuning features have their own interface support; this restriction applies specifically to Dynamic Voltage.

Every configured offset, including the idle value, must fit both the driver's range and the engine's −200…0 mV limits, and must exactly match a supported driver step measured from the range minimum. Startup rejects invalid values instead of silently rounding them. Read `amd-gpu-tuner tune get` to inspect the range and step before configuring the controller.

The controller must remain running. Edit thresholds in **Dynamic Voltage**, or supply an engine configuration file:

```powershell
amd-gpu-tuner run --config .\my-engine.json
```

The JSON file may contain the engine fields directly or under an `engine` key. The following illustrates the format with neutral offsets; the clock threshold is only a syntax example:

```json
{
  "engine": {
    "poll_interval_sec": 0.5,
    "hysteresis_count": 2,
    "idle_offset_mv": 0,
    "thresholds": [
      { "clock_mhz": 2000, "offset_mv": 0 }
    ]
  }
}
```

Review existing saved thresholds before starting, including neutral offsets in the example above: all values must be accepted by the current driver's range and steps. Stopping a started controller requests factory tuning reset. Threshold offsets are not a portable voltage/frequency curve for other GPU interfaces.

## Frame data

| Source | What to expect |
| --- | --- |
| PresentMon | Per-frame capture through Windows ETW; the executable must be available and capture may require elevation. |
| RTSS | Used when available and PresentMon is unavailable. Its sampled averages make percentile lows approximate. |
| No source | Hardware telemetry remains available. FPS, frame pacing and FPS-per-watt cannot be established without frame data. |

If no source is available, automatic PresentMon fetching can run in the background. It downloads the standalone console program from the official [PresentMon GitHub releases](https://github.com/GameTechDev/PresentMon/releases), checks allowed HTTPS destinations and basic executable structure, then records a SHA-256 and source metadata. A computed hash records the downloaded bytes; by itself it is not publisher-signature verification.

To disable automatic fetching in the GUI, set this field in `voltshift_config.json` while the app is closed, preserving the other settings:

```json
"telemetry": { "auto_fetch_presentmon": false }
```

For `autotune` and `adaptive`, use `--no-download`. The manual fetch helper is `scripts\fetch_presentmon.ps1`. Network failures should leave hardware telemetry available; inspect **Logs** for the frame-source status.

## Files and local data

Existing filenames are retained after the rename:

| File or directory | Contents |
| --- | --- |
| `voltshift_config.json` | Engine thresholds, App Boost settings, selected goal and frame-download preference |
| `profiles\` | Saved JSON profiles |
| `voltshift_knowledge.db` | Local observations, per-game settings, failed configurations and control-verification results |
| `voltshift_journal.json`, `voltshift_known_good.json` | Automatic-tuning recovery records |
| `voltshift_crashes.log`, `voltshift_telemetry.json`, heartbeat/session files | Local crash diagnostics and recent telemetry |
| `third_party\presentmon\source.json` | Metadata for a fetched PresentMon executable |

When running from a source checkout, runtime files are kept with the app. The local executable build keeps its runtime data beside the executable. A non-editable Python installation uses `%LOCALAPPDATA%\AMD GPU Tuner`. `AMD_GPU_TUNER_DATA_DIR` overrides the data directory, and `AMD_GPU_TUNER_BRIDGE` can point to a separately built bridge executable. Back up the configuration, profiles and knowledge database when the app is closed if you want to retain tuning history.

```powershell
amd-gpu-tuner knowledge stats
amd-gpu-tuner knowledge games
amd-gpu-tuner knowledge export
amd-gpu-tuner knowledge forget example-game.exe
```

`knowledge reset-frontier` clears learned failure limits for the card. Use it deliberately: future searches can reconsider settings that previously failed.

Diagnostics and logs can contain GPU identifiers, executable names and local paths. Review them before attaching them to a public issue. Reports are saved locally; creating a report does not upload it.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Bridge executable not found | Build the native bridge and check `bridge\build\Release\voltshift_bridge.exe`. |
| ADLX initialization fails | Confirm the supported AMD driver is installed and the reported GPU is available. Rebuild the bridge if the app and native code are from different versions. |
| Control is disabled or a write is rejected | Inspect `status --json` and `tune get`. The current hardware/driver may not expose that control or value. |
| Setting reads back unchanged | The driver may ignore the control, clamp it or apply it asynchronously. Check the result log; use deliberate `verify --force` if needed. |
| No FPS or 1% low | Confirm the frame collector is available, can capture the workload and has the required permissions. RTSS lows are approximate. |
| Auto-Tune finds no gain | Check frame-source quality, workload consistency, unsupported controls and the final report. Keeping the baseline is a valid outcome. |
| Driver reset or crash after a change | Stop tuning, restore defaults through AMD Software if necessary, and review the crash report before trying again. |

Create a support report without applying settings:

```powershell
amd-gpu-tuner diagnostics --output diagnostics.json
```

Include the app version, Windows version, GPU, driver version, exact action and expected result in a [bug report](https://github.com/eikkapine/amd-gpu-tuner/issues/new/choose). Share a reviewed diagnostics file and relevant log excerpts when useful. Use the [security policy](../SECURITY.md) for security-sensitive reports.
