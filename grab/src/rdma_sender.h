#pragma once
// RdmaSender — Grab 端 RDMA 發送（single-slot，同步）
// 封裝 RcConn::connect → MrInfo 握手 → per-frame post_write_imm。
// Step 2 用同步 poll_one（逐幀等 NIC 送完），驗通後可改 pipeline。

#include "rdma_common.h"
#include "../../shared/FrameHeader.h"

#include <cstdint>
#include <string>
#include <vector>

class RdmaSender {
public:
    // connect：向 IP server 建立 RDMA 連線，並完成 single-slot MrInfo 握手。
    // max_payload_bytes：一幀最大影像位元組數（由 pylon PayloadSize 取得）。
    // 成功回傳 true；失敗印 stderr 並回傳 false。
    bool connect(const char* spark_ip, const char* port, size_t max_payload_bytes);

    // send_frame：組 FrameHeader + 複製影像 → post_write_imm → poll_one。
    // 呼叫前必須先 connect()。panel_id_hash 由呼叫端用 frame_panel_hash() 計算。
    void send_frame(uint16_t cam_id, uint64_t frame_seq, uint32_t panel_id_hash,
                    const uint8_t* payload, uint32_t payload_bytes,
                    uint32_t width, uint32_t height);

    void disconnect();

    bool        is_connected() const { return connected_; }
    uint64_t    sent_frames()  const { return sent_frames_; }
    uint64_t    sent_bytes()   const { return sent_bytes_; }

private:
    RcConn   conn_;
    MrInfo   remote_{};
    ibv_mr*  mr_      = nullptr;
    ibv_mr*  ctrl_mr_ = nullptr;
    std::vector<uint8_t> txbuf_;
    std::vector<uint8_t> ctrl_buf_;
    bool     connected_   = false;
    uint64_t sent_frames_ = 0;
    uint64_t sent_bytes_  = 0;
};
