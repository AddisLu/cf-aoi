#ifndef CFAOI_TCP_SOURCE_H
#define CFAOI_TCP_SOURCE_H

/**
 * ============================================================================
 * TcpImageSource — offline-tcp（Step 1）
 * ============================================================================
 *
 * Control 透過 TCP 把影像送來（JSON header 命令 + binary payload）。
 * 實際的網路接收由 ControlServer 負責（SEND_IMAGE_FOR_REVIEW 指令），
 * 收到的影像被推入共用的 FrameQueue。
 *
 * TcpImageSource 只是 FrameQueue 的消費者轉接：next_frame() 阻塞等待
 * 下一張影像，連線結束 / 關機時 queue.close() 使其回傳 false。
 *
 * 這樣 control_server（網路前端）與 pipeline（消費端）完全解耦。
 * ============================================================================
 */

#include "image_source.h"

class TcpImageSource : public IImageSource {
public:
    explicit TcpImageSource(FrameQueue& queue) : queue_(queue) {}

    bool next_frame(FrameHeader& hdr, std::vector<uint8_t>& payload) override;

    // 最近一張影像的 panel_id（result 命名 / 回報用）。
    const std::string& current_panel_id() const { return panel_id_; }

private:
    FrameQueue& queue_;
    std::string panel_id_;
};

#endif // CFAOI_TCP_SOURCE_H
