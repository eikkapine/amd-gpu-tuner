# Third-party notices

The repository's [MIT license](../LICENSE) applies to the original AMD GPU Tuner code. It does not relicense dependencies, SDK components or downloaded executables. Preserve the applicable upstream notices and license texts when distributing a build.

## AMD ADLX SDK

- Source: [GPUOpen-LibrariesAndSDKs/ADLX](https://github.com/GPUOpen-LibrariesAndSDKs/ADLX).
- Fresh-checkout build revision: `d9f04a9bba022d6cf6333f005dd540b4ad19fb63`.
- Terms: [ADLX SDK License Agreement.pdf at that revision](https://github.com/GPUOpen-LibrariesAndSDKs/ADLX/blob/d9f04a9bba022d6cf6333f005dd540b4ad19fb63/ADLX%20SDK%20License%20Agreement.pdf).
- Notice in the compiled helper sources: Copyright Advanced Micro Devices, Inc. All rights reserved.

The native bridge compiles SDK headers and `SDK/ADLXHelper/Windows/Cpp/ADLXHelper.cpp` plus `SDK/Platform/Windows/WinAPIs.cpp`. The SDK checkout is fetched separately and is excluded from this repository. The driver-supplied `amdadlx64.dll` is not redistributed by this project.

The ADLX SDK is **not MIT licensed**. Its agreement includes a license for incorporated object-code distribution subject to end-user terms in sections 2(c), 3 and Schedule B. It also restricts use and redistribution of SDK materials. Including this notice or a copy of the agreement alone does not satisfy all of those requirements. Review the actual agreement before SDK use or distribution; do not present a compiled SDK-dependent build as covered solely by the project's MIT license.

## JSON for Modern C++

`bridge/vendor/nlohmann/json.hpp` is nlohmann/json version 3.11.3, distributed under MIT.

- Source: [nlohmann/json v3.11.3](https://github.com/nlohmann/json/tree/v3.11.3).
- Header copyright notices: 2013–2023 Niels Lohmann; embedded portions also credit 2016–2021 Evan Nemerson, 2018 The Abseil Authors, 2008–2009 Björn Hoehrmann and 2009 Florian Loitsch.
- [Full upstream MIT license and preserved header notices](licenses/nlohmann-json.txt).

## Python application and packaged runtime

Dependencies used by source installs and local frozen builds include the following projects. Exact versions depend on the build environment; the packaging license collector records installed versions and copies their available license files into the build's `licenses` directory.

| Component | Use | Upstream |
| --- | --- | --- |
| CPython | Python interpreter and standard library | [python/cpython](https://github.com/python/cpython) |
| Tcl/Tk | Desktop GUI runtime | [Tcl/Tk](https://www.tcl.tk/) |
| CustomTkinter | Desktop widgets and packaged resources | [TomSchimansky/CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) |
| darkdetect | Theme detection dependency | [albertosottile/darkdetect](https://github.com/albertosottile/darkdetect) |
| packaging | Python package/version utilities | [pypa/packaging](https://github.com/pypa/packaging) |
| Pillow | Image support used by GUI libraries | [python-pillow/Pillow](https://github.com/python-pillow/Pillow) |
| psutil | Process discovery | [giampaolo/psutil](https://github.com/giampaolo/psutil) |
| pywin32 | Windows APIs and Event Log access | [mhammond/pywin32](https://github.com/mhammond/pywin32) |
| NumPy | Numerical optimization | [numpy/numpy](https://github.com/numpy/numpy) |
| PyInstaller | Local executable packaging and bootloader | [pyinstaller/pyinstaller](https://github.com/pyinstaller/pyinstaller) |

Python and binary wheels may include additional licensed components, such as image codecs, numerical libraries, fonts or standard-library dependencies. The upstream license files bundled with the actual installed versions are authoritative; the table is an inventory of primary projects, not a replacement license for their transitive components. Preserve their collected notices with a redistributed build.

## Optional external tools

[Intel PresentMon](https://github.com/GameTechDev/PresentMon) is fetched separately from its official releases when frame capture is requested or automatic fetching is enabled. It is not included by the project's current PyInstaller specification. Its upstream release and license terms apply; local download provenance is recorded in `third_party/presentmon/source.json`.

RivaTuner Statistics Server and 3DMark are optional, separately installed tools. This project does not redistribute them or grant a license to them.

## Design references

[mVolt](https://github.com/b00nz/mVolt) informed the emphasis on explicit actions, current-state reporting and practical documentation. [RadeonTuner](https://github.com/dumbie/RadeonTuner) informed the earlier manual-tuning scope. No implementation or visual assets were copied from mVolt for this upgrade, and its NVIDIA-specific controls are not part of AMD GPU Tuner.
