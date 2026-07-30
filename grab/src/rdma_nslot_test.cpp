// =============================================================================
// rdma_nslot_test.cpp — Step 3 N-slot ring buffer 驗證用合成幀送器（不需相機）
// 用法： rdma_nslot_test <server_ip> <port> <num_frames> [width] [height] [delay_ms] [threads]
//   width/height 預設 256×256（小幀快送，驗繞回邏輯）
//   delay_ms     送幀間人工延遲（ms），預設 0；測背壓時 IP 端用 --test-consumer-delay-ms
//   threads      模擬幾台相機（預設 1）。>1 時**完全比照 main.cpp frame_cb 的結構**：
//                N 條 thread 共用一條 QP，以 send_mtx 序列化，**CRC 在鎖外算**。
//                用途：不必等 37 台相機到貨，就能量出「共用單一 QP 的序列化上限」。
//                （單執行緒量不出 send_mtx 的影響 → 量不到 CRC 移出鎖的效益。）
// =============================================================================
#include "rdma_sender.h"
// FrameHeader.h 已透過 rdma_sender.h → ../../shared/FrameHeader.h 引入
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <mutex>
#include <thread>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 4) {
        fprintf(stderr, "用法: %s <server_ip> <port> <num_frames> [width] [height] [delay_ms] [threads]\n", argv[0]);
        fprintf(stderr, "  width/height 預設 256x256（小幀驗繞回）；delay_ms 預設 0；threads 預設 1\n");
        fprintf(stderr, "  threads>1 = 模擬 N 台相機共用單一 QP（比照 main.cpp send_mtx 序列化）\n");
        return 1;
    }
    const char*    sip    = argv[1];
    const char*    sport  = argv[2];
    uint32_t       frames = (uint32_t)atoi(argv[3]);
    uint32_t       width  = argc > 4 ? (uint32_t)atoi(argv[4]) : 256;
    uint32_t       height = argc > 5 ? (uint32_t)atoi(argv[5]) : 256;
    int            delay  = argc > 6 ? atoi(argv[6]) : 0;
    int            nthr   = argc > 7 ? atoi(argv[7]) : 1;
    if (nthr < 1) nthr = 1;

    printf("[nslot_test] → %s:%s  frames=%u  %ux%u  delay=%dms  threads=%d\n",
           sip, sport, frames, width, height, delay, nthr);

    RdmaSender sender;
    size_t max_payload = (size_t)width * height;
    if (!sender.connect(sip, sport, max_payload)) {
        fprintf(stderr, "[nslot_test] RDMA connect 失敗\n");
        return 2;
    }

    std::mutex            send_mtx;   // 比照 main.cpp：N 相機 thread 共用一條 QP → 序列化
    std::atomic<uint64_t> frame_seq{0};
    std::atomic<uint32_t> ok{0};

    auto t0 = std::chrono::steady_clock::now();

    // 測試注入：CFAOI_TEST_CORRUPT_EVERY=N → 每 N 幀故意送錯 CRC，驗收端「丟棄 + 標記不完整」。
    // 只存在於測試送器，生產路徑（cfaoi_grab）沒有這條。
    const char* ce = std::getenv("CFAOI_TEST_CORRUPT_EVERY");
    const uint32_t corrupt_every = ce ? (uint32_t)atoi(ce) : 0;
    if (corrupt_every)
        printf("[nslot_test] ⚠ 測試注入：每 %u 幀故意送錯 CRC\n", corrupt_every);

    // 每條 thread 有自己的 payload 緩衝（比照每台相機有自己的 pylon 緩衝）
    // 每條 thread = 一台相機送一片：slice 0..my_frames-1，totalSlice=my_frames
    auto worker = [&](int tid, uint32_t my_frames) {
        std::vector<uint8_t> payload(max_payload);
        for (uint32_t i = 0; i < my_frames; ++i) {
            memset(payload.data(), (uint8_t)((tid * 37 + i) & 0xFF), max_payload);

            // ⚠️ 與 main.cpp frame_cb 同構：CRC 在 send_mtx **之外**算（各 thread 併發）
            uint32_t crc = RdmaSender::crc_of(payload.data(), (uint32_t)max_payload);
            if (corrupt_every && ((i + 1) % corrupt_every == 0)) crc ^= 0x1u;   // 故意送錯

            uint64_t seq;
            {
                std::lock_guard<std::mutex> lk(send_mtx);
                seq = ++frame_seq;      // 與 main.cpp 同：seq 在鎖內指派，順序 == wire 順序
                sender.send_frame((uint16_t)tid, seq, 0xDEADBEEFu,
                                  payload.data(), (uint32_t)max_payload,
                                  width, height,
                                  (uint16_t)i, (uint16_t)my_frames, crc);
            }
            ++ok;
            if (delay > 0)
                std::this_thread::sleep_for(std::chrono::milliseconds(delay));
        }
    };

    if (nthr == 1) {
        worker(0, frames);
    } else {
        std::vector<std::thread> pool;
        uint32_t per = frames / (uint32_t)nthr, rest = frames % (uint32_t)nthr;
        for (int t = 0; t < nthr; ++t)
            pool.emplace_back(worker, t, per + (t == 0 ? rest : 0));
        for (auto& th : pool) th.join();
    }

    auto t1 = std::chrono::steady_clock::now();
    double elapsed = std::chrono::duration<double>(t1 - t0).count();
    double fps = frames / elapsed;
    double mb  = (double)sender.sent_bytes() / 1024.0 / 1024.0;

    printf("[nslot_test] 完成：ok=%u err=0  total=%.1fMB  %.1f幀/s  %.1fMB/s（threads=%d）\n",
           ok.load(), mb, fps, mb / elapsed, nthr);

    sender.disconnect();
    return 0;
}
