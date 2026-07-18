#pragma once
// RdmaSender — Grab 端 RDMA 發送（N-buffer 非同步 pipeline）
// 封裝 RcConn::connect → MrInfoEx 握手 → 多筆 in-flight post_write_imm。
// 2026-06-23：由「single-slot 同步逐幀 poll」升級為「N 緩衝、≤N 筆 in-flight、lazy FIFO poll」，
// 解除送端逐幀等待的吞吐瓶頸（wire 格式不變，與 IP/grab 完全相容）。

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
    // slice_index/total_slice：每片 N 張切片標記（預設 0/1 = legacy 單幀語意，舊呼叫端不變）。
    // ⚠ 非 thread-safe：多相機 thread 共用一條 QP 時，呼叫端負責序列化（見 main.cpp send_mtx）。
    void send_frame(uint16_t cam_id, uint64_t frame_seq, uint32_t panel_id_hash,
                    const uint8_t* payload, uint32_t payload_bytes,
                    uint32_t width, uint32_t height,
                    uint16_t slice_index = 0, uint16_t total_slice = 1);

    void disconnect();

    bool        is_connected() const { return connected_; }
    uint64_t    sent_frames()  const { return sent_frames_; }
    uint64_t    sent_bytes()   const { return sent_bytes_; }

private:
    RcConn    conn_;
    MrInfoEx  remote_{};   // Step 3：N-slot 握手（addr/rkey/n_slots/slot_size）
    ibv_mr*   mr_      = nullptr;
    ibv_mr*   ctrl_mr_ = nullptr;
    std::vector<uint8_t> txbuf_;     // N_buf × frame_cap（多緩衝環，多筆 in-flight）
    std::vector<uint8_t> ctrl_buf_;
    size_t   frame_cap_   = 0;       // 每幀緩衝大小 = sizeof(FrameHeader)+max_payload
    uint32_t n_buf_       = 1;       // 送端緩衝數（= remote n_slots）
    uint32_t posted_      = 0;       // 已 post 未 poll 的 WRITE 數（pipeline 深度，≤ n_buf_）
    bool     connected_   = false;
    uint64_t sent_frames_ = 0;
    uint64_t sent_bytes_  = 0;
};
