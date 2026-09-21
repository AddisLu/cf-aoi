#pragma once
// CamPylon — Basler pylon 單台相機擷取（升級自 t31_pylon_grab）
// 在獨立 thread 跑 GrabStrategy_OneByOne 持續迴圈，每幀呼叫 FrameCb。
// Stop 後可重新 start（同一個 camera 物件）。

#include <atomic>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
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

class CamPylon {
public:
    CamPylon() = default;
    ~CamPylon() { stop(); }

    // 列舉主機看得到的所有相機（不需 open；EnumerateDevices + 讀 CDeviceInfo）。
    // 唯讀，不改任何相機；可在 idle 或（待實測）grabbing 中呼叫。供 LIST_CAMERAS。
    static std::vector<CamInfo> enumerate_cameras();

    // open：初始化相機（auto = 第一台，或給序號）、設 GevSCPSPacketSize。
    // 成功後可呼叫 payload_size() 取得幀大小，再去連 RDMA。
    // roi 非 0 → 設 Width/Height 並讀回確認；設不進（超出相機範圍等）→ open 失敗（fail-fast，
    // 不可默默用相機現值：新相機出廠 Height=256，不設 = 每幀 256 行且無任何錯誤）。
    // line_rate_hz：>0 = 設為該值；0 = 不動相機現值（舊行為）；<0 = 設為節點上限（不設限）。
    // 不設會繼承相機 flash —— 2026-09-21 實測四台不一致（兩台被 UserSet1 鎖在 11,001 Hz、
    // 兩台出廠不設限 12,195 Hz），故與 Width/Height 同等顯式化。見 grab/CLAUDE.md 不變式 11。
    bool open(const std::string& serial = "auto", int64_t pkt_size = 9000, Roi roi = {},
              double line_rate_hz = -1);

    int64_t  payload_size() const { return payload_; }   // 送出幀大小（已含拼接）
    uint32_t stitch_count() const { return stitch_; }    // 每張送出幀 = 幾張相機幀（1 = 不拼接）

    void set_frame_callback(FrameCb cb) { cb_ = std::move(cb); }

    // start/stop：控制持續取像 thread。start 前必須先 open() 且設好 callback。
    void start(uint16_t cam_id);
    void stop();

    // 每片張數上限：grab_loop 收滿 N 張後自動結束（thread 自然退出，stop() 再 join）。
    // 0 = 不限（連續取像，legacy 行為）。軟體觸發架構「每台收滿 N 張×5000 條自動停」的機制。
    void set_max_frames(uint64_t n) { max_frames_ = n; }

    bool     is_open()    const { return opened_; }
    bool     is_running() const { return running_.load(); }
    uint64_t grabbed()    const { return grabbed_; }   // 送出幀數（拼接後）
    uint64_t dropped()    const { return dropped_; }   // 相機幀單位：遺失 + 拼接中途作廢

    // ---- B1：取像 thread 故障狀態（docs/code_review_20260802.md B1 修法）----
    // grab_loop 攔到例外（拔線/斷電/交換機掉埠、或 frame_cb 內部丟出）後：
    //   ① 標記 faulted_ 並存下訊息 ② 該台 thread 乾淨退出 ③ **其餘相機不受影響**。
    // ⚠️ 故障後 is_running() 會變 false——與「收滿 N 張自動停」外觀相同，
    //    呼叫端**必須用 is_faulted() 區分**，否則會把斷線當成正常收滿（靜默假完成）。
    bool        is_faulted()    const { return faulted_.load(); }
    std::string fault_message() const {
        std::lock_guard<std::mutex> lk(fault_mtx_);
        return fault_msg_;
    }

    // Gap #2：曝光 / 增益（Stage 0 確認：ExposureTimeAbs µs, GainRaw int 256~2047）
    // 相機必須已 open()；acquisition 中可寫（TLParamsLocked=0）。
    // set_params: 寫 → read-back actual → 回傳 true/false
    // get_params: 直接從相機讀（不觸碰 JSON）
    bool set_params(float exposure_us, int gain_raw,
                    float& exp_actual, int& gain_actual);
    bool get_params(float& exp_actual, int& gain_actual);

    // 調參效果確認：抓 1 幀算 uint8 平均灰階（證明影像真的隨曝光/增益變，非只看回讀值）。
    // 需相機已 open 且「非串流中」(running_=false)；串流中請先停止。
    bool grab_one_mean(double& mean, std::string& err);

    // 讀回 GigE 機器層參數（PixelFormat/Auto/Trigger/ROI/封包），供 UI 顯示。需相機已 open。
    bool read_machine_params(MachineParams& mp, std::string& err);

private:
    void grab_loop();       // thread 進入點：try/catch 薄殼（B1；例外絕不逸出）
    void grab_loop_body();  // 取像迴圈本體（允許擲例外，由 grab_loop 攔）
    // B1：記錄故障訊息 + 豎 faulted_。收 const char*（呼叫端在 catch handler 內，
    // 不可做任何會擲例外的配置動作，否則例外二次逸出仍會 terminate）。
    void note_fault(const char* kind, const char* what);

    // pylon 物件用 void* 持有，避免 pylon headers 污染包含 cam_pylon.h 的非 pylon 檔案
    void*    camera_ptr_  = nullptr;  // CInstantCamera*
    bool     opened_      = false;
    int64_t  payload_     = 0;
    // 行速率（open 時設定/讀回；set_params 改曝光後會重讀並在被壓低時警告）
    double   line_rate_set_ = 0;   // 寫進 AcquisitionLineRateAbs 的值
    double   line_rate_res_ = 0;   // 相機算出的 ResultingLineRateAbs（實際上限）
    uint16_t cam_id_      = 0;
    FrameCb  cb_;

    std::atomic<bool> stop_flag_{false};
    std::atomic<bool> running_{false};
    std::thread thread_;

    // B1：故障旗標與訊息（grab thread 寫、控制 thread 讀 → atomic + mutex）
    std::atomic<bool>  faulted_{false};
    mutable std::mutex fault_mtx_;
    std::string        fault_msg_;

    // ⚠️ 已知限制（docs/code_review_20260802.md B13）：plain uint64 由 grab thread 寫、
    // ctrl thread（CHECK_HEALTH）無鎖讀——監控用途 x86 實務可用，正式屬 data race。
    uint64_t grabbed_ = 0;
    uint64_t dropped_ = 0;
    uint64_t max_frames_ = 0;   // 0 = 不限；>0 = 收滿自動停（每片 N 張，送出幀單位）

    // 拼接（open 時決定；stitch_>1 才用 stitch_buf_，於 open 預先配置，取像中不配置）
    uint32_t             stitch_      = 1;
    int64_t              chunk_bytes_ = 0;   // 單張相機幀大小
    std::vector<uint8_t> stitch_buf_;
};
