// =============================================================================
// image_replay_sender.cpp — 檔案回放 RDMA 送器（Gap #27）：舊圖 → RDMA → Spark IP 運算
// -----------------------------------------------------------------------------
// 相機/Switch 未到貨前，用舊機台存圖驗證「RDMA 傳輸 + Spark AOI」整條流程（是否塞車）。
//
// 設計：dependency-free（不連 OpenCV；damac 無 libopencv-dev）。
//   影像解碼交給 Python(PIL) 端，本程式只從 stdin 讀「已解碼的 Mono8 raw bytes」並經 RDMA 送出。
//   stdin 協議（重複）：一行文字 "cam_id seq\n"  接著  width*height bytes 的 Mono8 raw。
//   （cam_id = CCD 編號，IP 端據此命名 CCD{cam}_{seq}；seq = 全域遞增，slot=seq%n_slots。）
//
// 用法：
//   image_replay_sender <spark_ip> <port> <width> <height>
//   （Python 驅動：PIL 解 TIF → tobytes() → 寫 header+raw 到本程式 stdin；見 scripts/verify_rdma_replay.py）
//
// 背壓：RDMA credit 耗盡 → send_frame 內 poll_one 阻塞 → 不再讀 stdin → Python write 阻塞（pipe 滿）→ 自然限速。
// =============================================================================
#include "rdma_sender.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cinttypes>
#include <chrono>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 5) {
        fprintf(stderr, "用法: %s <spark_ip> <port> <width> <height>\n"
                        "  stdin 重複: \"cam_id seq\\n\" + width*height Mono8 raw bytes\n", argv[0]);
        return 1;
    }
    const char*  sip   = argv[1];
    const char*  sport = argv[2];
    uint32_t     W     = (uint32_t)atoi(argv[3]);
    uint32_t     H     = (uint32_t)atoi(argv[4]);
    const size_t fb    = (size_t)W * H;             // 每幀 payload bytes（Mono8）

    fprintf(stderr, "[replay] → %s:%s  frame=%ux%u (%zuMB)\n", sip, sport, W, H, fb >> 20);

    RdmaSender sender;
    if (!sender.connect(sip, sport, fb)) {
        fprintf(stderr, "[replay] RDMA connect 失敗\n");
        return 2;
    }

    std::vector<uint8_t> buf(fb);
    uint32_t sent = 0;
    char line[128];
    auto t0 = std::chrono::steady_clock::now();

    // 逐幀：讀 header 行 → 讀 fb bytes → send_frame
    while (fgets(line, sizeof(line), stdin)) {
        if (line[0] == '\n' || line[0] == '\0') continue;
        unsigned cam = 0;
        unsigned long long seq = 0;
        if (sscanf(line, "%u %llu", &cam, &seq) != 2) {
            fprintf(stderr, "[replay] 壞 header 行: %s", line);
            break;
        }
        size_t got = fread(buf.data(), 1, fb, stdin);
        if (got != fb) {
            fprintf(stderr, "[replay] payload 短讀 %zu/%zu（EOF？）\n", got, fb);
            break;
        }
        // panel_id_hash 不重要（IP rdma-process 以 camId+seq 命名）；填固定值
        sender.send_frame((uint16_t)cam, (uint64_t)seq, 0xCFA01CF0u,
                          buf.data(), (uint32_t)fb, W, H);
        ++sent;
        if (sent <= 5 || sent % 20 == 0)
            fprintf(stderr, "[replay] sent cam=%u seq=%llu (#%u)\n", cam, seq, sent);
    }

    sender.disconnect();
    double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    double mb = (double)sender.sent_bytes() / 1024.0 / 1024.0;
    fprintf(stderr, "[replay] 完成 sent=%u  %.1fMB  %.1fs  %.2f幀/s  %.1fMB/s\n",
            sent, mb, el, el > 0 ? sent / el : 0.0, el > 0 ? mb / el : 0.0);
    return 0;
}
