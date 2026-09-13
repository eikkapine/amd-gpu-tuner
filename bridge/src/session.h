// VoltShift bridge — ADLX session lifetime and service access.
//
// One Session per daemon process. ADLX is initialized once at startup and
// terminated on clean exit; every command handler borrows services from here
// instead of paying the init cost per call (the old ClawVolt bridge spawned a
// process and re-initialized ADLX for every read).
#pragma once

#include "SDK/ADLXHelper/Windows/Cpp/ADLXHelper.h"
#include "SDK/Include/ISystem.h"
#include "SDK/Include/ISystem2.h"
#include "SDK/Include/IPerformanceMonitoring.h"
#include "SDK/Include/IGPUTuning.h"
#include "SDK/Include/I3DSettings.h"
#include "SDK/Include/I3DSettings1.h"
#include "SDK/Include/I3DSettings2.h"
#include "SDK/Include/I3DSettings3.h"
#include "SDK/Include/IDisplays.h"
#include "SDK/Include/IDisplaySettings.h"
#include "SDK/Include/IDesktops.h"
#include "SDK/Include/IMultiMedia.h"

#include <stdexcept>
#include <string>

namespace voltshift {

// Thrown by handlers on any ADLX failure; the REPL turns it into an
// {"ok":false,"error":...} response instead of killing the daemon.
struct BridgeError : std::runtime_error {
    explicit BridgeError(const std::string& msg) : std::runtime_error(msg) {}
};

const char* ResultStr(ADLX_RESULT r);

// Throws BridgeError("<what>: <code>") when an ADLX call fails.
void Check(ADLX_RESULT res, const char* what);

class Session {
public:
    ~Session() { Terminate(); }
    Session() = default;
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    // Throws BridgeError if the ADLX runtime is missing or refuses to load.
    void Initialize();
    void Terminate();

    adlx::IADLXSystem* System() { return m_system; }

    // Primary (first) GPU. Multi-GPU systems can address others via gpuIndex
    // args later; every current command targets the primary GPU.
    adlx::IADLXGPUPtr Gpu();

    adlx::IADLXPerformanceMonitoringServicesPtr PerfServices();
    adlx::IADLXGPUTuningServicesPtr TuningServices();
    adlx::IADLX3DSettingsServicesPtr GfxServices();
    adlx::IADLXDisplayServicesPtr DisplayServices();
    adlx::IADLXDesktopServicesPtr DesktopServices();

    // nullptr when the driver predates ADLX 2.x/3.x — callers report
    // the feature as unsupported instead of failing.
    adlx::IADLX3DSettingsServices1Ptr GfxServices1();
    adlx::IADLX3DSettingsServices2Ptr GfxServices2();
    adlx::IADLX3DSettingsServices3Ptr GfxServices3();
    adlx::IADLXMultimediaServicesPtr MultimediaServices();

private:
    ADLXHelper m_helper;
    adlx::IADLXSystem* m_system = nullptr;  // owned by ADLX, not ref-counted
    bool m_initialized = false;
};

}  // namespace voltshift
