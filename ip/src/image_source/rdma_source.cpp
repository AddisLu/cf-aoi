// ═══ 📖 手冊對照（docs/html/cf-aoi-training.html，開啟後 ⌘K 搜章節）═══
// [手冊 ch5] N-slot ring + credit 背壓動畫（slot=seq%4、RNR 等待可視化）
// [手冊 r2] R2.3 兩端握手/背壓對照圖（本檔=IP 半邊）
// [手冊 p2] frame_validation 破案卡（NOCRC 兩端一致性）
// ═══════════════════════════════════════════════════════════════
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
#include <cstdlib>
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

        // 小 dummy buffer：保留給控制訊息路徑（資料路徑已改 SEND，不再使用）
        rx_small_ = new uint8_t[4]();
        rx_mr_ = conn_.reg(rx_small_, 4, IBV_ACCESS_LOCAL_WRITE);

        // ── ③ 預掛 N 個 post_recv，**每個指向一個 slot**（= N 個初始 credit）──────
        // 資料路徑改 SEND 後，資料落點由這裡的 WQE 決定（不再由 Grab 指定位址）。
        // wr_id = slot 編號 → 完成事件可反查資料落在哪個 slot。必須在 accept_conn 前掛好。
        for (uint32_t i = 0; i < n_slots_; ++i)
            conn_.post_recv(ring_mr_, (uint8_t*)ring_buf_ + (size_t)i * slot_size_,
                            slot_size_, /*wr_id=*/i);

        // 接受 Grab 連線
        conn_.accept_conn();

        // ── ③.5 設小 min_rnr_timer ───────────────────────────────────────────────
        // 預設 index 0 = 655.36ms：送端 credit 短暫耗盡 → RNR NAK → 等 655ms → 吞吐崩潰（實測 2.6fps）。
        // 改 index 12 ≈ 0.64ms：RNR 時快速重試，吞吐回到「消費端速率」而非長等。
        // （responder=收端 QP 的 min_rnr_timer 廣告給送端決定 RNR 等待；RTS 狀態下可改。）
        {
            ibv_qp_attr qa{}; qa.min_rnr_timer = 12;
            int e = ibv_modify_qp(conn_.id->qp, &qa, IBV_QP_MIN_RNR_TIMER);
            if (e) fprintf(stderr, "[rdma_source] WARN: set min_rnr_timer 失敗 (%d)\n", e);
            else   printf("[rdma_source] min_rnr_timer = index 12 (~0.64ms)\n");
        }

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
// [手冊 ch5] ring 動畫的三步就是下面 [1][2][3]——順序不可換（換了=slot 覆寫風險）
//   [1] memcpy slot → payload（CPU 讀 pinned memory，slot data 安全複製出來）
//   [2] push_blocking（阻塞等 FrameQueue 有位置，payload 已 move 進佇列）
//   [3] post_recv（補 credit，此後 Grab 可重用此 slot）
//   C++17 happens-before + mutex release in push_blocking → 不需額外 fence。
//   只要 post_recv 在 push_blocking 之後，slot 就不會被 Grab 在 CPU 讀期間覆蓋。
// ---------------------------------------------------------------------------
void RdmaImageSource::recv_thread_fn() {
    double sum_crc_ms = 0, sum_cpy_ms = 0, sum_push_ms = 0;   // [診斷] 收端各階段累計
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
            // RoCE v2：Grab 斷線後 WR_FLUSH_ERR 不保證立即出現。
            // 非阻塞輪詢 CM 事件頻道，偵測 RDMA_CM_EVENT_DISCONNECTED。
            if (conn_.check_cm_disconnect()) {
                printf("[rdma_source] 偵測到 Grab 斷線（CM DISCONNECTED）\n");
                break;
            }
            std::this_thread::sleep_for(std::chrono::microseconds(100));
            continue;
        }

        if (wc.opcode != IBV_WC_RECV) {
            fprintf(stderr, "[rdma_source] WARN: 非 SEND/RECV（opcode=%d）\n",
                    (int)wc.opcode);
            continue;
        }

        // 資料落在哪個 slot 由「我們自己掛的 WQE」決定 → wr_id 即 slot 編號（不再由 seq 推算）
        uint32_t slot_id = (uint32_t)wc.wr_id;
        if (slot_id >= n_slots_) {          // 防呆：理論上不可能，錯了寧可停也不要亂讀記憶體
            fprintf(stderr, "[rdma_source] ERR wr_id=%llu 超出 slot 範圍（n_slots=%u）\n",
                    (unsigned long long)wc.wr_id, n_slots_);
            break;
        }
        uint8_t* slot = (uint8_t*)ring_buf_ + (size_t)slot_id * slot_size_;

        // 讀取 FrameHeader（pinned memory，CPU 直讀，無需 cudaMemcpy）
        FrameHeader h{};
        memcpy(&h, slot, sizeof(h));
        // seq 改由 header 取（SEND 無 imm）；uint64 全幅，不再受 imm 的 32-bit 截斷限制
        uint64_t seq = h.frameSeq;

        // 補 credit＝重掛「指向同一個 slot」的 WQE（錯誤幀也要補，否則 credit 漏光會假死）
        auto repost_slot = [&]() {
            conn_.post_recv(ring_mr_, slot, slot_size_, /*wr_id=*/slot_id);
        };

        // magic / version 快速檢查
        if (h.magic != FRAME_MAGIC || h.version != FRAME_VERSION) {
            fprintf(stderr, "[rdma_source] ERR seq=%llu slot=%u magic/version 不符"
                    "（magic=0x%08x）\n", (unsigned long long)seq, slot_id, h.magic);
            FR_RECORD_INCIDENT("frame_validation",
                "rdma magic/version seq=" + std::to_string(seq) +
                " slot=" + std::to_string(slot_id));
            ++recv_err_;
            repost_slot();
            continue;
        }

        // payload 大小防呆（含尺寸一致性：畸形 header 不得帶著錯誤 w×h 進 cv::Mat）
        // SEND 另有 wc.byte_len＝對端實際送出的位元組數 → 可直接與 header 宣告值對帳（WRITE 時沒有）
        size_t total = sizeof(FrameHeader) + h.payloadBytes;
        const bool dim_ok = h.width >= 1 && h.width <= 16384 &&
                            h.height >= 1 && h.height <= 16384 &&
                            (uint64_t)h.payloadBytes == (uint64_t)h.width * h.height;
        const bool len_ok = (size_t)wc.byte_len == total;
        if (total > slot_size_ || !dim_ok || !len_ok) {
            fprintf(stderr, "[rdma_source] ERR seq=%llu slot=%u total=%zu byte_len=%u slot_size=%u"
                    " w=%u h=%u payload=%u（尺寸/大小不合法）\n",
                    (unsigned long long)seq, slot_id, total, wc.byte_len, slot_size_,
                    h.width, h.height, h.payloadBytes);
            FR_RECORD_INCIDENT("frame_validation",
                "rdma payload/尺寸不合法 seq=" + std::to_string(seq) +
                " slot=" + std::to_string(slot_id) +
                " byte_len=" + std::to_string(wc.byte_len) +
                " w=" + std::to_string(h.width) + " h=" + std::to_string(h.height) +
                " payload=" + std::to_string(h.payloadBytes));
            ++recv_err_;
            repost_slot();
            continue;
        }

        // ── ★ 釘點 1 [1]：memcpy slot → payload ──────────────────────────────────
        // 資料路徑改 SEND 後，slot 的安全性已由「WQE 尚未重掛」保證（Grab 無處可放 → RNR 等待），
        // 不再依賴這裡的複製速度。仍先複製出來的理由：後續 CRC/push 只碰自己的副本，
        // 讓 slot 能在 push_blocking 前就邏輯上「用完」，語意單純。
        auto _tm0 = std::chrono::steady_clock::now();
        std::vector<uint8_t> payload(h.payloadBytes);
        memcpy(payload.data(), slot + sizeof(FrameHeader), h.payloadBytes);
        sum_cpy_ms += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - _tm0).count();

        // CRC 驗證（對**已複製出來的 payload**做，不再碰 slot）
        // CFAOI_RDMA_NOCRC=1：跳過收端 app-CRC 複驗（RDMA RC 已保證有序無損送達；省 ~16ms/幀）。
        //   預設關閉(=做 CRC，較保守)；可信 fabric 求吞吐時開啟。資料若真損壞，下游缺陷結果會偏離 golden→仍可抓。
        static const bool s_nocrc = std::getenv("CFAOI_RDMA_NOCRC") != nullptr;
        if (!s_nocrc) {
            auto _tc0 = std::chrono::steady_clock::now();
            uint32_t crc = crc32_ieee(payload.data(), h.payloadBytes);
            sum_crc_ms += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - _tc0).count();
            if (crc != h.crc32) {
                fprintf(stderr, "[rdma_source] ERR seq=%llu slot=%u CRC 不符"
                        "（got=0x%08x want=0x%08x）\n",
                        (unsigned long long)seq, slot_id, crc, h.crc32);
                FR_RECORD_INCIDENT("frame_validation",
                    "rdma crc seq=" + std::to_string(seq) +
                    " slot=" + std::to_string(slot_id));
                ++recv_err_;
                repost_slot();
                continue;
            }
        }

        // （memcpy 已上移到碰 slot 的第一步；此後不再讀 slot）
        // panel_id：RDMA 模式以 "rdma_seq_NNN" 合成（FrameHeader 只帶 hash，不帶字串）
        std::string panel = "rdma_seq_" + std::to_string(seq);

        // ── ★ 釘點 1 [2]：push_blocking（阻塞等 FrameQueue 有位置）─────────────
        // 佇列滿時此處阻塞 → recv_thread 不繼續 post_recv → Grab credit 耗盡 → RNR
        // 返回後 payload 已 move 進 FrameQueue（payload 現為空 vector）
        auto _tp0 = std::chrono::steady_clock::now();
        queue_->push_blocking(h, panel, std::move(payload));
        sum_push_ms += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - _tp0).count();

        // ── ★ 釘點 1 [3]：重掛指向本 slot 的 WQE（= 釋放此 slot 給 Grab）──────────
        // 位置刻意仍放在 push_blocking 之後 → 佇列滿時不補 WQE → Grab SEND 收 RNR → 背壓（行為不變）。
        // 與舊版的差別：現在「不補 WQE」是**真的**擋得住資料落地（SEND 沒有落點就不放資料），
        // 舊版 WRITE 是送端自己決定位址，不補 WQE 只擋 imm、payload 照樣寫進來 → 覆寫損毀。
        repost_slot();

        ++recv_ok_;

        if (recv_ok_.load() % 20 == 0 || recv_ok_.load() <= 5) {
            printf("[rdma_source] 已收 %llu 幀 ok / %llu err（slot=%u seq=%llu CRC=OK）\n",
                   (unsigned long long)recv_ok_.load(),
                   (unsigned long long)recv_err_.load(),
                   slot_id, (unsigned long long)seq);
        }
    }
    {
        uint64_t n = recv_ok_.load() ? recv_ok_.load() : 1;
        printf("[rdma_source] recv_thread 結束（ok=%llu err=%llu）｜每幀均: CRC=%.1fms memcpy=%.1fms push=%.1fms\n",
               (unsigned long long)recv_ok_.load(), (unsigned long long)recv_err_.load(),
               sum_crc_ms / n, sum_cpy_ms / n, sum_push_ms / n);
    }
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
