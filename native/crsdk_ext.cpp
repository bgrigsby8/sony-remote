// crsdk_ext.cpp - the `_crsdk` Python extension.
//
// The ONLY file in this repo that includes Sony's headers or links their
// libraries. Everything Python-side (`src/binding/native.py` upward) is written
// against `CameraBinding`, so the blast radius of an SDK version bump is this
// file plus the property tables below.
//
// Design rules, in priority order:
//
//   1. NO POLICY. One exported function per SDK operation. Retry, tolerance,
//      timeouts, unit conversion and reconnect all live in Python, where they
//      are testable against `FakeCamera`. The only "logic" allowed here is the
//      symbol tables - and they are here rather than in Python precisely
//      because this is where Sony's enum symbols actually exist.
//
//   2. CALLBACKS NEVER RE-ENTER THE SDK. CrSDK invokes IDeviceCallback on its
//      own threads. Every callback below does one thing: push a POD event onto
//      `g_events` and return. It does not call SDK functions, it does not take
//      the GIL, it does not allocate Python objects. Python drains the queue
//      through `poll_event`.
//
//   3. RELEASE THE GIL AROUND EVERY BLOCKING CALL. Connect, capture, live-view
//      fetch and property I/O all block on USB. Holding the GIL through them
//      would stall the whole viam-server module process.
//
// Errors are raised as RuntimeError with the flat message
// `category|code|text`, parsed by `native.py::_translate`. A flat string keeps
// the C++ side to one helper and needs no registered exception type.
//
// ---------------------------------------------------------------------------
// STATUS: compiles and links against CrSDK v2.02.00 (Linux x64). Not yet
// validated against a live body - SMOKE.md is the checklist for that. If a
// future SDK bump renames symbols, the fixes belong in the tables below and
// nothing above binding/interface.py changes.
// ---------------------------------------------------------------------------

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "CameraRemote_SDK.h"
#include "CrDeviceProperty.h"
#include "IDeviceCallback.h"

namespace py = pybind11;
namespace cr = SCRSDK;

// ---------------------------------------------------------------------------
// Symbol tables
//
// Symbolic name (the vocabulary `binding/interface.py` speaks) -> SDK code.
// If a body reports a property under a different code, fix it HERE; no Python
// changes needed.
// ---------------------------------------------------------------------------

static const std::map<std::string, cr::CrDevicePropertyCode> kPropertyCodes = {
    {"f_number", cr::CrDeviceProperty_FNumber},
    {"shutter_speed", cr::CrDeviceProperty_ShutterSpeed},
    {"iso_sensitivity", cr::CrDeviceProperty_IsoSensitivity},
    {"white_balance", cr::CrDeviceProperty_WhiteBalance},
    {"still_file_format", cr::CrDeviceProperty_FileType},
    {"exposure_program_mode", cr::CrDeviceProperty_ExposureProgramMode},
    {"focus_mode", cr::CrDeviceProperty_FocusMode},
    // Absolute focus. scope.md §10 Q3: confirm this exists on ILCE-7RM5 in the
    // SDK's per-model feature matrix. If it does not, the fallback is relative
    // stepping via CrDeviceProperty_NearFar - and the change is confined to
    // this file plus a `focus_position` shim, because Python only ever asks for
    // "focus_position".
    {"focus_position", cr::CrDeviceProperty_FocusPositionSetting},
    {"shutter_type", cr::CrDeviceProperty_ShutterType},
    {"battery_level", cr::CrDeviceProperty_BatteryRemain},
    {"lens_model", cr::CrDeviceProperty_LensModelName},
    // Who owns the shooting settings: the body's dials or the PC. Defaults to
    // the dials, in which state remote sets are rejected (Api_InvalidCalled)
    // or silently ignored. session.py takes PCRemote right after connect.
    {"priority_key", cr::CrDeviceProperty_PriorityKeySettings},
};

// Symbolic value <-> SDK enum, for the properties whose values are enums rather
// than numbers. Numeric properties (f_number, shutter_speed, iso,
// focus_position) pass through untouched - their encoding is documented and
// handled in `settings.py` where it can be unit-tested.
static const std::map<std::string, std::map<std::string, uint64_t>> kEnumValues = {
    {"white_balance",
     {
         {"AWB", cr::CrWhiteBalance_AWB},
         {"Daylight", cr::CrWhiteBalance_Daylight},
         {"Shade", cr::CrWhiteBalance_Shadow},
         {"Cloudy", cr::CrWhiteBalance_Cloudy},
         {"Incandescent", cr::CrWhiteBalance_Tungsten},
         {"Fluorescent", cr::CrWhiteBalance_Fluorescent_WarmWhite},
         {"Flash", cr::CrWhiteBalance_Flush},  // sic - Sony spells it "Flush"
         {"Underwater", cr::CrWhiteBalance_Underwater_Auto},
         {"ColorTemp", cr::CrWhiteBalance_ColorTemp},
         {"Custom1", cr::CrWhiteBalance_Custom_1},
     }},
    {"shutter_type",
     {
         {"Auto", cr::CrShutterType_Auto},
         {"Mechanical", cr::CrShutterType_MechanicalShutter},
         {"Electronic", cr::CrShutterType_ElectronicShutter},
     }},
    {"still_file_format",
     {
         {"RAW", cr::CrFileType_Raw},
         {"RAW_JPEG", cr::CrFileType_RawJpeg},
         {"JPEG", cr::CrFileType_Jpeg},
         {"RAW_HEIF", cr::CrFileType_RawHeif},
         {"HEIF", cr::CrFileType_Heif},
     }},
    {"focus_mode",
     {
         {"AF_S", cr::CrFocus_AF_S},
         {"AF_C", cr::CrFocus_AF_C},
         {"AF_A", cr::CrFocus_AF_A},
         {"DMF", cr::CrFocus_DMF},
         {"MF", cr::CrFocus_MF},
     }},
    {"priority_key",
     {
         {"CameraPosition", cr::CrPriorityKey_CameraPosition},
         {"PCRemote", cr::CrPriorityKey_PCRemote},
     }},
};

// ---------------------------------------------------------------------------
// Event queue - written by SDK threads, drained by the Python owner thread.
// POD only: no Python objects are constructed off the main thread.
// ---------------------------------------------------------------------------

struct Ev {
    std::string kind;
    std::string path;      // file_written
    std::string property;  // property_changed
    int64_t value = 0;     // property_changed / warning code
    std::string reason;    // disconnected
};

static std::mutex g_ev_mutex;
static std::condition_variable g_ev_cv;
static std::deque<Ev> g_events;

// Bounded so a camera spraying property-change notifications while nothing
// drains (a stalled owner thread) can't grow the process without limit. Old
// events are dropped, not new ones: the freshest state is the useful one.
static const size_t kMaxEvents = 4096;

static void push_event(const Ev& ev) {
    std::lock_guard<std::mutex> lock(g_ev_mutex);
    if (g_events.size() >= kMaxEvents) {
        g_events.pop_front();
    }
    g_events.push_back(ev);
    g_ev_cv.notify_one();
}

// ---------------------------------------------------------------------------
// Global session state. "One process, one camera" (scope.md §3) is enforced in
// Python; this side simply holds the single handle.
// ---------------------------------------------------------------------------

static std::atomic<bool> g_sdk_initialized{false};
static std::atomic<bool> g_connected{false};
static cr::CrDeviceHandle g_handle = 0;
static cr::ICrEnumCameraObjectInfo* g_enum = nullptr;
// From the enumeration info at connect time. GetDeviceProperties has no
// model/serial property on every body, so this is the reliable source.
static std::string g_model;
static std::string g_serial;

[[noreturn]] static void fail(const char* category, int code, const std::string& text) {
    throw std::runtime_error(std::string(category) + "|" + std::to_string(code) + "|" + text);
}

static void require_connected() {
    if (!g_connected.load() || g_handle == 0) {
        fail("disconnected", 0, "no camera session is open");
    }
}

// CrError -> our category. The distinction that matters downstream is
// retryable-busy vs. gone vs. bad-value; everything else is "sdk".
static const char* category_for(CrInt32u err) {
    switch (err) {
        case cr::CrError_Connect_Disconnected:
        case cr::CrError_Connect_TimeOut:
            return "disconnected";
        case cr::CrError_Api_InvalidCalled:
        case cr::CrError_Connect_FailBusy:
        case cr::CrError_Adaptor_DeviceBusy:
            return "busy";
        case cr::CrError_Api_OutOfModelList:
            return "unsupported";
        default:
            return "sdk";
    }
}

static void check(CrInt32u err, const std::string& what) {
    if (err != cr::CrError_None) {
        char code[16];
        std::snprintf(code, sizeof code, "0x%04X", err);
        fail(category_for(err), static_cast<int>(err), what + " failed (CrError " + code + ")");
    }
}

// ---------------------------------------------------------------------------
// Device callback. Every override is: build a POD, push, return. Nothing else.
// ---------------------------------------------------------------------------

class Callback : public cr::IDeviceCallback {
   public:
    void OnConnected(cr::DeviceConnectionVersioin version) override {
        (void)version;
        g_connected.store(true);
        Ev ev;
        ev.kind = "connected";
        push_event(ev);
    }

    void OnDisconnected(CrInt32u error) override {
        g_connected.store(false);
        Ev ev;
        ev.kind = "disconnected";
        ev.value = error;
        ev.reason = "sdk";
        push_event(ev);
    }

    void OnPropertyChanged() override {
        // The no-argument form tells you *something* changed, not what. It is
        // advisory: Python re-reads the properties it cares about rather than
        // trusting a diff. Still worth surfacing - it is how a body announces
        // that a dial was turned under us.
        Ev ev;
        ev.kind = "property_changed";
        push_event(ev);
    }

    void OnPropertyChangedCodes(CrInt32u num, CrInt32u* codes) override {
        for (CrInt32u i = 0; i < num; ++i) {
            Ev ev;
            ev.kind = "property_changed";
            ev.value = codes[i];
            push_event(ev);
        }
    }

    void OnLvPropertyChanged() override {}
    void OnLvPropertyChangedCodes(CrInt32u, CrInt32u*) override {}

    void OnCompleteDownload(CrChar* filename, CrInt32u type) override {
        // Direct-to-host: the SDK has finished writing the still to the
        // directory given to SetSaveInfo. This is the event `capture` waits on.
        (void)type;
        Ev ev;
        ev.kind = "file_written";
        if (filename != nullptr) {
            ev.path = to_utf8(filename);
        }
        push_event(ev);
    }

    void OnNotifyContentsTransfer(CrInt32u notify, cr::CrContentHandle handle,
                                  CrChar* filename) override {
        // Contents-transfer (pull) mode is never entered by this module -
        // stills arrive direct-to-host via OnCompleteDownload above. Surfacing
        // these would double-report files if the mode were ever enabled.
        (void)notify;
        (void)handle;
        (void)filename;
    }

    void OnWarning(CrInt32u warning) override {
        Ev ev;
        ev.kind = "warning";
        ev.value = warning;
        push_event(ev);
    }

    void OnError(CrInt32u error) override {
        // A connection-class error means the session is gone even if
        // OnDisconnected never arrives - which is exactly the USB-yank case.
        if (std::strcmp(category_for(error), "disconnected") == 0) {
            g_connected.store(false);
            Ev ev;
            ev.kind = "disconnected";
            ev.value = error;
            ev.reason = "error";
            push_event(ev);
            return;
        }
        Ev ev;
        ev.kind = "warning";
        ev.value = error;
        push_event(ev);
    }

   private:
    // CrChar is wchar_t on Windows and char elsewhere. This module is
    // POSIX-only (scope.md §2 non-goals), so the narrow path is the only one -
    // but the conversion is isolated here so a Windows port is one function.
    static std::string to_utf8(const CrChar* s) { return std::string(s); }
};

static Callback g_callback;

// ---------------------------------------------------------------------------
// Property helpers
// ---------------------------------------------------------------------------

static cr::CrDevicePropertyCode code_for(const std::string& name) {
    auto it = kPropertyCodes.find(name);
    if (it == kPropertyCodes.end()) {
        fail("unsupported", 0, "unknown property name '" + name + "'");
    }
    return it->second;
}

// Enum-valued property? Then Python speaks in symbolic strings.
static const std::map<std::string, uint64_t>* enum_table(const std::string& name) {
    auto it = kEnumValues.find(name);
    return it == kEnumValues.end() ? nullptr : &it->second;
}

static std::string enum_name_for(const std::map<std::string, uint64_t>& table, uint64_t value) {
    for (const auto& kv : table) {
        if (kv.second == value) {
            return kv.first;
        }
    }
    return std::string();
}

// Decode the property's raw "possible values" buffer. The SDK hands back a byte
// blob whose element width depends on the declared value type; getting this
// wrong silently produces garbage choices, which is why every branch is
// explicit rather than a memcpy of sizeof(T).
static std::vector<uint64_t> decode_values(const cr::CrDeviceProperty& prop) {
    std::vector<uint64_t> out;
    CrInt32u size = prop.GetValueSize();
    void* buf = prop.GetValues();
    if (buf == nullptr || size == 0) {
        return out;
    }
    switch (prop.GetValueType()) {
        case cr::CrDataType_UInt8Array:
        case cr::CrDataType_UInt8: {
            auto* p = static_cast<CrInt8u*>(buf);
            for (CrInt32u i = 0; i < size / sizeof(CrInt8u); ++i) out.push_back(p[i]);
            break;
        }
        case cr::CrDataType_UInt16Array:
        case cr::CrDataType_UInt16: {
            auto* p = static_cast<CrInt16u*>(buf);
            for (CrInt32u i = 0; i < size / sizeof(CrInt16u); ++i) out.push_back(p[i]);
            break;
        }
        case cr::CrDataType_UInt32Array:
        case cr::CrDataType_UInt32: {
            auto* p = static_cast<CrInt32u*>(buf);
            for (CrInt32u i = 0; i < size / sizeof(CrInt32u); ++i) out.push_back(p[i]);
            break;
        }
        case cr::CrDataType_UInt64Array:
        case cr::CrDataType_UInt64: {
            auto* p = static_cast<CrInt64u*>(buf);
            for (CrInt32u i = 0; i < size / sizeof(CrInt64u); ++i) out.push_back(p[i]);
            break;
        }
        default:
            // Signed and string types aren't used by any property we expose.
            // Returning empty means "camera didn't tell us", which callers
            // already handle - better than inventing values.
            break;
    }
    return out;
}

// ---------------------------------------------------------------------------
// Exported functions
// ---------------------------------------------------------------------------

static void ext_init() {
    if (g_sdk_initialized.load()) {
        return;  // idempotent by contract
    }
    bool ok = false;
    {
        py::gil_scoped_release unlock;
        ok = cr::Init(0);
    }
    if (!ok) {
        fail("configuration", 0,
             "SCRSDK::Init() returned false - the CrSDK shared libraries are present but "
             "refused to initialise; check that the adapter .so files are in the directory "
             "the loader searches");
    }
    g_sdk_initialized.store(true);
}

static void ext_release() {
    if (g_handle != 0) {
        py::gil_scoped_release unlock;
        cr::Disconnect(g_handle);
        cr::ReleaseDevice(g_handle);
    }
    g_handle = 0;
    g_connected.store(false);
    if (g_enum != nullptr) {
        g_enum->Release();
        g_enum = nullptr;
    }
    if (g_sdk_initialized.exchange(false)) {
        py::gil_scoped_release unlock;
        cr::Release();
    }
}

static py::list ext_enumerate() {
    if (!g_sdk_initialized.load()) {
        fail("configuration", 0, "call init() before enumerate()");
    }
    // The previous enumeration owns the ICrCameraObjectInfo pointers we hand to
    // connect(), so it is released here rather than at the end of this function.
    if (g_enum != nullptr) {
        g_enum->Release();
        g_enum = nullptr;
    }
    CrInt32u err = 0;
    {
        py::gil_scoped_release unlock;
        err = cr::EnumCameraObjects(&g_enum, 3 /* seconds */);
    }
    if (err != cr::CrError_None || g_enum == nullptr) {
        // No cameras is a normal answer, not an error - the reconnect loop
        // polls this while the operator is still plugging the cable in.
        return py::list();
    }

    py::list out;
    for (CrInt32u i = 0; i < g_enum->GetCount(); ++i) {
        auto* info = g_enum->GetCameraObjectInfo(i);
        py::dict d;
        d["index"] = i;
        d["model"] = std::string(info->GetModel());
        // Not every SDK build exposes a serial on the enumeration object; where
        // it is missing this is the empty string and Python falls back to
        // "there is exactly one camera" selection.
        d["serial"] = std::string(info->GetId() ? reinterpret_cast<const char*>(info->GetId())
                                                : "");
        out.append(d);
    }
    return out;
}

static void ext_connect(int index, int timeout_ms) {
    if (g_enum == nullptr) {
        fail("configuration", 0, "call enumerate() before connect()");
    }
    if (index < 0 || static_cast<CrInt32u>(index) >= g_enum->GetCount()) {
        fail("configuration", 0, "camera index " + std::to_string(index) + " is out of range");
    }
    auto* info = g_enum->GetCameraObjectInfo(static_cast<CrInt32u>(index));
    CrInt32u err = 0;
    {
        py::gil_scoped_release unlock;
        g_connected.store(false);
        // GetCameraObjectInfo returns const; Connect's signature never took the
        // const. Sony's own RemoteCli does the same cast.
        err = cr::Connect(const_cast<cr::ICrCameraObjectInfo*>(info), &g_callback, &g_handle);
    }
    check(err, "Connect");
    // Connect() only starts the handshake - the session is usable when
    // OnConnected fires (which sets g_connected). Property writes before that
    // bounce with Api_InvalidCalled, so block here until the callback lands.
    {
        py::gil_scoped_release unlock;
        std::unique_lock<std::mutex> lock(g_ev_mutex);
        bool up = g_ev_cv.wait_for(
            lock, std::chrono::milliseconds(timeout_ms > 0 ? timeout_ms : 10000),
            [] { return g_connected.load(); });
        if (!up) {
            fail("disconnected", 0,
                 "Connect was accepted but OnConnected did not arrive in time");
        }
    }
    g_model = info->GetModel() ? std::string(info->GetModel()) : std::string();
    g_serial = info->GetId() ? std::string(reinterpret_cast<const char*>(info->GetId()))
                             : std::string();
}

static void ext_disconnect() {
    if (g_handle == 0) {
        return;
    }
    {
        py::gil_scoped_release unlock;
        cr::Disconnect(g_handle);
        cr::ReleaseDevice(g_handle);
    }
    g_handle = 0;
    g_connected.store(false);
}

static bool ext_is_connected() { return g_connected.load() && g_handle != 0; }

static void ext_set_save_info(const std::string& directory, const std::string& prefix,
                              int start_no) {
    require_connected();
    CrInt32u err = 0;
    {
        py::gil_scoped_release unlock;
        err = cr::SetSaveInfo(g_handle, const_cast<CrChar*>(directory.c_str()),
                              const_cast<CrChar*>(prefix.c_str()), start_no);
    }
    check(err, "SetSaveInfo");
}

static py::dict ext_get_property(const std::string& name) {
    require_connected();
    auto want = code_for(name);

    cr::CrDeviceProperty* props = nullptr;
    CrInt32 count = 0;
    CrInt32u err = 0;
    {
        py::gil_scoped_release unlock;
        err = cr::GetDeviceProperties(g_handle, &props, &count);
    }
    check(err, "GetDeviceProperties");

    py::dict out;
    bool found = false;
    for (CrInt32 i = 0; i < count; ++i) {
        if (props[i].GetCode() != want) {
            continue;
        }
        found = true;
        uint64_t current = props[i].GetCurrentValue();
        std::vector<uint64_t> values = decode_values(props[i]);
        const auto* table = enum_table(name);

        if (table != nullptr) {
            std::string symbolic = enum_name_for(*table, current);
            out["value"] = symbolic.empty() ? py::cast(current) : py::cast(symbolic);
            py::list choices;
            for (uint64_t v : values) {
                std::string sym = enum_name_for(*table, v);
                if (!sym.empty()) {
                    choices.append(sym);
                }
            }
            out["choices"] = choices;
        } else {
            out["value"] = current;
            out["choices"] = py::cast(values);
        }
        // GetPropertyEnableFlag() distinguishes "readable" from "settable right
        // now" - focus_position is read-only while the lens is in AF, which is
        // exactly the case set_focus_position has to detect and correct.
        out["writable"] = props[i].GetPropertyEnableFlag() == cr::CrEnableValue_True ||
                          props[i].GetPropertyEnableFlag() == cr::CrEnableValue_SetOnly;
        break;
    }
    if (props != nullptr) {
        cr::ReleaseDeviceProperties(g_handle, props);
    }
    if (!found) {
        fail("unsupported", 0, "this body does not report property '" + name + "'");
    }
    return out;
}

static void ext_set_property(const std::string& name, py::object value) {
    require_connected();
    auto code = code_for(name);
    const auto* table = enum_table(name);

    uint64_t raw = 0;
    if (table != nullptr) {
        if (!py::isinstance<py::str>(value)) {
            fail("unsupported", 0, "property '" + name + "' takes a symbolic string value");
        }
        auto key = value.cast<std::string>();
        auto it = table->find(key);
        if (it == table->end()) {
            std::string valid;
            for (const auto& kv : *table) {
                valid += (valid.empty() ? "" : ", ") + kv.first;
            }
            fail("unsupported", 0,
                 "property '" + name + "' does not accept '" + key + "'; valid: " + valid);
        }
        raw = it->second;
    } else {
        raw = value.cast<uint64_t>();
    }

    // The body rejects a set whose declared value type doesn't match the
    // property's own (shutter_type came back 0x8402 when declared UInt32), so
    // ask the camera what type it reports and echo that back - Sony's sample
    // hardcodes the exact width per property for the same reason.
    cr::CrDataType value_type = cr::CrDataType_UInt32;
    {
        py::gil_scoped_release unlock;
        cr::CrDeviceProperty* props = nullptr;
        CrInt32 count = 0;
        if (cr::GetDeviceProperties(g_handle, &props, &count) == cr::CrError_None) {
            for (CrInt32 i = 0; i < count; ++i) {
                if (props[i].GetCode() == code) {
                    value_type = props[i].GetValueType();
                    break;
                }
            }
            cr::ReleaseDeviceProperties(g_handle, props);
        }
    }
    // Echo the reported type back VERBATIM, array flavour included - Sony's
    // sample sets PriorityKeySettings as UInt32Array even though the enum is
    // 16-bit, so demoting arrays to scalars is exactly the wrong move.

    cr::CrDeviceProperty prop;
    prop.SetCode(code);
    prop.SetCurrentValue(raw);
    prop.SetValueType(value_type);

    CrInt32u err = 0;
    {
        py::gil_scoped_release unlock;
        err = cr::SetDeviceProperty(g_handle, &prop);
    }
    check(err, "SetDeviceProperty(" + name + ")");
}

static py::object ext_live_view_jpeg() {
    require_connected();

    cr::CrImageInfo info;
    CrInt32u err = 0;
    {
        py::gil_scoped_release unlock;
        err = cr::GetLiveViewImageInfo(g_handle, &info);
    }
    if (err != cr::CrError_None || info.GetBufferSize() == 0) {
        // Normal right after connecting - the body hasn't produced a frame yet.
        return py::none();
    }

    std::vector<CrInt8u> buffer(info.GetBufferSize());
    cr::CrImageDataBlock image;
    image.SetSize(static_cast<CrInt32u>(buffer.size()));
    image.SetData(buffer.data());

    {
        py::gil_scoped_release unlock;
        err = cr::GetLiveViewImage(g_handle, &image);
    }
    if (err != cr::CrError_None || image.GetImageSize() == 0) {
        return py::none();
    }
    return py::bytes(reinterpret_cast<const char*>(image.GetImageData()), image.GetImageSize());
}

static void ext_trigger_capture() {
    require_connected();
    CrInt32u err = 0;
    {
        py::gil_scoped_release unlock;
        // Down then Up on S2. The SDK models the shutter button as a held
        // state; leaving it Down wedges the body until the next Up, so the two
        // calls are kept adjacent and unconditional.
        err = cr::SendCommand(g_handle, cr::CrCommandId_Release, cr::CrCommandParam_Down);
        if (err == cr::CrError_None) {
            err = cr::SendCommand(g_handle, cr::CrCommandId_Release, cr::CrCommandParam_Up);
        } else {
            cr::SendCommand(g_handle, cr::CrCommandId_Release, cr::CrCommandParam_Up);
        }
    }
    check(err, "SendCommand(Release)");
}

static bool ext_autofocus_once(int timeout_ms) {
    require_connected();
    // Half-press is a device *property* (S1 = LockIndicator), not a
    // SendCommand - this mirrors Sony's own RemoteCli sample.
    auto set_s1 = [](bool down) {
        cr::CrDeviceProperty prop;
        prop.SetCode(cr::CrDeviceProperty_S1);
        prop.SetCurrentValue(down ? cr::CrLockIndicator_Locked : cr::CrLockIndicator_Unlocked);
        prop.SetValueType(cr::CrDataType_UInt16);
        return cr::SetDeviceProperty(g_handle, &prop);
    };
    bool acquired = false;
    {
        py::gil_scoped_release unlock;
        CrInt32u err = set_s1(true);
        if (err == cr::CrError_None) {
            // The body reports focus state through a property, not an event, so
            // this polls. Half-press is released on every path, including the
            // timeout - a body left holding S1 stops accepting most commands.
            auto deadline = std::chrono::steady_clock::now() +
                            std::chrono::milliseconds(timeout_ms > 0 ? timeout_ms : 3000);
            while (std::chrono::steady_clock::now() < deadline) {
                cr::CrDeviceProperty* props = nullptr;
                CrInt32 count = 0;
                if (cr::GetDeviceProperties(g_handle, &props, &count) == cr::CrError_None) {
                    for (CrInt32 i = 0; i < count; ++i) {
                        if (props[i].GetCode() == cr::CrDeviceProperty_FocusIndication) {
                            auto v = props[i].GetCurrentValue();
                            acquired = (v == cr::CrFocusIndicator_Focused_AF_S ||
                                        v == cr::CrFocusIndicator_Focused_AF_C);
                        }
                    }
                    cr::ReleaseDeviceProperties(g_handle, props);
                }
                if (acquired) {
                    break;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
        }
        set_s1(false);
    }
    return acquired;
}

static py::object ext_poll_event(int timeout_ms) {
    Ev ev;
    {
        py::gil_scoped_release unlock;
        std::unique_lock<std::mutex> lock(g_ev_mutex);
        if (g_events.empty()) {
            g_ev_cv.wait_for(lock, std::chrono::milliseconds(timeout_ms < 0 ? 0 : timeout_ms),
                             [] { return !g_events.empty(); });
        }
        if (g_events.empty()) {
            return py::none();
        }
        ev = g_events.front();
        g_events.pop_front();
    }

    py::dict out;
    out["kind"] = ev.kind;
    if (!ev.path.empty()) out["path"] = ev.path;
    if (!ev.property.empty()) out["property"] = ev.property;
    if (!ev.reason.empty()) out["reason"] = ev.reason;
    if (ev.value != 0) out["value"] = ev.value;
    return out;
}

static py::dict ext_device_info() {
    require_connected();
    py::dict out;
    out["model"] = g_model;
    out["serial"] = g_serial;
    out["battery_pct"] = py::none();
    out["lens"] = py::none();

    cr::CrDeviceProperty* props = nullptr;
    CrInt32 count = 0;
    CrInt32u err = 0;
    {
        py::gil_scoped_release unlock;
        err = cr::GetDeviceProperties(g_handle, &props, &count);
    }
    check(err, "GetDeviceProperties");

    for (CrInt32 i = 0; i < count; ++i) {
        auto code = props[i].GetCode();
        if (code == cr::CrDeviceProperty_BatteryRemain) {
            out["battery_pct"] = static_cast<int>(props[i].GetCurrentValue());
        } else if (code == cr::CrDeviceProperty_LensModelName) {
            auto* raw = reinterpret_cast<const char*>(props[i].GetValues());
            if (raw != nullptr) {
                out["lens"] = std::string(raw, props[i].GetValueSize());
            }
        }
    }
    if (props != nullptr) {
        cr::ReleaseDeviceProperties(g_handle, props);
    }
    return out;
}

PYBIND11_MODULE(_crsdk, m) {
    m.doc() = "Thin binding over the Sony Camera Remote SDK. No policy lives here.";
    m.def("init", &ext_init);
    m.def("release", &ext_release);
    m.def("enumerate", &ext_enumerate);
    m.def("connect", &ext_connect, py::arg("index"), py::arg("timeout_ms"));
    m.def("disconnect", &ext_disconnect);
    m.def("is_connected", &ext_is_connected);
    m.def("device_info", &ext_device_info);
    m.def("set_save_info", &ext_set_save_info, py::arg("directory"), py::arg("prefix"),
          py::arg("start_no"));
    m.def("get_property", &ext_get_property, py::arg("name"));
    m.def("set_property", &ext_set_property, py::arg("name"), py::arg("value"));
    m.def("live_view_jpeg", &ext_live_view_jpeg);
    m.def("trigger_capture", &ext_trigger_capture);
    m.def("autofocus_once", &ext_autofocus_once, py::arg("timeout_ms"));
    m.def("poll_event", &ext_poll_event, py::arg("timeout_ms"));
}
