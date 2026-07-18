#pragma once
// CamManager — 多相機陣列管理（STATUS #94；docs plan「37 CCD 觸發設計」）
//
// 軟體觸發架構：GRAB_START = 觸發。逐台平行 arm（StartGrabbing），啟動順序/skew 不影響
// 座標精度——同舊系統「逐台 arm + 共用觸發」原則，新架構的啟動時間差由 IP 端玻璃前緣
// 對位（edge_check + per-CCD 對位）吸收，前提是前緣落在第一張（5000 條 = 40mm 窗口）內。
// 每台收滿 frames_per_panel 張自動停（CamPylon::set_max_frames）。
//
// cam_id 暫依列舉順序派 0..N-1（#21 MAC 穩定映射未做前的過渡；同 CamInfo.cam_id 語意）。

#include "cam_pylon.h"

#include <memory>
#include <string>
#include <vector>

class CamManager {
public:
    struct Entry {
        std::unique_ptr<CamPylon> cam;
        uint16_t    cam_id = 0;
        std::string serial;   // 開機時鎖定的序號（列舉快照；重插拔後 cam_id 可能變 → #21）
    };

    ~CamManager() { stop_all(); }

    // 開 want 台（want<=0 = ALL 列舉到的）。任一台開失敗 → 全關、回 false（fail-fast 不半開）。
    // want==1 沿用舊單台語意：cli_serial（auto = 第一台 / 指定序號）。
    // want>1：列舉後依序取前 want 台，各依序號開（不可兩台都 "auto"）。
    bool open_all(int want, const std::string& cli_serial, int64_t pkt_size, std::string& err);

    // 平行啟動全部（每台自帶 grab thread）；max_frames_per_cam=0 → 連續（legacy）。
    // cb 會被 N 個相機 thread 併發呼叫 → 呼叫端負責 thread-safe（RDMA 單 QP 需序列化）。
    void start_all(uint64_t max_frames_per_cam, FrameCb cb);
    void stop_all();   // 停 thread + 關相機 + 清列表

    size_t    size()  const { return cams_.size(); }
    bool      empty() const { return cams_.empty(); }
    CamPylon* get(int cam_id);
    CamPylon* primary() { return cams_.empty() ? nullptr : cams_.front().cam.get(); }

    // idle 調參路徑（TUNE_MEAN / GET_CAM_NODES）：尚無相機時開單台（舊語意），有則回第一台。
    CamPylon* get_or_open_primary(const std::string& cli_serial, int64_t pkt_size);

    int64_t  max_payload() const;      // 所有台最大 PayloadSize（RDMA frame_cap 用）
    uint64_t total_grabbed() const;
    uint64_t total_dropped() const;
    size_t   running_count() const;    // 仍在取像的台數（收滿自動停後遞減）

    std::vector<Entry>& entries() { return cams_; }

private:
    std::vector<Entry> cams_;
};
