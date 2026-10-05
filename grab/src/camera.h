#pragma once
// camera.h — 相機抽象層（2026-10-05）：Grab 可選 pylon（raL8192-12gm 原生 GigE）或
// eBUS（Basler L803K Camera Link 經 Pleora iPORT CL-GigE 轉 GigE Vision）。
// CamManager / main 只透過 ICamera 操作相機；各後端各自處理開相機、取像 thread、參數。
// 只用 std 型別，不洩漏 pylon / eBUS header。

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

// 每幀回呼：cam_id / raw pixels / 位元組數 / 寬 / 高
using FrameCb = std::function<void(uint16_t cam_id,
                                   const uint8_t* data, uint32_t bytes,
                                   uint32_t width, uint32_t height)>;

// 相機列舉結果（CTlFactory::EnumerateDevices 後讀 CDeviceInfo，不需開相機）。
// 只用 std 型別，不洩漏 pylon header 給非 pylon 檔。供 LIST_CAMERAS 用。
struct CamInfo {
    // cam_id/ccd_id/bound/bind_source 由 CamManager::resolve() 填入
    // （相機 DeviceUserID "CCDnn" 優先，其次 cam_map.json 的 MAC 綁定）。
    // CamPylon::enumerate_cameras() 本身只填列舉 index + bound=false（它不認識身分規則）。
    int         cam_id      = 0;     // 已綁定 = 槽位；未綁定 = 列舉 index（不穩定）
    std::string ccd_id;              // 顯示標籤（例 CCD00）；未綁定為空
    bool        bound      = false;  // 是否已取得 CCD 身分（未綁定不得當成已就位）
    std::string bind_source;         // "user_id" / "mac" / ""（未綁定）
    std::string user_id;             // 相機 DeviceUserID（pylon 的 UserDefinedName；存在相機 flash）
    std::string model;               // GetModelName 例 raL8192-12gm
    std::string serial;              // GetSerialNumber
    std::string device_class;        // GetDeviceClass 例 BaslerGigE
    std::string mac;                 // GetMacAddress（GigE；非 GigE 空）
    std::string ip;                  // GetIpAddress（空 = N/A）
    bool        online     = true;   // 出現在列舉即視為 online
    bool        persistent = false;  // IsPersistentIpActive()（有 persistent IP = 已綁定）
    std::string ip_config;           // GetIpConfigCurrent（Persistent/DHCP/AutoIP…）
};

// GigE 機器層參數快照（open() 設定的東西,供 UI 顯示「看得到」）。只 std 型別。
struct MachineParams {
    std::string pixel_format, exposure_auto, gain_auto;
    std::string trigger_mode, trigger_selector, trigger_source;
    long long width = 0, height = 0, packet_size = 0, scpd = 0;
    // 行速率（2026-09-21 起 open() 顯式設定；resulting 是相機依 ROI/曝光/頻寬算出的實際上限）
    double line_rate_set = 0, line_rate_resulting = 0;
};

// 影像 ROI（open 時設定）。0 = 不動相機現值。
// height = **送出的**每幀行數。GigE 相機單幀受機上緩衝限制（raL8192 寬 8192 時 ≤3573 行），
// 超過時 open() 自動把相機 Height 設成 height/k，取像時每 k 張相機幀拼成一張送出
// （舊 L803K 為 Camera Link，由擷取卡組幀，無此限制）。
struct Roi {
    int64_t width  = 0;
    int64_t height = 0;
};

// 相機後端
enum class CamBackend { Pylon, Ebus };

// 單台相機介面（CamPylon / CamEbus 實作）。語意以 CamPylon 為準：
//   open() 設好 ROI/封包/行速率並鎖定機台 → start(cam_id) 起取像 thread，每幀呼叫 FrameCb
//   → stop() 停 thread 並關相機（之後可再 open）。故障（拔線等）只標記 faulted、不讓行程死（B1）。
class ICamera {
public:
    virtual ~ICamera() = default;

    // serial：序號（eBUS 亦接受 MAC）；"auto" = 第一台。roi/line_rate 語意見 Roi 與 CamPylon::open。
    virtual bool open(const std::string& serial, int64_t pkt_size, Roi roi, double line_rate_hz) = 0;

    virtual int64_t  payload_size() const = 0;   // 送出幀大小（bytes）
    virtual uint32_t stitch_count() const = 0;   // 每張送出幀 = 幾張相機幀（1 = 不拼接）

    virtual void set_frame_callback(FrameCb cb) = 0;
    virtual void start(uint16_t cam_id) = 0;
    virtual void stop() = 0;
    virtual void set_max_frames(uint64_t n) = 0;

    virtual bool     is_open() const = 0;
    virtual bool     is_running() const = 0;
    virtual uint64_t grabbed() const = 0;
    virtual uint64_t dropped() const = 0;
    virtual bool        is_faulted() const = 0;
    virtual std::string fault_message() const = 0;

    // 曝光（µs）/ 增益（raw）。eBUS(L803K) 走 Camera Link 序列埠，單位換算由後端處理。
    virtual bool set_params(float exposure_us, int gain_raw, float& exp_actual, int& gain_actual) = 0;
    virtual bool get_params(float& exp_actual, int& gain_actual) = 0;
    virtual bool grab_one_mean(double& mean, std::string& err) = 0;
    virtual bool read_machine_params(MachineParams& mp, std::string& err) = 0;
};
