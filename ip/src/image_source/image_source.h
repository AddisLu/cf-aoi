#ifndef CFAOI_IMAGE_SOURCE_H
#define CFAOI_IMAGE_SOURCE_H

/**
 * ============================================================================
 * IImageSource — 影像來源統一介面
 * ============================================================================
 *
 * 每個 mode 對應一個 IImageSource 實作：
 *   offline-file → FileImageSource（讀 tif/bmp/png）
 *   offline-tcp  → TcpImageSource（Control 透過 TCP 傳來）
 *   rdma-*       → RdmaImageSource（需 libibverbs，本檔不含）
 *
 * main loop：while (src->next_frame(hdr, buf)) { pipeline.process_frame(...) }
 *
 * 同時提供一個 thread-safe FrameQueue，供 control_server（生產者）與
 * TcpImageSource（消費者）解耦。
 * ============================================================================
 */

#include <cstdint>
#include <string>
#include <vector>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <utility>
#include <cstddef>

#include "FrameHeader.h"

// 影像來源介面。next_frame 阻塞直到下一張影像可用；
// 回傳 false 代表來源已結束（檔案讀完 / 連線關閉 / 收到關機訊號）。
class IImageSource {
public:
    virtual ~IImageSource() = default;

    // 取得下一張影像：填入 header 與 payload（grayscale 8-bit）。
    virtual bool next_frame(FrameHeader& hdr, std::vector<uint8_t>& payload) = 0;
};

// ----------------------------------------------------------------------------
// FrameQueue — 多生產者 / 單消費者阻塞佇列
// ----------------------------------------------------------------------------
class FrameQueue {
public:
    struct Item {
        FrameHeader hdr;
        std::string panel_id;   // 原始 panel_id 字串（header 只存 hash）
        std::vector<uint8_t> payload;
    };

    // 推入幀。若 max_size_ > 0 且 queue 已滿，返回 false（背壓：呼叫方應回 ERR 拒收此幀）。
    // max_size_ == 0 = 舊行為（無上限），向下相容。
    bool push(FrameHeader hdr, std::string panel_id, std::vector<uint8_t> payload) {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            if (max_size_ > 0 && q_.size() >= max_size_) return false;
            q_.push(Item{hdr, std::move(panel_id), std::move(payload)});
        }
        cv_.notify_one();
        return true;
    }

    // 阻塞直到有資料或被 close()。回傳 false 代表已關閉且佇列清空。
    bool pop(Item& out) {
        std::unique_lock<std::mutex> lk(mtx_);
        cv_.wait(lk, [&] { return closed_ || !q_.empty(); });
        if (q_.empty()) return false;  // closed
        out = std::move(q_.front());
        q_.pop();
        return true;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            closed_ = true;
        }
        cv_.notify_all();
    }

    size_t size() const {
        std::lock_guard<std::mutex> lk(mtx_);
        return q_.size();
    }

    // 啟動時由 buffer 計算器設置；0 = 無上限。啟動後不可動態增大（不變式）。
    void set_max_size(size_t n) { max_size_ = n; }
    size_t max_size() const { return max_size_; }

private:
    mutable std::mutex mtx_;
    std::condition_variable cv_;
    std::queue<Item> q_;
    bool closed_ = false;
    size_t max_size_ = 0;  // 0 = 無上限（向下相容）；set_max_size 後固定
};

#endif // CFAOI_IMAGE_SOURCE_H
