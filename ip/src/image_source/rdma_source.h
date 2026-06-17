#pragma once
// =============================================================================
// RdmaImageSource — Step 3 RDMA N-slot ring buffer 接收端（IP 側）
// =============================================================================
// 改自 Reference/cfaoi_phase1/t40_e2e_server.cpp，升級為 N-slot + credit 背壓。
//
// 設計：
//   N 個 slot（cudaHostAlloc Portable|Mapped，GB10 用；不用 nvidia_peermem）
//   N 個 post_recv = N 個初始 credit
//   握手 SEND MrInfoEx{base_addr, rkey, n_slots, slot_size} 給 Grab
//
//   recv_thread：
//     poll_one_nonblock + 100μs sleep（可中斷，running_ 旗標控制）
//     → IBV_WC_RECV_RDMA_WITH_IMM → slot_id = seq % n_slots
//     → 驗 magic/CRC → memcpy slot → push_blocking（阻塞等 FrameQueue 有位置）
//     → post_recv（補 credit）   ★ 此順序是釘點 1 的正確性保證
//
//   背壓鏈：
//     process_image 慢 → FrameQueue 滿 → push_blocking 阻塞 → 不 post_recv
//     → Grab WRITE_WITH_IMM 觸發 RNR（rnr_retry_count=7=∞）→ Grab poll_one 阻塞
//
// main loop 呼叫 next_frame()（= FrameQueue::pop()），其他模式（offline-tcp）介面相同。
// =============================================================================

#include "image_source.h"
#include "rdma_common.h"
#include <atomic>
#include <cstdint>
#include <string>
#include <thread>
#include <vector>

class RdmaImageSource : public IImageSource {
public:
    RdmaImageSource() = default;
    ~RdmaImageSource() { stop(); }

    // init：建立 RDMA server、配置 N-slot pinned ring buffer、接受 Grab 連線、
    //       握手發送 MrInfoEx、啟動 recv_thread。
    // bind_ip   : RDMA server 綁定 IP（例 "0.0.0.0"）
    // port      : RDMA server port（例 "18515"）
    // n_slots   : ring buffer slot 數（= RDMA credit 數，預設 4）
    // max_payload: 一幀最大 payload bytes（= width×height，例 8192×5000=40MB）
    // queue     : 由 main.cpp 建立的 FrameQueue（push_blocking 寫入，next_frame pop 讀出）
    bool init(const std::string& bind_ip, const std::string& port,
              uint32_t n_slots, uint32_t max_payload, FrameQueue& queue);

    // next_frame：阻塞等 FrameQueue 有幀，消費一幀。
    // 回傳 false 代表 queue 關閉（stop() 或信號）。
    bool next_frame(FrameHeader& hdr, std::vector<uint8_t>& payload) override;

    // 最近一張影像的 panel_id（rdma_source 中為 "rdma_seq_NNN" 合成字串）
    const std::string& current_panel_id() const { return panel_id_; }

    void stop();

    uint64_t recv_ok()  const { return recv_ok_.load(); }
    uint64_t recv_err() const { return recv_err_.load(); }

private:
    void recv_thread_fn();

    RcConn   conn_;
    void*    ring_buf_ = nullptr;  // cudaHostAlloc N×slot_size
    ibv_mr*  ring_mr_  = nullptr;
    ibv_mr*  rx_mr_    = nullptr;  // 小 buffer，消耗 WRITE_WITH_IMM RR 用
    ibv_mr*  ctrl_mr_  = nullptr;  // MrInfoEx 握手 buffer MR（init 後保留至 stop）
    uint8_t* rx_small_ = nullptr;  // 4 bytes dummy recv buffer

    std::vector<uint8_t> ctrl_buf_;  // MrInfoEx 握手 buffer（必須與 ctrl_mr_ 共存亡）

    uint32_t n_slots_   = 0;
    uint32_t slot_size_ = 0;

    FrameQueue* queue_ = nullptr;

    std::thread          recv_thread_;
    std::atomic<bool>    running_{false};

    std::atomic<uint64_t> recv_ok_{0};
    std::atomic<uint64_t> recv_err_{0};

    std::string panel_id_;  // 最近一幀 panel_id（next_frame 後更新）
};
