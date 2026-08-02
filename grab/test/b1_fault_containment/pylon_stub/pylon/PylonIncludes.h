#pragma once
// ─────────────────────────────────────────────────────────────────────────────
// pylon SDK **編譯用 stub**（僅供無 pylon 的機器做語法/型別檢查，不進版控、不可執行）
//
// 目的：驗證 cam_pylon.cpp 的 **B1 例外處理結構** 編得過——catch 順序、
//       note_fault 簽章、grab_loop/grab_loop_body 拆分、收尾 StopGrabbing 的 try。
// ⚠️ 不驗證的事：真實 pylon API 契約、執行期行為、相機互動。
//    本次改動沒有動任何 pylon API 呼叫（只是包起來），故此 stub 的涵蓋範圍剛好對應風險所在。
//    真正的功能驗證必須在 damac + 真相機（見 docs/code_review_20260802.md 修復記錄）。
// ─────────────────────────────────────────────────────────────────────────────

#include <cstddef>
#include <cstdint>
#include <exception>
#include <string>
#include <vector>

namespace Pylon {

// pylon 的字串型別：呼叫端一律 .c_str() 後塞進 std::string
class String_t {
public:
    String_t() = default;
    String_t(const char* s) : s_(s ? s : "") {}
    const char* c_str() const { return s_.c_str(); }
private:
    std::string s_;
};

} // namespace Pylon

namespace GenICam {
// 真實 pylon：GenericException 繼承 std::exception。
// cam_pylon.cpp 的 catch 順序（GenericException 先於 std::exception）正確性依賴這條繼承關係。
class GenericException : public std::exception {
public:
    const char* GetDescription() const { return "stub-description"; }
    const char* what() const noexcept override { return "stub-what"; }
};
} // namespace GenICam

namespace GenApi {
class INodeMap { public: int dummy_ = 0; };
} // namespace GenApi

// ── 故障注入開關（B1 行為測試用；真 pylon 無此物）──────────────────────────
namespace PylonStub {
inline int throw_on_start    = 0;   // StartGrabbing 擲 GenericException（模擬 ARM 當下已斷線）
inline int throw_on_retrieve = 0;   // RetrieveResult 擲 GenericException（模擬取像途中拔線）
inline int throw_on_stop     = 0;   // StopGrabbing 擲（斷線後收尾自己也會擲 → 必須被吞）
inline int deliver_frames    = 0;   // RetrieveResult 回幾張成功的幀（測 frame_cb 擲例外）
inline int grabbing_ticks    = 0;   // IsGrabbing 回 true 的剩餘次數
inline void reset() { throw_on_start = throw_on_retrieve = throw_on_stop
                    = deliver_frames = grabbing_ticks = 0; }
} // namespace PylonStub

namespace Pylon {

using GenICam::GenericException;

enum EGrabStrategy    { GrabStrategy_OneByOne };
enum EGrabLoop        { GrabLoop_ProvidedByUser };
enum ETimeoutHandling { TimeoutHandling_Return };

inline void PylonInitialize() {}
inline void PylonTerminate()  {}

class CDeviceInfo {
public:
    void     SetSerialNumber(const char*) {}
    String_t GetModelName()    const { return String_t("stub-model"); }
    String_t GetSerialNumber() const { return String_t("stub-sn"); }
    String_t GetDeviceClass()  const { return String_t("BaslerGigE"); }
    String_t GetMacAddress()   const { return String_t("003053531941"); }
    String_t GetIpAddress()    const { return String_t("192.168.1.10"); }
    String_t GetIpConfigCurrent() const { return String_t("Persistent"); }
    bool IsMacAddressAvailable()     const { return true; }
    bool IsIpAddressAvailable()      const { return true; }
    bool IsIpConfigCurrentAvailable()const { return true; }
    bool IsPersistentIpActive()      const { return true; }
};

using DeviceInfoList_t = std::vector<CDeviceInfo>;

class IPylonDevice {};

class CTlFactory {
public:
    static CTlFactory& GetInstance() { static CTlFactory f; return f; }
    IPylonDevice* CreateFirstDevice() { return nullptr; }
    IPylonDevice* CreateDevice(const CDeviceInfo&) { return nullptr; }
    int EnumerateDevices(DeviceInfoList_t&) { return 0; }
};

class CGrabResultData {
public:
    bool     GrabSucceeded()           const { return true; }
    uint64_t GetNumberOfSkippedImages()const { return 0; }
    int64_t  GetBlockID()              const { return 0; }
    const void* GetBuffer()            const { return nullptr; }
    size_t   GetImageSize()            const { return 0; }
    uint32_t GetWidth()                const { return 0; }
    uint32_t GetHeight()               const { return 0; }
    String_t GetErrorDescription()     const { return String_t("stub-err"); }
};

class CGrabResultPtr {
public:
    explicit operator bool() const { return p_ != nullptr; }
    bool operator!()         const { return p_ == nullptr; }
    CGrabResultData* operator->() const { return p_; }
    void stub_set(CGrabResultData* p) { p_ = p; }   // stub only
private:
    CGrabResultData* p_ = nullptr;
};

class CInstantCamera {
public:
    int64_t MaxNumBuffer = 0;

    void Attach(IPylonDevice*) {}
    void Open()  {}
    void Close() {}

    GenApi::INodeMap& GetNodeMap() { return nm_; }
    const CDeviceInfo& GetDeviceInfo() const { return di_; }

    void StartGrabbing(EGrabStrategy) {
        if (PylonStub::throw_on_start) throw GenericException();
    }
    void StartGrabbing(int, EGrabStrategy, EGrabLoop) {}
    void StopGrabbing() {
        if (PylonStub::throw_on_stop) throw GenericException();
    }
    bool IsGrabbing() const {
        return PylonStub::grabbing_ticks > 0 ? (--PylonStub::grabbing_ticks, true) : false;
    }

    void RetrieveResult(int, CGrabResultPtr& r, ETimeoutHandling) {
        if (PylonStub::throw_on_retrieve) throw GenericException();
        if (PylonStub::deliver_frames > 0) {
            --PylonStub::deliver_frames;
            static CGrabResultData d;
            r.stub_set(&d);              // 交出一張「成功」的幀 → 呼叫端會呼叫 frame_cb
        }
    }
private:
    GenApi::INodeMap nm_;
    CDeviceInfo      di_;
};

} // namespace Pylon
