// =============================================================================
// rdma_source.cpp — RdmaImageSource N-slot ring buffer 實作
// 改自 Reference/cfaoi_phase1/t40_e2e_server.cpp，升級為 N-slot + credit 背壓。
// =============================================================================
#include "rdma_source.h"
#include "diag/flight_recorder.h"
#include "FrameHeader.h"

#include <arpa/inet.h>
#include <chrono>
#include <cstring>
#include <cstdio>
#include <stdexcept>
#include <thread>

#include <cuda_runtime.h>

#define CUDA_OK(x) do {                                                    \
    cudaError_t _e = (x);                                                  \
    if (_e != cudaSuccess)                                                 \
        throw std::runtime_error(std::string("CUDA: ") +                  \
                                 cudaGetErrorString(_e));                  \
} while(0)

// ---------------------------------------------------------------------------
bool RdmaImageSource::init(const std::string& bind_ip, const std::string& port,
                            uint32_t n_slots, uint32_t max_payload,
                            FrameQueue& queue) {
    n_slots_   = n_slots;
    slot_size_ = (uint32_t)(sizeof(FrameHeader) + max_payload);
    queue_     = &queue;

    size_t ring_bytes = (size_t)n_slots_ * slot_size_;

    printf("[rdma_source] 初始化 N-slot ring：n_slots=%u slot_size=%uMB total=%zuMB\n",
           n_slots_, slot_size_ >> 20, ring_bytes >> 20);

    // ── ① 配置 N-slot pinned memory（GB10：cudaHostAlloc，不用 nvidia_peermem）────
    try {
        CUDA_OK(cudaHostAlloc(&ring_buf_, ring_bytes,
                              cudaHostAllocPortable | cudaHostAllocMapped));
        memset(ring_buf_, 0, ring_bytes);
    } catch (const std::exception& e) {
        fprintf(stderr, "[rdma_source] cudaHostAlloc 失敗：%s\n", e.what());
        return false;
    }

    try {
        // ── ② 建立 RDMA server（serve → make_qp，在 accept 前完成）──────────────
        conn_.serve(bind_ip.c_str(), port.c_str());

        // 註冊整塊 ring buffer（一個 MR，一個 rkey）
        ring_mr_ = conn_.reg(ring_buf_, ring_bytes,
                             IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);

        // 小 dummy buffer：WRITE_WITH_IMM 消耗 RR 時不寫此 buffer（imm 才是關鍵）
        rx_small_ = new uint8_t[4]();
        rx_mr_ = conn_.reg(rx_small_, 4, IBV_ACCESS_LOCAL_WRITE);

        // ── ③ 預掛 N 個 post_recv（= N 個初始 credit）—— 必須在 accept_conn 前 ──
        for (uint32_t i = 0; i < n_slots_; ++i)
            conn_.post_recv(rx_mr_, rx_small_, 4);

        // 接受 Grab 連線
        conn_.accept_conn();

        // ── ④ SEND MrInfoEx 給 Grab（N-slot 握手）─────────────────────────────
        ctrl_buf_.assign(sizeof(MrInfoEx), 0);
        ctrl_mr_ = conn_.reg(ctrl_buf_.data(), ctrl_buf_.size(), IBV_ACCESS_LOCAL_WRITE);

        MrInfoEx mi{};
        mi.addr      = (uint64_t)ring_buf_;
        mi.rkey      = ring_mr_->rkey;
        mi.len       = (uint32_t)ring_bytes;
        mi.crc       = 0;
        mi.n_slots   = n_slots_;
        mi.slot_size = slot_size_;
        memcpy(ctrl_buf_.data(), &mi, sizeof(mi));

        conn_.post_send(ctrl_mr_, ctrl_buf_.data(), sizeof(mi));
        conn_.poll_one();  // 吃 SEND 完成事件

        printf("[rdma_source] MrInfoEx 已送出：addr=0x%lx rkey=0x%x n_slots=%u slot_size=%uMB\n",
               (unsigned long)ring_buf_, ring_mr_->rkey, n_slots_, slot_size_ >> 20);
        printf("[rdma_source] 開始接收幀（Grab 可送幀了）...\n");

    } catch (const std::exception& e) {
        fprintf(stderr, "[rdma_source] RDMA init 失敗：%s\n", e.what());
        if (ring_buf_) { cudaFreeHost(ring_buf_); ring_buf_ = nullptr; }
        return false;
    }

    // ── ⑤ 啟動 recv_thread ───────────────────────────────────────────────────
    running_.store(true);
    recv_thread_ = std::thread([this] { recv_thread_fn(); });
    return true;
}

// ---------------------------------------------------------------------------
// recv_thread：非阻塞 poll + 100μs sleep（可被 running_=false 中斷）。
//
// ★ 釘點 1 的正確順序（sequential statements in single thread）：
//   [1] memcpy slot → payload（CPU 讀 pinned memory，slot data 安全複製出來）
//   [2] push_blocking（阻塞等 FrameQueue 有位置，payload 已 move 進佇列）
//   [3] post_recv（補 credit，此後 Grab 可重用此 slot）
//   C++17 happens-before + mutex release in push_blocking → 不需額外 fence。
//   只要 post_recv 在 push_blocking 之後，slot 就不會被 Grab 在 CPU 讀期間覆蓋。
// ---------------------------------------------------------------------------
void RdmaImageSource::recv_thread_fn() {
    while (running_.load(std::memory_order_relaxed)) {
        ibv_wc wc{};
        bool got = false;
        try {
            got = conn_.poll_one_nonblock(wc);
        } catch (const std::exception& e) {
            if (running_.load())
                fprintf(stderr, "[rdma_source] poll 失敗：%s\n", e.what());
            break;
        }

        if (!got) {
            std::this_thread::sleep_for(std::chrono::microseconds(100));
            continue;
        }

        if (wc.opcode != IBV_WC_RECV_RDMA_WITH_IMM) {
            fprintf(stderr, "[rdma_source] WARN: 非 WRITE_WITH_IMM（opcode=%d）\n",
                    (int)wc.opcode);
            continue;
        }

        uint32_t seq     = ntohl(wc.imm_data);
        uint32_t slot_id = seq % n_slots_;
        uint8_t* slot    = (uint8_t*)ring_buf_ + (size_t)slot_id * slot_size_;

        // 讀取 FrameHeader（pinned memory，CPU 直讀，無需 cudaMemcpy）
        FrameHeader h{};
        memcpy(&h, slot, sizeof(h));

        // magic / version 快速檢查
        if (h.magic != FRAME_MAGIC || h.version != FRAME_VERSION) {
            fprintf(stderr, "[rdma_source] ERR seq=%u slot=%u magic/version 不符"
                    "（magic=0x%08x）\n", seq, slot_id, h.magic);
            diag::FlightRecorder::instance().record_incident("frame_validation",
                "rdma magic/version seq=" + std::to_string(seq) +
                " slot=" + std::to_string(slot_id));
            ++recv_err_;
            conn_.post_recv(rx_mr_, rx_small_, 4);  // 補 credit（錯誤幀也要補）
            continue;
        }

        // payload 大小防呆
        size_t total = sizeof(FrameHeader) + h.payloadBytes;
        if (total > slot_size_) {
            fprintf(stderr, "[rdma_source] ERR seq=%u slot=%u total=%zu > slot_size=%u\n",
                    seq, slot_id, total, slot_size_);
            ++recv_err_;
            conn_.post_recv(rx_mr_, rx_small_, 4);
            continue;
        }

        // CRC 驗證（payload 在 pinned memory 中，CPU 直接讀）
        uint32_t crc = crc32_ieee(slot + sizeof(FrameHeader), h.payloadBytes);
        if (crc != h.crc32) {
            fprintf(stderr, "[rdma_source] ERR seq=%u slot=%u CRC 不符"
                    "（got=0x%08x want=0x%08x）\n", seq, slot_id, crc, h.crc32);
            diag::FlightRecorder::instance().record_incident("frame_validation",
                "rdma crc seq=" + std::to_string(seq) +
                " slot=" + std::to_string(slot_id));
            ++recv_err_;
            conn_.post_recv(rx_mr_, rx_small_, 4);
            continue;
        }

        // ── ★ 釘點 1 [1]：memcpy slot → payload（slot data 安全複製出來）──────
        // 此 memcpy 完成後，slot 的資料已在 payload 中；無論 Grab 是否覆蓋 slot，
        // payload 的內容不受影響。
        std::vector<uint8_t> payload(h.payloadBytes);
        memcpy(payload.data(), slot + sizeof(FrameHeader), h.payloadBytes);

        // panel_id：RDMA 模式以 "rdma_seq_NNN" 合成（FrameHeader 只帶 hash，不帶字串）
        std::string panel = "rdma_seq_" + std::to_string(seq);

        // ── ★ 釘點 1 [2]：push_blocking（阻塞等 FrameQueue 有位置）─────────────
        // 佇列滿時此處阻塞 → recv_thread 不繼續 post_recv → Grab credit 耗盡 → RNR
        // 返回後 payload 已 move 進 FrameQueue（payload 現為空 vector）
        queue_->push_blocking(h, panel, std::move(payload));

        // ── ★ 釘點 1 [3]：post_recv（補 credit，在 memcpy + push_blocking 後）──
        // 此後 Grab 可重用 slot_id 的 ring 位置（seq+n_slots 會寫到同一 slot）
        conn_.post_recv(rx_mr_, rx_small_, 4);

        ++recv_ok_;

        if (recv_ok_.load() % 20 == 0 || recv_ok_.load() <= 5) {
            printf("[rdma_source] 已收 %llu 幀 ok / %llu err（slot=%u seq=%u CRC=OK）\n",
                   (unsigned long long)recv_ok_.load(),
                   (unsigned long long)recv_err_.load(),
                   slot_id, seq);
        }
    }
    printf("[rdma_source] recv_thread 結束（ok=%llu err=%llu）\n",
           (unsigned long long)recv_ok_.load(),
           (unsigned long long)recv_err_.load());
    // Grab 斷線後 recv_thread 自然退出，需關閉 queue 讓主迴圈的 next_frame/pop 返回 false。
    // stop() 正常路徑也會呼叫 queue_->close()，重複呼叫安全（FrameQueue::close 是冪等的）。
    if (queue_) queue_->close();
}

// ---------------------------------------------------------------------------
bool RdmaImageSource::next_frame(FrameHeader& hdr, std::vector<uint8_t>& payload) {
    FrameQueue::Item item;
    if (!queue_->pop(item)) return false;
    hdr      = item.hdr;
    payload  = std::move(item.payload);
    panel_id_ = item.panel_id;
    return true;
}

// ---------------------------------------------------------------------------
void RdmaImageSource::stop() {
    if (!running_.exchange(false)) return;  // 避免重複 stop

    // ① 關閉 FrameQueue → push_blocking / pop 均感知 closed_ 並返回
    if (queue_) queue_->close();

    // ② 等 recv_thread 退出（running_=false + queue close → poll 迴圈自然結束）
    if (recv_thread_.joinable()) recv_thread_.join();

    // ③ 釋放 RDMA MR（必須在 conn_.close() 之前）
    if (ctrl_mr_) { ibv_dereg_mr(ctrl_mr_); ctrl_mr_ = nullptr; }
    if (rx_mr_)   { ibv_dereg_mr(rx_mr_);   rx_mr_   = nullptr; }
    if (ring_mr_) { ibv_dereg_mr(ring_mr_); ring_mr_ = nullptr; }

    // ④ 關閉 QP/CQ/PD/CM（recv_thread 已退出，安全）
    conn_.close();

    // ⑤ 釋放 pinned memory
    if (ring_buf_) { cudaFreeHost(ring_buf_); ring_buf_ = nullptr; }
    delete[] rx_small_; rx_small_ = nullptr;

    printf("[rdma_source] 已停止\n");
}
