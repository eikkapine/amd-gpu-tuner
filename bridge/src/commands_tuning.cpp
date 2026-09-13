// VoltShift bridge — manual tuning commands.
//
// Voltage semantics (carried over from ClawVolt, verified on RX 9070 XT):
//   MGT2_1 (RDNA4): SetGPUVoltage takes the offset directly; range -200..0 mV.
//   MGT2:           SetGPUVoltage takes an absolute value; offset is applied
//                   relative to the current value.
//   MGT1 (RDNA2/3): per-point VF curve; the offset is added to every point.
// Safety: positive offsets and values outside the ADLX-reported range/step
// are rejected. A failed range query must never become an unbounded write.
#include "rpc.h"

#include <cstdint>
#include <limits>

#include "SDK/Include/IGPUManualGFXTuning.h"
#include "SDK/Include/IGPUManualVRAMTuning.h"
#include "SDK/Include/IGPUManualFanTuning.h"
#include "SDK/Include/IGPUManualPowerTuning.h"

using namespace adlx;

namespace voltshift {

namespace {

json RangeJson(const ADLX_IntRange& r)
{
    return {{"min", r.minValue}, {"max", r.maxValue}, {"step", r.step}};
}

adlx_int IntegerArg(const json& args, const char* name)
{
    const json& value = args.at(name);
    if (!value.is_number_integer() ||
        (value.is_number_unsigned() && value.get<std::uint64_t>() >
            static_cast<std::uint64_t>(std::numeric_limits<adlx_int>::max())))
        throw BridgeError(std::string(name) + " must be a 32-bit integer");
    auto number = value.get<std::int64_t>();
    if (number < std::numeric_limits<adlx_int>::min() ||
        number > std::numeric_limits<adlx_int>::max())
        throw BridgeError(std::string(name) + " must be a 32-bit integer");
    return static_cast<adlx_int>(number);
}

adlx_int ValidateValue(std::int64_t value, const ADLX_IntRange& r, const char* name)
{
    if (r.minValue > r.maxValue || r.step <= 0)
        throw BridgeError(std::string(name) + ": driver returned an invalid tuning range");
    if (value < r.minValue || value > r.maxValue || (value - r.minValue) % r.step != 0)
        throw BridgeError(std::string(name) + " must be within " +
            std::to_string(r.minValue) + ".." + std::to_string(r.maxValue) +
            " in steps of " + std::to_string(r.step));
    return static_cast<adlx_int>(value);
}

IADLXInterfacePtr ManualGfxIfc(Session& session, IADLXGPUPtr& gpu)
{
    IADLXGPUTuningServicesPtr svc = session.TuningServices();
    adlx_bool supported = false;
    Check(svc->IsSupportedManualGFXTuning(gpu, &supported), "IsSupportedManualGFXTuning");
    if (!supported)
        throw BridgeError("Manual GFX tuning not supported on this GPU");
    IADLXInterfacePtr ifc;
    Check(svc->GetManualGFXTuning(gpu, &ifc), "GetManualGFXTuning");
    return ifc;
}

IADLXManualFanTuningPtr FanIfc(Session& session, IADLXGPUPtr& gpu)
{
    IADLXGPUTuningServicesPtr svc = session.TuningServices();
    adlx_bool supported = false;
    Check(svc->IsSupportedManualFanTuning(gpu, &supported), "IsSupportedManualFanTuning");
    if (!supported)
        throw BridgeError("Manual fan tuning not supported on this GPU");
    IADLXInterfacePtr ifc;
    Check(svc->GetManualFanTuning(gpu, &ifc), "GetManualFanTuning");
    IADLXManualFanTuningPtr fan;
    Check(ifc->QueryInterface(IADLXManualFanTuning::IID(), reinterpret_cast<void**>(&fan)),
          "QueryInterface(IADLXManualFanTuning)");
    return fan;
}

IADLXManualPowerTuningPtr PowerIfc(Session& session, IADLXGPUPtr& gpu)
{
    IADLXGPUTuningServicesPtr svc = session.TuningServices();
    adlx_bool supported = false;
    Check(svc->IsSupportedManualPowerTuning(gpu, &supported), "IsSupportedManualPowerTuning");
    if (!supported)
        throw BridgeError("Manual power tuning not supported on this GPU");
    IADLXInterfacePtr ifc;
    Check(svc->GetManualPowerTuning(gpu, &ifc), "GetManualPowerTuning");
    IADLXManualPowerTuningPtr power;
    Check(ifc->QueryInterface(IADLXManualPowerTuning::IID(), reinterpret_cast<void**>(&power)),
          "QueryInterface(IADLXManualPowerTuning)");
    return power;
}

// VRAM tuning arrives as VRAMTuning1 (RDNA1-) or VRAMTuning2/2_1 (RDNA2+).
IADLXManualVRAMTuning2Ptr VramIfc2(Session& session, IADLXGPUPtr& gpu)
{
    IADLXGPUTuningServicesPtr svc = session.TuningServices();
    adlx_bool supported = false;
    Check(svc->IsSupportedManualVRAMTuning(gpu, &supported), "IsSupportedManualVRAMTuning");
    if (!supported)
        throw BridgeError("Manual VRAM tuning not supported on this GPU");
    IADLXInterfacePtr ifc;
    Check(svc->GetManualVRAMTuning(gpu, &ifc), "GetManualVRAMTuning");
    IADLXManualVRAMTuning2Ptr vram2;
    if (ADLX_FAILED(ifc->QueryInterface(IADLXManualVRAMTuning2::IID(), reinterpret_cast<void**>(&vram2))) || !vram2)
        throw BridgeError("VRAM tuning interface v2 unavailable (older GPU generation)");
    return vram2;
}

// ── tuning.get ── full snapshot of the manual tuning state + ranges ──────────

json CmdTuningGet(Session& session, const json&)
{
    IADLXGPUPtr gpu = session.Gpu();
    json out = json::object();

    adlx_bool atFactory = false;
    Check(session.TuningServices()->IsAtFactory(gpu, &atFactory), "IsAtFactory");
    out["atFactory"] = static_cast<bool>(atFactory);

    // GFX: voltage + core clocks.
    try
    {
        IADLXInterfacePtr ifc = ManualGfxIfc(session, gpu);
        json gfx = json::object();

        IADLXManualGraphicsTuning2Ptr mgt2;
        IADLXManualGraphicsTuning2_1Ptr mgt21;
        ifc->QueryInterface(IADLXManualGraphicsTuning2_1::IID(), reinterpret_cast<void**>(&mgt21));
        ifc->QueryInterface(IADLXManualGraphicsTuning2::IID(), reinterpret_cast<void**>(&mgt2));

        if (mgt2)
        {
            adlx_int volt = 0, minF = 0, maxF = 0;
            ADLX_IntRange voltRange = {}, minFreqRange = {}, maxFreqRange = {};
            Check(mgt2->GetGPUVoltage(&volt), "GetGPUVoltage");
            Check(mgt2->GetGPUMaxFrequency(&maxF), "GetGPUMaxFrequency");
            Check(mgt2->GetGPUVoltageRange(&voltRange), "GetGPUVoltageRange");
            Check(mgt2->GetGPUMaxFrequencyRange(&maxFreqRange), "GetGPUMaxFrequencyRange");
            gfx["interface"] = mgt21 ? "MGT2_1" : "MGT2";
            gfx["voltageMv"] = volt;
            gfx["maxFreqMhz"] = maxF;
            gfx["voltageRange"] = RangeJson(voltRange);
            gfx["maxFreqRange"] = RangeJson(maxFreqRange);
            // RDNA4 drivers can expose no minimum-clock control. A failed
            // optional getter must not hide valid voltage/max-clock controls.
            const bool minSupported = ADLX_SUCCEEDED(mgt2->GetGPUMinFrequencyRange(&minFreqRange)) &&
                minFreqRange.step > 0 && minFreqRange.minValue <= minFreqRange.maxValue &&
                ADLX_SUCCEEDED(mgt2->GetGPUMinFrequency(&minF));
            gfx["minFrequencySupported"] = minSupported;
            if (minSupported)
            {
                gfx["minFreqMhz"] = minF;
                gfx["minFreqRange"] = RangeJson(minFreqRange);
            }
            if (mgt21)
            {
                adlx_int dv = 0, dmin = 0, dmax = 0;
                json defaults = json::object();
                if (ADLX_SUCCEEDED(mgt21->GetGPUVoltageDefault(&dv))) defaults["voltageMv"] = dv;
                if (minSupported && ADLX_SUCCEEDED(mgt21->GetGPUMinFrequencyDefault(&dmin))) defaults["minFreqMhz"] = dmin;
                if (ADLX_SUCCEEDED(mgt21->GetGPUMaxFrequencyDefault(&dmax))) defaults["maxFreqMhz"] = dmax;
                gfx["defaults"] = defaults;
            }
        }
        else
        {
            IADLXManualGraphicsTuning1Ptr mgt1;
            if (ADLX_SUCCEEDED(ifc->QueryInterface(IADLXManualGraphicsTuning1::IID(), reinterpret_cast<void**>(&mgt1))) && mgt1)
            {
                gfx["interface"] = "MGT1";
                json points = json::array();
                IADLXManualTuningStateListPtr states;
                if (ADLX_SUCCEEDED(mgt1->GetGPUTuningStates(&states)) && states)
                {
                    for (adlx_uint i = states->Begin(); i != states->End(); ++i)
                    {
                        IADLXManualTuningStatePtr st;
                        if (ADLX_SUCCEEDED(states->At(i, &st)))
                        {
                            adlx_int freq = 0, volt = 0;
                            Check(st->GetFrequency(&freq), "GetFrequency");
                            Check(st->GetVoltage(&volt), "GetVoltage");
                            points.push_back({{"freqMhz", freq}, {"voltageMv", volt}});
                        }
                    }
                }
                gfx["vfPoints"] = points;
                ADLX_IntRange freqRange = {}, voltRange = {};
                if (ADLX_SUCCEEDED(mgt1->GetGPUTuningRanges(&freqRange, &voltRange)))
                {
                    gfx["freqRange"] = RangeJson(freqRange);
                    gfx["voltageRange"] = RangeJson(voltRange);
                }
            }
        }
        out["gfx"] = gfx;
    }
    catch (const BridgeError& e) { out["gfx"] = {{"unsupported", e.what()}}; }

    // VRAM: max frequency + memory timing.
    try
    {
        IADLXManualVRAMTuning2Ptr vram = VramIfc2(session, gpu);
        json vj = json::object();

        adlx_int freq = 0;
        ADLX_IntRange freqRange = {};
        Check(vram->GetMaxVRAMFrequency(&freq), "GetMaxVRAMFrequency");
        Check(vram->GetMaxVRAMFrequencyRange(&freqRange), "GetMaxVRAMFrequencyRange");
        vj["maxFreqMhz"] = freq;
        vj["maxFreqRange"] = RangeJson(freqRange);

        IADLXManualVRAMTuning2_1Ptr vram21;
        if (ADLX_SUCCEEDED(vram->QueryInterface(IADLXManualVRAMTuning2_1::IID(), reinterpret_cast<void**>(&vram21))) && vram21)
        {
            adlx_int dfreq = 0;
            if (ADLX_SUCCEEDED(vram21->GetMaxVRAMFrequencyDefault(&dfreq)))
                vj["maxFreqDefaultMhz"] = dfreq;
        }

        adlx_bool timingSupported = false;
        Check(vram->IsSupportedMemoryTiming(&timingSupported), "IsSupportedMemoryTiming");
        vj["timingSupported"] = static_cast<bool>(timingSupported);
        if (timingSupported)
        {
            ADLX_MEMORYTIMING_DESCRIPTION current = MEMORYTIMING_DEFAULT;
            Check(vram->GetMemoryTimingDescription(&current), "GetMemoryTimingDescription");
            vj["timing"] = static_cast<int>(current);

            json available = json::array();
            IADLXMemoryTimingDescriptionListPtr list;
            if (ADLX_SUCCEEDED(vram->GetSupportedMemoryTimingDescriptionList(&list)) && list)
            {
                for (adlx_uint i = list->Begin(); i != list->End(); ++i)
                {
                    IADLXMemoryTimingDescriptionPtr desc;
                    if (ADLX_SUCCEEDED(list->At(i, &desc)))
                    {
                        ADLX_MEMORYTIMING_DESCRIPTION d = MEMORYTIMING_DEFAULT;
                        Check(desc->GetDescription(&d), "GetDescription");
                        available.push_back(static_cast<int>(d));
                    }
                }
            }
            vj["timingOptions"] = available;
        }
        out["vram"] = vj;
    }
    catch (const BridgeError& e) { out["vram"] = {{"unsupported", e.what()}}; }

    // Power: limit % + TDC.
    try
    {
        IADLXManualPowerTuningPtr power = PowerIfc(session, gpu);
        json pj = json::object();

        adlx_int limit = 0;
        ADLX_IntRange limitRange = {};
        Check(power->GetPowerLimit(&limit), "GetPowerLimit");
        Check(power->GetPowerLimitRange(&limitRange), "GetPowerLimitRange");
        pj["powerLimit"] = limit;
        pj["powerLimitRange"] = RangeJson(limitRange);

        adlx_bool tdcSupported = false;
        Check(power->IsSupportedTDCLimit(&tdcSupported), "IsSupportedTDCLimit");
        pj["tdcSupported"] = static_cast<bool>(tdcSupported);
        if (tdcSupported)
        {
            adlx_int tdc = 0;
            ADLX_IntRange tdcRange = {};
            Check(power->GetTDCLimit(&tdc), "GetTDCLimit");
            Check(power->GetTDCLimitRange(&tdcRange), "GetTDCLimitRange");
            pj["tdcLimit"] = tdc;
            pj["tdcRange"] = RangeJson(tdcRange);
        }

        IADLXManualPowerTuning1Ptr power1;
        if (ADLX_SUCCEEDED(power->QueryInterface(IADLXManualPowerTuning1::IID(), reinterpret_cast<void**>(&power1))) && power1)
        {
            adlx_int d = 0;
            if (ADLX_SUCCEEDED(power1->GetPowerLimitDefault(&d)))
                pj["powerLimitDefault"] = d;
        }
        out["power"] = pj;
    }
    catch (const BridgeError& e) { out["power"] = {{"unsupported", e.what()}}; }

    return out;
}

// ── tuning.setVoltageOffset {"mv": -120} ─────────────────────────────────────

json CmdSetVoltageOffset(Session& session, const json& args)
{
    adlx_int offsetMv = IntegerArg(args, "mv");
    if (offsetMv > 0)
        throw BridgeError("Positive voltage offsets are blocked for safety");

    IADLXGPUPtr gpu = session.Gpu();
    IADLXInterfacePtr ifc = ManualGfxIfc(session, gpu);

    // RDNA4: the voltage value IS the offset.
    IADLXManualGraphicsTuning2_1Ptr mgt21;
    if (ADLX_SUCCEEDED(ifc->QueryInterface(IADLXManualGraphicsTuning2_1::IID(), reinterpret_cast<void**>(&mgt21))) && mgt21)
    {
        ADLX_IntRange range = {};
        Check(mgt21->GetGPUVoltageRange(&range), "GetGPUVoltageRange");
        // Version 2_1 adds default getters; refuse to assume offset semantics
        // if this driver advertises an absolute-voltage range.
        if (range.minValue > 0)
            throw BridgeError("Driver does not expose a voltage-offset range on MGT2_1");
        adlx_int applied = ValidateValue(offsetMv, range, "Voltage offset");
        Check(mgt21->SetGPUVoltage(applied), "SetGPUVoltage(MGT2_1)");
        return {{"appliedMv", applied}, {"interface", "MGT2_1"}};
    }

    // MGT2: absolute voltage, offset applied relative to current.
    IADLXManualGraphicsTuning2Ptr mgt2;
    if (ADLX_SUCCEEDED(ifc->QueryInterface(IADLXManualGraphicsTuning2::IID(), reinterpret_cast<void**>(&mgt2))) && mgt2)
    {
        adlx_int current = 0;
        Check(mgt2->GetGPUVoltage(&current), "GetGPUVoltage");
        ADLX_IntRange range = {};
        Check(mgt2->GetGPUVoltageRange(&range), "GetGPUVoltageRange");
        adlx_int applied = ValidateValue(static_cast<std::int64_t>(current) + offsetMv, range, "Voltage");
        Check(mgt2->SetGPUVoltage(applied), "SetGPUVoltage(MGT2)");
        return {{"baseMv", current}, {"offsetMv", offsetMv}, {"appliedMv", applied}, {"interface", "MGT2"}};
    }

    // MGT1: shift every VF curve point.
    IADLXManualGraphicsTuning1Ptr mgt1;
    if (ADLX_FAILED(ifc->QueryInterface(IADLXManualGraphicsTuning1::IID(), reinterpret_cast<void**>(&mgt1))) || !mgt1)
        throw BridgeError("No compatible manual GFX tuning interface");

    IADLXManualTuningStateListPtr current, empty;
    Check(mgt1->GetGPUTuningStates(&current), "GetGPUTuningStates");
    Check(mgt1->GetEmptyGPUTuningStates(&empty), "GetEmptyGPUTuningStates");

    ADLX_IntRange freqRange = {}, voltRange = {};
    Check(mgt1->GetGPUTuningRanges(&freqRange, &voltRange), "GetGPUTuningRanges");

    adlx_uint count = current->End() - current->Begin();
    if (count == 0 || count != empty->End() - empty->Begin())
        throw BridgeError("Driver returned inconsistent GPU tuning states");
    for (adlx_uint i = 0; i < count; ++i)
    {
        IADLXManualTuningStatePtr src, dst;
        Check(current->At(i + current->Begin(), &src), "CurrentGPUStates::At");
        Check(empty->At(i + empty->Begin(), &dst), "EmptyGPUStates::At");

        adlx_int freq = 0, volt = 0;
        Check(src->GetFrequency(&freq), "GetFrequency");
        Check(src->GetVoltage(&volt), "GetVoltage");
        Check(dst->SetFrequency(freq), "SetFrequency");
        Check(dst->SetVoltage(ValidateValue(static_cast<std::int64_t>(volt) + offsetMv,
            voltRange, "VF point voltage")), "SetVoltage");
    }

    adlx_int errIdx = 0;
    Check(mgt1->IsValidGPUTuningStates(empty, &errIdx), "IsValidGPUTuningStates");
    if (errIdx != -1)
        throw BridgeError("Invalid GPU tuning state at point " + std::to_string(errIdx));
    Check(mgt1->SetGPUTuningStates(empty), "SetGPUTuningStates");
    return {{"offsetMv", offsetMv}, {"interface", "MGT1"}};
}

// ── tuning.setCoreClocks {"minMhz": 500, "maxMhz": 3100} — both optional ─────

json CmdSetCoreClocks(Session& session, const json& args)
{
    const bool setMin = args.contains("minMhz"), setMax = args.contains("maxMhz");
    if (!setMin && !setMax)
        throw BridgeError("setCoreClocks needs minMhz and/or maxMhz");

    IADLXGPUPtr gpu = session.Gpu();
    IADLXInterfacePtr ifc = ManualGfxIfc(session, gpu);

    IADLXManualGraphicsTuning2Ptr mgt2;
    if (ADLX_FAILED(ifc->QueryInterface(IADLXManualGraphicsTuning2::IID(), reinterpret_cast<void**>(&mgt2))) || !mgt2)
        throw BridgeError("Core clock control requires the MGT2 tuning interface");

    ADLX_IntRange minRange = {}, maxRange = {};
    Check(mgt2->GetGPUMaxFrequencyRange(&maxRange), "GetGPUMaxFrequencyRange");
    adlx_int oldMin = 0, oldMax = 0;
    Check(mgt2->GetGPUMaxFrequency(&oldMax), "GetGPUMaxFrequency");
    const bool absoluteMax = maxRange.minValue > 0;
    if (setMin)
        Check(mgt2->GetGPUMinFrequencyRange(&minRange), "GetGPUMinFrequencyRange");
    if (setMin || absoluteMax)
        Check(mgt2->GetGPUMinFrequency(&oldMin), "GetGPUMinFrequency");
    const adlx_int min = setMin ? ValidateValue(IntegerArg(args, "minMhz"), minRange, "Minimum clock") : oldMin;
    const adlx_int max = setMax ? ValidateValue(IntegerArg(args, "maxMhz"), maxRange, "Maximum clock") : oldMax;

    // Navi4+ exposes maximum frequency as an offset, whereas minimum
    // frequency remains absolute. Compare them only for absolute ranges.
    if (absoluteMax && min > max)
        throw BridgeError("Minimum core clock must not exceed maximum core clock");
    const bool maxFirst = absoluteMax && setMax && min > oldMax;
    bool firstWritten = false;
    try
    {
        if (maxFirst)
        {
            Check(mgt2->SetGPUMaxFrequency(max), "SetGPUMaxFrequency");
            firstWritten = true;
        }
        if (setMin)
        {
            Check(mgt2->SetGPUMinFrequency(min), "SetGPUMinFrequency");
            firstWritten = true;
        }
        if (setMax && !maxFirst)
            Check(mgt2->SetGPUMaxFrequency(max), "SetGPUMaxFrequency");
    }
    catch (const BridgeError& error)
    {
        if (setMin && setMax && firstWritten)
        {
            // Undo in reverse order if the second write fails. Never reset
            // unrelated voltage, VRAM, power, or fan settings as rollback.
            ADLX_RESULT second = maxFirst ? mgt2->SetGPUMinFrequency(oldMin) : mgt2->SetGPUMaxFrequency(oldMax);
            ADLX_RESULT first = maxFirst ? mgt2->SetGPUMaxFrequency(oldMax) : mgt2->SetGPUMinFrequency(oldMin);
            if (ADLX_FAILED(first) || ADLX_FAILED(second))
                throw BridgeError(std::string(error.what()) + "; clock rollback failed; tuning may be partially applied");
        }
        throw;
    }
    json out = json::object();
    if (setMin) out["minMhz"] = min;
    if (setMax) out["maxMhz"] = max;
    return out;
}

// ── tuning.setVramMax {"mhz": 2600} ──────────────────────────────────────────

json CmdSetVramMax(Session& session, const json& args)
{
    IADLXGPUPtr gpu = session.Gpu();
    IADLXManualVRAMTuning2Ptr vram = VramIfc2(session, gpu);

    ADLX_IntRange range = {};
    Check(vram->GetMaxVRAMFrequencyRange(&range), "GetMaxVRAMFrequencyRange");
    adlx_int v = ValidateValue(IntegerArg(args, "mhz"), range, "VRAM frequency");
    Check(vram->SetMaxVRAMFrequency(v), "SetMaxVRAMFrequency");
    return {{"appliedMhz", v}};
}

// ── tuning.setMemoryTiming {"timing": 1} ─────────────────────────────────────

json CmdSetMemoryTiming(Session& session, const json& args)
{
    IADLXGPUPtr gpu = session.Gpu();
    IADLXManualVRAMTuning2Ptr vram = VramIfc2(session, gpu);

    adlx_bool supported = false;
    Check(vram->IsSupportedMemoryTiming(&supported), "IsSupportedMemoryTiming");
    if (!supported)
        throw BridgeError("Memory timing not supported on this GPU");

    int timing = IntegerArg(args, "timing");
    IADLXMemoryTimingDescriptionListPtr options;
    Check(vram->GetSupportedMemoryTimingDescriptionList(&options), "GetSupportedMemoryTimingDescriptionList");
    bool available = false;
    for (adlx_uint i = options->Begin(); i != options->End(); ++i)
    {
        IADLXMemoryTimingDescriptionPtr option;
        Check(options->At(i, &option), "MemoryTimingDescriptions::At");
        ADLX_MEMORYTIMING_DESCRIPTION value = MEMORYTIMING_DEFAULT;
        Check(option->GetDescription(&value), "GetDescription");
        if (timing == static_cast<int>(value)) available = true;
    }
    if (!available)
        throw BridgeError("Requested memory timing is not supported on this GPU");
    Check(vram->SetMemoryTimingDescription(static_cast<ADLX_MEMORYTIMING_DESCRIPTION>(timing)),
          "SetMemoryTimingDescription");
    return {{"applied", timing}};
}

// ── tuning.setPowerLimit {"pct": 10} / tuning.setTdc {"amps": N} ─────────────

json CmdSetPowerLimit(Session& session, const json& args)
{
    IADLXGPUPtr gpu = session.Gpu();
    IADLXManualPowerTuningPtr power = PowerIfc(session, gpu);

    ADLX_IntRange range = {};
    Check(power->GetPowerLimitRange(&range), "GetPowerLimitRange");
    adlx_int v = ValidateValue(IntegerArg(args, "pct"), range, "Power limit");
    Check(power->SetPowerLimit(v), "SetPowerLimit");
    return {{"applied", v}};
}

json CmdSetTdc(Session& session, const json& args)
{
    IADLXGPUPtr gpu = session.Gpu();
    IADLXManualPowerTuningPtr power = PowerIfc(session, gpu);

    adlx_bool supported = false;
    Check(power->IsSupportedTDCLimit(&supported), "IsSupportedTDCLimit");
    if (!supported)
        throw BridgeError("TDC limit not supported on this GPU");

    ADLX_IntRange range = {};
    Check(power->GetTDCLimitRange(&range), "GetTDCLimitRange");
    adlx_int v = ValidateValue(IntegerArg(args, "amps"), range, "TDC limit");
    Check(power->SetTDCLimit(v), "SetTDCLimit");
    return {{"applied", v}};
}

// ── tuning.getFans ───────────────────────────────────────────────────────────

json CmdGetFans(Session& session, const json&)
{
    IADLXGPUPtr gpu = session.Gpu();
    IADLXManualFanTuningPtr fan = FanIfc(session, gpu);

    json out = json::object();

    ADLX_IntRange speedRange = {}, tempRange = {};
    Check(fan->GetFanTuningRanges(&speedRange, &tempRange), "GetFanTuningRanges");
    out["speedRange"] = RangeJson(speedRange);
    out["tempRange"] = RangeJson(tempRange);

    json curve = json::array();
    IADLXManualFanTuningStateListPtr states;
    Check(fan->GetFanTuningStates(&states), "GetFanTuningStates");
    for (adlx_uint i = states->Begin(); i != states->End(); ++i)
    {
        IADLXManualFanTuningStatePtr st;
        if (ADLX_SUCCEEDED(states->At(i, &st)))
        {
            adlx_int speed = 0, temp = 0;
            Check(st->GetFanSpeed(&speed), "GetFanSpeed");
            Check(st->GetTemperature(&temp), "GetTemperature");
            curve.push_back({{"tempC", temp}, {"speedPct", speed}});
        }
    }
    out["curve"] = curve;

    adlx_bool zeroRpmSupported = false;
    Check(fan->IsSupportedZeroRPM(&zeroRpmSupported), "IsSupportedZeroRPM");
    out["zeroRpmSupported"] = static_cast<bool>(zeroRpmSupported);
    if (zeroRpmSupported)
    {
        adlx_bool zeroRpm = false;
        Check(fan->GetZeroRPMState(&zeroRpm), "GetZeroRPMState");
        out["zeroRpm"] = static_cast<bool>(zeroRpm);
    }

    adlx_bool targetSupported = false;
    Check(fan->IsSupportedTargetFanSpeed(&targetSupported), "IsSupportedTargetFanSpeed");
    out["targetFanSpeedSupported"] = static_cast<bool>(targetSupported);
    if (targetSupported)
    {
        adlx_int target = 0;
        ADLX_IntRange targetRange = {};
        Check(fan->GetTargetFanSpeed(&target), "GetTargetFanSpeed");
        Check(fan->GetTargetFanSpeedRange(&targetRange), "GetTargetFanSpeedRange");
        out["targetFanSpeed"] = target;
        out["targetFanSpeedRange"] = RangeJson(targetRange);
    }

    adlx_bool minFanSupported = false;
    Check(fan->IsSupportedMinFanSpeed(&minFanSupported), "IsSupportedMinFanSpeed");
    out["minFanSpeedSupported"] = static_cast<bool>(minFanSupported);
    if (minFanSupported)
    {
        adlx_int minFan = 0;
        ADLX_IntRange minFanRange = {};
        Check(fan->GetMinFanSpeed(&minFan), "GetMinFanSpeed");
        Check(fan->GetMinFanSpeedRange(&minFanRange), "GetMinFanSpeedRange");
        out["minFanSpeed"] = minFan;
        out["minFanSpeedRange"] = RangeJson(minFanRange);
    }

    return out;
}

// ── tuning.setFanCurve {"curve":[{"tempC":40,"speedPct":30},...]} ────────────

json CmdSetFanCurve(Session& session, const json& args)
{
    const json& curve = args.at("curve");
    if (!curve.is_array() || curve.empty())
        throw BridgeError("setFanCurve needs a non-empty curve array");

    IADLXGPUPtr gpu = session.Gpu();
    IADLXManualFanTuningPtr fan = FanIfc(session, gpu);

    ADLX_IntRange speedRange = {}, tempRange = {};
    Check(fan->GetFanTuningRanges(&speedRange, &tempRange), "GetFanTuningRanges");

    IADLXManualFanTuningStateListPtr states;
    Check(fan->GetEmptyFanTuningStates(&states), "GetEmptyFanTuningStates");

    adlx_uint stateCount = states->End() - states->Begin();
    if (curve.size() != stateCount)
        throw BridgeError("Fan curve must have exactly " + std::to_string(stateCount) + " points");

    json applied = json::array();
    for (adlx_uint i = 0; i < stateCount; ++i)
    {
        IADLXManualFanTuningStatePtr st;
        Check(states->At(i + states->Begin(), &st), "FanStates::At");
        adlx_int temp = ValidateValue(IntegerArg(curve[i], "tempC"), tempRange, "Fan temperature");
        adlx_int speed = ValidateValue(IntegerArg(curve[i], "speedPct"), speedRange, "Fan speed");
        if (i > 0 && (temp <= applied[i - 1]["tempC"].get<adlx_int>() ||
                      speed < applied[i - 1]["speedPct"].get<adlx_int>()))
            throw BridgeError("Fan temperatures must increase and fan speeds must not decrease");
        Check(st->SetTemperature(temp), "SetTemperature");
        Check(st->SetFanSpeed(speed), "SetFanSpeed");
        applied.push_back({{"tempC", temp}, {"speedPct", speed}});
    }

    adlx_int errIdx = 0;
    Check(fan->IsValidFanTuningStates(states, &errIdx), "IsValidFanTuningStates");
    if (errIdx != -1)
        throw BridgeError("Invalid fan tuning state at point " + std::to_string(errIdx));
    Check(fan->SetFanTuningStates(states), "SetFanTuningStates");
    return {{"curve", applied}};
}

// ── tuning.setZeroRpm {"enabled": true} ──────────────────────────────────────

json CmdSetZeroRpm(Session& session, const json& args)
{
    IADLXGPUPtr gpu = session.Gpu();
    IADLXManualFanTuningPtr fan = FanIfc(session, gpu);

    adlx_bool supported = false;
    Check(fan->IsSupportedZeroRPM(&supported), "IsSupportedZeroRPM");
    if (!supported)
        throw BridgeError("ZeroRPM not supported on this GPU");

    bool enabled = args.at("enabled").get<bool>();
    Check(fan->SetZeroRPMState(enabled), "SetZeroRPMState");
    return {{"enabled", enabled}};
}

// ── tuning.reset — restore AMD factory defaults ──────────────────────────────

json CmdTuningReset(Session& session, const json&)
{
    IADLXGPUPtr gpu = session.Gpu();
    Check(session.TuningServices()->ResetToFactory(gpu), "ResetToFactory");
    return {{"reset", true}};
}

}  // namespace

void RegisterTuning(Registry& reg)
{
    reg["tuning.get"] = CmdTuningGet;
    reg["tuning.setVoltageOffset"] = CmdSetVoltageOffset;
    reg["tuning.setCoreClocks"] = CmdSetCoreClocks;
    reg["tuning.setVramMax"] = CmdSetVramMax;
    reg["tuning.setMemoryTiming"] = CmdSetMemoryTiming;
    reg["tuning.setPowerLimit"] = CmdSetPowerLimit;
    reg["tuning.setTdc"] = CmdSetTdc;
    reg["tuning.getFans"] = CmdGetFans;
    reg["tuning.setFanCurve"] = CmdSetFanCurve;
    reg["tuning.setZeroRpm"] = CmdSetZeroRpm;
    reg["tuning.reset"] = CmdTuningReset;
}

}  // namespace voltshift
