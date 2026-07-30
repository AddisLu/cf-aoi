#pragma once
// =============================================================================
// RdmaImageSource — Step 3 RDMA N-slot ring buffer 接收端（IP 側）
// =============================================================================
// 改自 Reference/cfaoi_phase1/t40_e2e_server.cpp，升級為 N-slot + credit 背壓。
//
// 設計（⚠️ 2026-07-30 資料路徑由 RDMA_WRITE_WITH_IMM 改為 SEND/RECV，見 rdma_common.h 註解）：
//   N 個 slot（cudaHostAlloc Portable|Mapped，GB10 用；不用 nvidia_peermem）
//   N 個 post_recv **各指向一個 slot**（wr_id = slot 編號）= N 個初始 credit
//   握手 SEND MrInfoEx{base_addr, rkey, n_slots, slot_size} 給 Grab
//     （addr/rkey 改 SEND 後資料路徑已不用，保留於 wire 供相容/診斷；n_slots/slot_size 仍必要）
//
//   recv_thread：
//     poll_one_nonblock + 100μs sleep（可中斷，running_ 旗標控制）
//     → IBV_WC_RECV → slot_id = wc.wr_id（**不再用 seq % n_slots 推算**）
//     → seq 取自 payload 內的 FrameHeader.frameSeq（SEND 無 imm；uint64 不受 32-bit 截斷）
//     → 驗 magic / byte_len 對帳 / CRC → memcpy slot → push_blocking（阻塞等 FrameQueue 有位置）
//     → 重掛指向本 slot 的 WQE（= 釋放此 slot）
//
//   ★ 正確性保證（改 SEND 的理由）：SEND 的資料落點由收端 WQE 決定，WQE 未重掛時
//     RNR 發生在「放置資料之前」→ slot 不可能在讀取期間被覆寫（by construction）。
//     舊 WRITE 版由送端自算位址，RNR 只擋 imm、payload 照樣落地並在重試時反覆重寫 → 實機 CRC 損毀。
//
//   背壓鏈（行為不變）：
//     process_image 慢 → FrameQueue 滿 → push_blocking 阻塞 → 不重掛 WQE
//     → Grab SEND 觸發 RNR（rnr_retry_count=7=∞）→ Grab poll_one 阻塞
//
// main loop 呼叫 next_frame()（= FrameQueue::pop()），其他模式（offline-tcp）介面相同。
// =============================================================================

#include "image_source.h"
#include "rdma_common.h"
#include <atomic>
#include <cstdint>
#include <mutex>
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

    // 被丟棄的幀（CRC/magic/尺寸驗證失敗）。丟棄是對的——絕不能拿壞資料判缺陷——
    // 但必須讓消費端知道「哪一片少了哪一張」，否則會變成靜默漏檢。
    // header_ok=false 表示連 header 都不可信（magic/尺寸失敗）→ cam_id/slice 不可用。
    struct LostFrame {
        uint16_t cam_id = 0;
        uint16_t slice_index = 0;
        uint16_t total_slice = 1;
        uint64_t frame_seq = 0;
        bool     header_ok = false;   // true = CRC 失敗但 header 通過驗證，可歸屬到 cam/slice
    };
    // 取出並清空累積的遺失記錄（消費端每處理一幀呼叫一次）。
    std::vector<LostFrame> take_lost();

private:
    void recv_thread_fn();

    RcConn   conn_;
    void*    ring_buf_ = nullptr;  // cudaHostAlloc N×slot_size
    ibv_mr*  ring_mr_  = nullptr;
    ibv_mr*  rx_mr_    = nullptr;  // 小 buffer（舊 WRITE_WITH_IMM 路徑遺留；改 SEND 後資料路徑不用）
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

    std::mutex             lost_mtx_;   // recv_thread 寫、消費端 take_lost() 讀
    std::vector<LostFrame> lost_;

    std::string panel_id_;  // 最近一幀 panel_id（next_frame 後更新）
};
