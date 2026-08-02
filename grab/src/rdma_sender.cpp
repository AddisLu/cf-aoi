// ═══ 📖 手冊對照（docs/html/cf-aoi-training.html，開啟後 ⌘K 搜章節）═══
// [手冊 ch5] credit 背壓動畫（本檔=送端半邊）/ 33fps 牆（min_rnr_timer）
// [手冊 r2] R2.2 進入點表（⚠ 失敗後靜默丟幀=審計 P0-7，上線前必修）
// [手冊 p7] 模擬器案件一：凌晨斷供的完整排障劇本
// ═══════════════════════════════════════════════════════════════
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

// app-CRC 計算（可在 send_mtx 之外先算好 → 37 台共用一條 QP 時不佔序列化區間）。
// CFAOI_RDMA_NOCRC=1 → 回 0（跳過；RDMA RC 已保證有序無損送達）。
uint32_t RdmaSender::crc_of(const uint8_t* payload, uint32_t payload_bytes) {
    static const bool s_nocrc = std::getenv("CFAOI_RDMA_NOCRC") != nullptr;
    return s_nocrc ? 0u : crc32_ieee(payload, payload_bytes);
}

void RdmaSender::send_frame(uint16_t cam_id, uint64_t frame_seq, uint32_t panel_id_hash,
                             const uint8_t* payload, uint32_t payload_bytes,
                             uint32_t width, uint32_t height,
                             uint16_t slice_index, uint16_t total_slice,
                             uint32_t payload_crc32) {
    // 手填 FrameHeader（不用 make_frame_header，因為 frameSeq 需要 uint64）
    FrameHeader h{};
    h.magic        = FRAME_MAGIC;
    h.version      = FRAME_VERSION;
    h.headerBytes  = sizeof(FrameHeader);
    h.frameSeq     = frame_seq;
    h.panelId      = panel_id_hash;
    h.camId        = cam_id;
    h.sliceIndex   = slice_index;
    h.totalSlice   = total_slice;
    h.scanStep     = 0;
    h.width        = width;
    h.height       = height;
    h.bitDepth     = 8;
    h.pixelFormat  = 0;   // Mono8
    h.ptpTimestampNs = 0; // Phase-2 無 PTP
    h.machineCoordX  = 0;
    h.machineCoordY  = 0;
    h.payloadBytes = payload_bytes;
    // ⚠️ CRC 由呼叫端用 crc_of() 在 send_mtx **之外**先算好再傳進來。
    // 原本在此處算 → 落在 send_mtx 鎖內 → 37 台共用一條 QP 時全部序列化：
    // 生產幀 40.8MB 的 CRC 實測 ~16ms/幀 → 序列上限僅 ~50 幀/s，而 37 台 @12kHz 需要 88.8 幀/s。
    // 實測（8160×5000、rdma_nslot_test）：含 CRC 39.2 幀/s vs 關 CRC 90.3 幀/s（2.3×）。
    h.crc32        = payload_crc32;

    // ⚠️ 已知限制（docs/code_review_20260802.md B2，= 審計 P0-7「上線前必修」）：斷線後每一幀在此
    //   靜默丟棄——dropped 不加、CHECK_HEALTH 無 error 欄位，僅 sent_frames 凍結可察覺；無自動重連。
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

    // SEND（非 RDMA WRITE）：**不指定遠端位址** —— 資料落在 IP 端下一個 recv WQE 指向的 slot。
    // IP 端處理完某 slot 才 post 指向它的 WQE ⇒ 該 slot 不可能在 IP 讀取期間被覆寫。
    // （舊法自行算 write_addr 直接寫，RNR 擋不住 payload 落地 → 實機 CRC 損毀，見 rdma_common.h 註解。）
    // 背壓不變：IP 端 WQE 用完 → 本 SEND 收 RNR（rnr_retry_count=7=∞）→ completion 不回來
    //           → 下一幀的 poll_one() 阻塞 → 自然背壓。
    // seq 不再靠 imm 傳遞，收端從 payload 內的 FrameHeader.frameSeq 取得（uint64，不受 32-bit 截斷）。
    uint32_t total = (uint32_t)(sizeof(h) + payload_bytes);
    try {
        conn_.post_send(mr_, buf, total);
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
    // ⚠️ 已知限制（docs/code_review_20260802.md B3）：connected_ 已因送/poll 失敗轉 false 時，
    //   此處直接返回——QP/MR/event channel 不清理；之後重 connect 會覆蓋 RcConn 指標（資源洩漏）。
    if (!connected_) return;
    // 排空剩餘 in-flight SEND 完成（確保最後幾幀資料確實送達後才關連線；舊註解寫 WRITE 為 ★2 前殘留）
    try { while (posted_ > 0) { conn_.poll_one(); --posted_; } }
    catch (...) { /* 對端可能已斷，忽略殘餘完成 */ }
    posted_ = 0;
    conn_.close();
    mr_       = nullptr;
    ctrl_mr_  = nullptr;
    connected_ = false;
    printf("[rdma_sender] 已斷線\n");
}
