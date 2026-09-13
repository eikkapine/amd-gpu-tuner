#include "session.h"

using namespace adlx;

namespace voltshift {

const char* ResultStr(ADLX_RESULT r)
{
    switch (r)
    {
        case ADLX_OK:                return "OK";
        case ADLX_ALREADY_ENABLED:   return "ALREADY_ENABLED";
        case ADLX_ALREADY_INITIALIZED: return "ALREADY_INITIALIZED";
        case ADLX_FAIL:              return "FAIL";
        case ADLX_INVALID_ARGS:      return "INVALID_ARGS";
        case ADLX_BAD_VER:           return "BAD_VER";
        case ADLX_UNKNOWN_INTERFACE: return "UNKNOWN_INTERFACE";
        case ADLX_TERMINATED:        return "TERMINATED";
        case ADLX_ADL_INIT_ERROR:    return "ADL_INIT_ERROR";
        case ADLX_NOT_FOUND:         return "NOT_FOUND";
        case ADLX_INVALID_OBJECT:    return "INVALID_OBJECT";
        case ADLX_ORPHAN_OBJECTS:    return "ORPHAN_OBJECTS";
        case ADLX_NOT_SUPPORTED:     return "NOT_SUPPORTED";
        case ADLX_PENDING_OPERATION: return "PENDING_OPERATION";
        case ADLX_GPU_INACTIVE:      return "GPU_INACTIVE";
        case ADLX_GPU_IN_USE:        return "GPU_IN_USE";
        case ADLX_TIMEOUT_OPERATION: return "TIMEOUT_OPERATION";
        case ADLX_NOT_ACTIVE:        return "NOT_ACTIVE";
        case ADLX_RESET_NEEDED:      return "RESET_NEEDED";
        default:                     return "UNKNOWN";
    }
}

void Check(ADLX_RESULT res, const char* what)
{
    if (ADLX_FAILED(res))
        throw BridgeError(std::string(what) + ": " + ResultStr(res));
}

void Session::Initialize()
{
    if (m_initialized)
        return;
    ADLX_RESULT res = m_helper.Initialize();
    if (ADLX_FAILED(res))
        throw BridgeError(std::string("ADLX init failed (") + ResultStr(res) +
                          "). Is the AMD Adrenalin driver installed?");
    m_initialized = true;
    m_system = m_helper.GetSystemServices();
    if (!m_system)
    {
        Terminate();
        throw BridgeError("ADLX system services unavailable");
    }
}

void Session::Terminate()
{
    if (m_initialized)
    {
        m_helper.Terminate();
        m_initialized = false;
        m_system = nullptr;
    }
}

IADLXGPUPtr Session::Gpu()
{
    IADLXGPUListPtr gpus;
    Check(m_system->GetGPUs(&gpus), "GetGPUs");
    if (gpus->Empty())
        throw BridgeError("No AMD GPU found");

    // Ryzen iGPUs enumerate before the discrete card; tuning targets the
    // discrete GPU, so prefer it and fall back to whatever is first.
    IADLXGPUPtr first;
    for (adlx_uint i = gpus->Begin(); i != gpus->End(); ++i)
    {
        IADLXGPUPtr gpu;
        if (ADLX_FAILED(gpus->At(i, &gpu)))
            continue;
        if (!first)
            first = gpu;
        ADLX_GPU_TYPE type = GPUTYPE_UNDEFINED;
        if (ADLX_SUCCEEDED(gpu->Type(&type)) && type == GPUTYPE_DISCRETE)
            return gpu;
    }
    if (!first)
        throw BridgeError("No AMD GPU found");
    return first;
}

IADLXPerformanceMonitoringServicesPtr Session::PerfServices()
{
    IADLXPerformanceMonitoringServicesPtr svc;
    Check(m_system->GetPerformanceMonitoringServices(&svc), "GetPerformanceMonitoringServices");
    return svc;
}

IADLXGPUTuningServicesPtr Session::TuningServices()
{
    IADLXGPUTuningServicesPtr svc;
    Check(m_system->GetGPUTuningServices(&svc), "GetGPUTuningServices");
    return svc;
}

IADLX3DSettingsServicesPtr Session::GfxServices()
{
    IADLX3DSettingsServicesPtr svc;
    Check(m_system->Get3DSettingsServices(&svc), "Get3DSettingsServices");
    return svc;
}

IADLXDisplayServicesPtr Session::DisplayServices()
{
    IADLXDisplayServicesPtr svc;
    Check(m_system->GetDisplaysServices(&svc), "GetDisplaysServices");
    return svc;
}

IADLXDesktopServicesPtr Session::DesktopServices()
{
    IADLXDesktopServicesPtr svc;
    Check(m_system->GetDesktopsServices(&svc), "GetDesktopsServices");
    return svc;
}

IADLX3DSettingsServices1Ptr Session::GfxServices1()
{
    IADLX3DSettingsServices1Ptr svc1;
    IADLX3DSettingsServicesPtr svc = GfxServices();
    if (ADLX_FAILED(svc->QueryInterface(IADLX3DSettingsServices1::IID(), reinterpret_cast<void**>(&svc1))))
        return nullptr;
    return svc1;
}

IADLX3DSettingsServices2Ptr Session::GfxServices2()
{
    IADLX3DSettingsServices2Ptr svc2;
    IADLX3DSettingsServicesPtr svc = GfxServices();
    if (ADLX_FAILED(svc->QueryInterface(IADLX3DSettingsServices2::IID(), reinterpret_cast<void**>(&svc2))))
        return nullptr;
    return svc2;
}

IADLX3DSettingsServices3Ptr Session::GfxServices3()
{
    IADLX3DSettingsServices3Ptr svc3;
    IADLX3DSettingsServicesPtr svc = GfxServices();
    if (ADLX_FAILED(svc->QueryInterface(IADLX3DSettingsServices3::IID(), reinterpret_cast<void**>(&svc3))))
        return nullptr;
    return svc3;
}

IADLXMultimediaServicesPtr Session::MultimediaServices()
{
    IADLXSystem2* system2 = nullptr;
    if (ADLX_FAILED(m_system->QueryInterface(IADLXSystem2::IID(), reinterpret_cast<void**>(&system2))) || !system2)
        return nullptr;
    IADLXMultimediaServicesPtr svc;
    ADLX_RESULT res = system2->GetMultimediaServices(&svc);
    system2->Release();
    if (ADLX_FAILED(res))
        return nullptr;
    return svc;
}

}  // namespace voltshift
