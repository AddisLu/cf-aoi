#include "rdma_sender.h"

#include <arpa/inet.h>
#include <cstring>
#include <cstdio>
#include <stdexcept>

bool RdmaSender::connect(const char* spark_ip, const char* port, size_t max_payload_bytes) {
    size_t cap = sizeof(FrameHeader) + max_payload_bytes;

    txbuf_.resize(cap);
    ctrl_buf_.resize(sizeof(MrInfo));

    try {
        conn_.connect(spark_ip, port);

        mr_      = conn_.reg(txbuf_.data(),    txbuf_.size(),    IBV_ACCESS_LOCAL_WRITE);
        ctrl_mr_ = conn_.reg(ctrl_buf_.data(), ctrl_buf_.size(), IBV_ACCESS_LOCAL_WRITE);

        // 預掛 RECV 等 IP server SEND MrInfo
        conn_.post_recv(ctrl_mr_, ctrl_buf_.data(), (uint32_t)ctrl_buf_.size());
        conn_.poll_one();

        memcpy(&remote_, ctrl_buf_.data(), sizeof(MrInfo));
        if (remote_.len < cap) {
            fprintf(stderr, "[rdma_sender] IP buffer 太小：%u < %zu\n", remote_.len, cap);
            conn_.close();
            return false;
        }

        connected_ = true;
        printf("[rdma_sender] 連線成功，remote addr=0x%lx rkey=0x%x len=%u\n",
               (unsigned long)remote_.addr, remote_.rkey, remote_.len);
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
    h.crc32        = crc32_ieee(payload, payload_bytes);

    // [FrameHeader(256B) || payload] → txbuf
    memcpy(txbuf_.data(), &h, sizeof(h));
    memcpy(txbuf_.data() + sizeof(h), payload, payload_bytes);

    if (!connected_) return;

    uint32_t total = (uint32_t)(sizeof(h) + payload_bytes);
    try {
        conn_.post_write_imm(mr_, txbuf_.data(), total,
                             remote_.addr, remote_.rkey, (uint32_t)frame_seq);
        conn_.poll_one();  // 同步等 NIC 送完（Step 2 簡化版）
    } catch (const std::exception& e) {
        if (connected_) {
            fprintf(stderr, "[rdma_sender] 發送失敗（seq=%llu）：%s\n",
                    (unsigned long long)frame_seq, e.what());
            connected_ = false;  // 停止後續嘗試
        }
        return;
    }

    ++sent_frames_;
    sent_bytes_ += total;
}

void RdmaSender::disconnect() {
    if (!connected_) return;
    conn_.close();
    mr_       = nullptr;
    ctrl_mr_  = nullptr;
    connected_ = false;
    printf("[rdma_sender] 已斷線\n");
}
