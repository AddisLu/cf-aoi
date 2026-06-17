#pragma once
// CamPylon — Basler pylon 單台相機擷取（升級自 t31_pylon_grab）
// 在獨立 thread 跑 GrabStrategy_OneByOne 持續迴圈，每幀呼叫 FrameCb。
// Stop 後可重新 start（同一個 camera 物件）。

#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <thread>

// 每幀回呼：cam_id / raw pixels / 位元組數 / 寬 / 高
using FrameCb = std::function<void(uint16_t cam_id,
                                   const uint8_t* data, uint32_t bytes,
                                   uint32_t width, uint32_t height)>;

class CamPylon {
public:
    CamPylon() = default;
    ~CamPylon() { stop(); }

    // open：初始化相機（auto = 第一台，或給序號）、設 GevSCPSPacketSize。
    // 成功後可呼叫 payload_size() 取得幀大小，再去連 RDMA。
    bool open(const std::string& serial = "auto", int64_t pkt_size = 8192);

    int64_t payload_size() const { return payload_; }

    void set_frame_callback(FrameCb cb) { cb_ = std::move(cb); }

    // start/stop：控制持續取像 thread。start 前必須先 open() 且設好 callback。
    void start(uint16_t cam_id);
    void stop();

    bool     is_open()    const { return opened_; }
    bool     is_running() const { return running_.load(); }
    uint64_t grabbed()    const { return grabbed_; }
    uint64_t dropped()    const { return dropped_; }

    // Gap #2：曝光 / 增益（Stage 0 確認：ExposureTimeAbs µs, GainRaw int 256~2047）
    // 相機必須已 open()；acquisition 中可寫（TLParamsLocked=0）。
    // set_params: 寫 → read-back actual → 回傳 true/false
    // get_params: 直接從相機讀（不觸碰 JSON）
    bool set_params(float exposure_us, int gain_raw,
                    float& exp_actual, int& gain_actual);
    bool get_params(float& exp_actual, int& gain_actual);

private:
    void grab_loop();

    // pylon 物件用 void* 持有，避免 pylon headers 污染包含 cam_pylon.h 的非 pylon 檔案
    void*    camera_ptr_  = nullptr;  // CInstantCamera*
    bool     opened_      = false;
    int64_t  payload_     = 0;
    uint16_t cam_id_      = 0;
    FrameCb  cb_;

    std::atomic<bool> stop_flag_{false};
    std::atomic<bool> running_{false};
    std::thread thread_;

    uint64_t grabbed_ = 0;
    uint64_t dropped_ = 0;
};
