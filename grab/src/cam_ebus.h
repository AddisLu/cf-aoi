#pragma once
// CamEbus — Basler L803K（Camera Link 線掃）經 Pleora iPORT CL-GigE 轉 GigE Vision，用 eBUS SDK 取像（2026-10-05）。
// 介面與語意同 CamPylon（見 camera.h）：open → start(cam_id) 起取像 thread → stop 關相機。
//
// 與 raL8192（pylon）的差異：
//   - 影像由 iPORT 組幀：Width 8160（L803K 雙 tap）、Height = 每幀行數（5000，無單幀行數上限 → 不拼接）
//   - 相機參數（曝光 µs、增益 raw、行速率）不在 GenICam 上，走 iPORT Bulk0 UART → L803K 的
//     Basler L800 二進位序列協定（9600 8N1；協定細節見 cam_ebus.cpp L800Serial，移植自
//     tools/cam_align/l800_serial.py，已在調機工具上實機驗過）
//   - 身分：iPORT 的 GigE Vision 使用者名稱（DeviceUserID，0xE8）= "CCDnn"，同 raL8192 規則
//   - 列舉只收 Pleora 的裝置（eBUS 也看得到 Basler 原生 GigE 相機，要排除）
// 只用 std 型別，eBUS header 只出現在 cam_ebus.cpp（pimpl）。

#include "camera.h"

#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

class CamEbus : public ICamera {
public:
    CamEbus();
    ~CamEbus() override;

    static std::vector<CamInfo> enumerate_cameras();

    bool open(const std::string& serial, int64_t pkt_size, Roi roi, double line_rate_hz) override;

    int64_t  payload_size() const override { return payload_; }
    uint32_t stitch_count() const override { return 1; }   // iPORT 自己組 5000 行幀，不拼接

    void set_frame_callback(FrameCb cb) override { cb_ = std::move(cb); }
    void start(uint16_t cam_id) override;
    void stop() override;
    void set_max_frames(uint64_t n) override { max_frames_ = n; }

    bool     is_open()    const override { return opened_; }
    bool     is_running() const override { return running_.load(); }
    uint64_t grabbed()    const override { return grabbed_; }
    uint64_t dropped()    const override { return dropped_; }
    bool        is_faulted()    const override { return faulted_.load(); }
    std::string fault_message() const override {
        std::lock_guard<std::mutex> lk(fault_mtx_);
        return fault_msg_;
    }

    bool set_params(float exposure_us, int gain_raw, float& exp_actual, int& gain_actual) override;
    bool get_params(float& exp_actual, int& gain_actual) override;
    bool grab_one_mean(double& mean, std::string& err) override;
    bool read_machine_params(MachineParams& mp, std::string& err) override;

private:
    struct Impl;                       // eBUS 物件（PvDevice / PvStream / 序列埠 / buffers）
    std::unique_ptr<Impl> d_;

    void grab_loop();                  // thread 進入點：try/catch 薄殼（B1）
    void grab_loop_body();
    void note_fault(const char* kind, const char* what);
    void close_device();

    bool     opened_  = false;
    int64_t  payload_ = 0;
    uint32_t width_ = 0, height_ = 0;
    uint16_t cam_id_  = 0;
    FrameCb  cb_;

    std::atomic<bool> stop_flag_{false};
    std::atomic<bool> running_{false};
    std::thread thread_;

    std::atomic<bool> faulted_{false};
    mutable std::mutex fault_mtx_;
    std::string fault_msg_;

    std::atomic<uint64_t> grabbed_{0};
    std::atomic<uint64_t> dropped_{0};
    std::atomic<uint64_t> max_frames_{0};
};
