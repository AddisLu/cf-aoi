#include "rdma_sender.h"

#include <arpa/inet.h>
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>

bool RdmaSender::connect(const char* spark_ip, const char* port, size_t max_payload_bytes) {
    frame_cap_ = sizeof(FrameHeader) + max_payload_bytes;
    ctrl_buf_.resize(sizeof(MrInfoEx));  // Step 3：接收 MrInfoEx（256 bytes）

    try {
        conn_.connect(spark_ip, port);

        // 先握手取得 n_slots（才知道要配幾個送端緩衝）
        ctrl_mr_ = conn_.reg(ctrl_buf_.data(), ctrl_buf_.size(), IBV_ACCESS_LOCAL_WRITE);
        conn_.post_recv(ctrl_mr_, ctrl_buf_.data(), (uint32_t)ctrl_buf_.size());
        conn_.poll_one();
        memcpy(&remote_, ctrl_buf_.data(), sizeof(remote_));

        // 驗證：每個 slot 必須能容納一幀
        if (remote_.n_slots == 0 || remote_.slot_size < frame_cap_) {
            fprintf(stderr, "[rdma_sender] MrInfoEx 無效：n_slots=%u slot_size=%u frame_cap=%zu\n",
                    remote_.n_slots, remote_.slot_size, frame_cap_);
            conn_.close();
            return false;
        }

        // N 緩衝環（= n_slots 個）：一塊大 buffer 一個 MR，可同時 ≤N 筆 in-flight
        n_buf_ = remote_.n_slots;
        txbuf_.resize((size_t)n_buf_ * frame_cap_);
        mr_ = conn_.reg(txbuf_.data(), txbuf_.size(), IBV_ACCESS_LOCAL_WRITE);
        posted_ = 0;

        connected_ = true;
        printf("[rdma_sender] N-slot 連線成功：n_slots=%u slot_size=%uMB addr=0x%lx rkey=0x%x（送端 %u 緩衝 async）\n",
               remote_.n_slots, remote_.slot_size >> 20,
               (unsigned long)remote_.addr, remote_.rkey, n_buf_);
        return true;

    } catch (const std::exception& e) {
        fprintf(stderr, "[rdma_sender] connect 失敗：%s\n", e.what());
        conn_.close();
        return false;
    }
}

void RdmaSender::send_frame(uint16_t cam_id, uint64_t frame_seq, uint32_t panel_id_hash,
                             const uint8_t* payload, uint32_t payload_bytes,
                             uint32_t width, uint32_t height) {
    // 手填 FrameHeader（不用 make_frame_header，因為 frameSeq 需要 uint64）
    FrameHeader h{};
    h.magic        = FRAME_MAGIC;
    h.version      = FRAME_VERSION;
    h.headerBytes  = sizeof(FrameHeader);
    h.frameSeq     = frame_seq;
    h.panelId      = panel_id_hash;
    h.camId        = cam_id;
    h.sliceIndex   = 0;
    h.totalSlice   = 1;
    h.scanStep     = 0;
    h.width        = width;
    h.height       = height;
    h.bitDepth     = 8;
    h.pixelFormat  = 0;   // Mono8
    h.ptpTimestampNs = 0; // Phase-2 無 PTP
    h.machineCoordX  = 0;
    h.machineCoordY  = 0;
    h.payloadBytes = payload_bytes;
    // CFAOI_RDMA_NOCRC=1：跳過送端 app-CRC（RDMA RC 已保證送達；省 ~16ms/幀）。預設仍算 CRC（保守）。
    static const bool s_nocrc = std::getenv("CFAOI_RDMA_NOCRC") != nullptr;
    h.crc32        = s_nocrc ? 0u : crc32_ieee(payload, payload_bytes);

    if (!connected_) return;

    // N-buffer pipeline：本幀用緩衝 buf_idx；保留 ≤ n_buf_ 筆 in-flight。
    // completion 為 FIFO（單一 RC QP 保序）→ 緩衝滿時 poll 掉最舊一筆，正好是 n_buf_ 幀前用同一 buf_idx 者，
    // 釋放後才覆寫，故同步安全（不會在 WRITE 進行中改寫緩衝）。
    uint32_t buf_idx = (uint32_t)(frame_seq % n_buf_);
    uint8_t* buf     = txbuf_.data() + (size_t)buf_idx * frame_cap_;
    try {
        while (posted_ >= n_buf_) { conn_.poll_one(); --posted_; }
    } catch (const std::exception& e) {
        if (connected_) {
            fprintf(stderr, "[rdma_sender] poll 失敗（seq=%llu）：%s\n",
                    (unsigned long long)frame_seq, e.what());
            connected_ = false;
        }
        return;
    }

    // [FrameHeader(256B) || payload] → 該緩衝
    memcpy(buf, &h, sizeof(h));
    memcpy(buf + sizeof(h), payload, payload_bytes);

    // N-slot 定址 — slot_id = frame_seq % n_slots（= buf_idx），write_addr 落在對應 slot
    // 背壓：IP credit 耗盡 → WRITE_WITH_IMM RNR（rnr_retry_count=7=∞）→ 後續 poll_one 阻塞 → 自然背壓
    uint32_t slot_id    = (uint32_t)(frame_seq % remote_.n_slots);
    uint64_t write_addr = remote_.addr + (uint64_t)slot_id * remote_.slot_size;
    uint32_t total = (uint32_t)(sizeof(h) + payload_bytes);
    try {
        conn_.post_write_imm(mr_, buf, total,
                             write_addr, remote_.rkey, (uint32_t)frame_seq);
        ++posted_;   // async：不逐幀 poll，讓 ≤ n_buf_ 筆同時 in-flight（pipeline）
    } catch (const std::exception& e) {
        if (connected_) {
            fprintf(stderr, "[rdma_sender] 發送失敗（seq=%llu）：%s\n",
                    (unsigned long long)frame_seq, e.what());
            connected_ = false;
        }
        return;
    }

    ++sent_frames_;
    sent_bytes_ += total;
}

void RdmaSender::disconnect() {
    if (!connected_) return;
    // 排空剩餘 in-flight WRITE 完成（確保最後幾幀資料確實送達後才關連線）
    try { while (posted_ > 0) { conn_.poll_one(); --posted_; } }
    catch (...) { /* 對端可能已斷，忽略殘餘完成 */ }
    posted_ = 0;
    conn_.close();
    mr_       = nullptr;
    ctrl_mr_  = nullptr;
    connected_ = false;
    printf("[rdma_sender] 已斷線\n");
}
