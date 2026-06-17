// =============================================================================
// rdma_nslot_test.cpp — Step 3 N-slot ring buffer 驗證用合成幀送器（不需相機）
// 用法： rdma_nslot_test <server_ip> <port> <num_frames> [width] [height] [delay_ms]
//   width/height 預設 256×256（小幀快送，驗繞回邏輯）
//   delay_ms     送幀間人工延遲（ms），預設 0；測背壓時 IP 端用 --test-consumer-delay-ms
// =============================================================================
#include "rdma_sender.h"
// FrameHeader.h 已透過 rdma_sender.h → ../../shared/FrameHeader.h 引入
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <thread>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 4) {
        fprintf(stderr, "用法: %s <server_ip> <port> <num_frames> [width] [height] [delay_ms]\n", argv[0]);
        fprintf(stderr, "  width/height 預設 256x256（小幀驗繞回）；delay_ms 預設 0\n");
        return 1;
    }
    const char*    sip    = argv[1];
    const char*    sport  = argv[2];
    uint32_t       frames = (uint32_t)atoi(argv[3]);
    uint32_t       width  = argc > 4 ? (uint32_t)atoi(argv[4]) : 256;
    uint32_t       height = argc > 5 ? (uint32_t)atoi(argv[5]) : 256;
    int            delay  = argc > 6 ? atoi(argv[6]) : 0;

    printf("[nslot_test] → %s:%s  frames=%u  %ux%u  delay=%dms\n",
           sip, sport, frames, width, height, delay);

    RdmaSender sender;
    size_t max_payload = (size_t)width * height;
    if (!sender.connect(sip, sport, max_payload)) {
        fprintf(stderr, "[nslot_test] RDMA connect 失敗\n");
        return 2;
    }

    // 合成 payload：每幀填 frame_seq % 256 重複（方便肉眼辨識是否串位）
    std::vector<uint8_t> payload(max_payload);

    uint32_t ok = 0, err = 0;
    auto t0 = std::chrono::steady_clock::now();

    for (uint32_t i = 0; i < frames; ++i) {
        // 填不同值讓 CRC 每幀不同，便於驗 slot 沒被覆蓋
        memset(payload.data(), (uint8_t)(i & 0xFF), max_payload);

        sender.send_frame(/*cam_id*/0, /*frame_seq*/(uint64_t)i,
                          /*panel_id_hash*/0xDEADBEEFu,
                          payload.data(), (uint32_t)max_payload,
                          width, height);
        ++ok;

        if (i < 5 || (i + 1) % 20 == 0)
            printf("[nslot_test] sent seq=%-4u slot=%-2u  ok/total=%u/%u\n",
                   i, i % 4, ok, frames);  // 預設 4-slot，印槽位

        if (delay > 0)
            std::this_thread::sleep_for(std::chrono::milliseconds(delay));
    }

    auto t1 = std::chrono::steady_clock::now();
    double elapsed = std::chrono::duration<double>(t1 - t0).count();
    double fps = frames / elapsed;
    double mb  = (double)sender.sent_bytes() / 1024.0 / 1024.0;

    printf("[nslot_test] 完成：ok=%u err=%u  total=%.1fMB  %.1f幀/s  %.1fMB/s\n",
           ok, err, mb, fps, mb / elapsed);

    sender.disconnect();
    return err > 0 ? 3 : 0;
}
