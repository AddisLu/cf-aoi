// =============================================================================
// cfaoi_grab — Grab 主程式（截取中心 damac，x86 Linux）
// -----------------------------------------------------------------------------
// 狀態機：
//   IDLE → (LOAD_RECIPE) → IDLE（更新 panel_id）
//        → (GRAB_START)  → GRABBING（開相機 + 連 RDMA + 送幀）
//        → (GRAB_STOP)   → IDLE（停相機 + 斷 RDMA）
//
// 用法：
//   cfaoi_grab --rdma-dest 192.168.3.1:18515 [options]
//
// 選項：
//   --rdma-dest  IP:PORT     Spark IP 端的 RDMA server（必填）
//   --cam-id     N           相機 cam_id（預設 0）
//   --serial     STRING      pylon 序號；auto = 第一台（預設 auto）
//   --pkt-size   N           GevSCPSPacketSize（預設 8192）
//   --ctrl-port  N           等 Control 連入的 port（預設 8100）
// =============================================================================

#include "cam_pylon.h"
#include "control_server.h"
#include "rdma_sender.h"
#include "../../shared/FrameHeader.h"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>

// ---- 全域停止旗標（SIGINT/SIGTERM 用）----
static std::atomic<bool> g_shutdown{false};
static void sig_handler(int) { g_shutdown = true; }

// ---- 從 "IP:PORT" 字串切開 ----
static bool parse_host_port(const std::string& s, std::string& host, std::string& port) {
    auto colon = s.rfind(':');
    if (colon == std::string::npos || colon == 0 || colon + 1 >= s.size()) return false;
    host = s.substr(0, colon);
    port = s.substr(colon + 1);
    return true;
}

int main(int argc, char** argv) {
    // ---- 解析 CLI ----
    std::string rdma_dest;
    uint16_t    cam_id    = 0;
    std::string serial    = "auto";
    int64_t     pkt_size  = 8192;
    int         ctrl_port = 8100;

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&]() -> const char* {
            return (i + 1 < argc) ? argv[++i] : "";
        };
        if      (a == "--rdma-dest")  rdma_dest = next();
        else if (a == "--cam-id")     cam_id    = (uint16_t)atoi(next());
        else if (a == "--serial")     serial    = next();
        else if (a == "--pkt-size")   pkt_size  = atoll(next());
        else if (a == "--ctrl-port")  ctrl_port = atoi(next());
        else { fprintf(stderr, "未知參數：%s\n", a.c_str()); return 1; }
    }

    if (rdma_dest.empty()) {
        fprintf(stderr, "必填：--rdma-dest IP:PORT\n"); return 1;
    }
    std::string rdma_host, rdma_port;
    if (!parse_host_port(rdma_dest, rdma_host, rdma_port)) {
        fprintf(stderr, "--rdma-dest 格式錯誤，應為 IP:PORT\n"); return 1;
    }

    ::signal(SIGINT,  sig_handler);
    ::signal(SIGTERM, sig_handler);

    // ---- 元件 ----
    CamPylon      cam;
    RdmaSender    sender;
    ControlServer ctrl(ctrl_port);

    // ---- 狀態（mutex 保護，跨 ControlServer thread 與 cam grab thread）----
    std::mutex          state_mtx;
    bool                grabbing    = false;
    std::string         panel_id    = "PANEL";
    uint32_t            panel_hash  = frame_panel_hash(panel_id);
    std::atomic<uint64_t> frame_seq{0};

    // ---- 每幀回呼（跑在 CamPylon 的 grab thread 裡）----
    cam.set_frame_callback([&](uint16_t cid, const uint8_t* data,
                                uint32_t bytes, uint32_t w, uint32_t h) {
        uint64_t seq = ++frame_seq;  // atomic fetch_add
        uint32_t phash;
        { std::lock_guard<std::mutex> lk(state_mtx); phash = panel_hash; }
        sender.send_frame(cid, seq, phash, data, bytes, w, h);
    });

    // ---- Control 回呼 ----
    ctrl.set_load_recipe([&](const std::string& recipe, const std::string& pid) {
        std::lock_guard<std::mutex> lk(state_mtx);
        panel_id   = pid.empty() ? recipe : pid;
        panel_hash = frame_panel_hash(panel_id);
        printf("[main] LOAD_RECIPE recipe=%s panel_id=%s hash=0x%08x\n",
               recipe.c_str(), panel_id.c_str(), panel_hash);
    });

    ctrl.set_grab_start([&](int /*timeout_ms*/, std::string& err) -> bool {
        std::lock_guard<std::mutex> lk(state_mtx);
        if (grabbing) { err = "already grabbing"; return false; }

        // 開相機（若尚未開）
        if (!cam.open(serial, pkt_size)) {
            err = "pylon open failed";
            return false;
        }
        // 連 RDMA（用 pylon PayloadSize 決定緩衝大小）
        if (!sender.connect(rdma_host.c_str(), rdma_port.c_str(),
                            (size_t)cam.payload_size())) {
            err = "RDMA connect failed";
            return false;
        }
        frame_seq.store(0);
        cam.start(cam_id);
        grabbing = true;
        printf("[main] GRAB_START  cam%u  rdma→%s:%s  panel=%s\n",
               cam_id, rdma_host.c_str(), rdma_port.c_str(), panel_id.c_str());
        return true;
    });

    ctrl.set_grab_stop([&]() {
        std::lock_guard<std::mutex> lk(state_mtx);
        if (!grabbing) return;
        cam.stop();
        sender.disconnect();
        grabbing = false;
        printf("[main] GRAB_STOP  已送 %llu 幀（%llu bytes）\n",
               (unsigned long long)sender.sent_frames(),
               (unsigned long long)sender.sent_bytes());
    });

    ctrl.set_status_provider([&]() -> std::string {
        bool g;
        uint64_t grabbed, dropped, sent_f, sent_b;
        {
            std::lock_guard<std::mutex> lk(state_mtx);
            g       = grabbing;
            grabbed = cam.grabbed();
            dropped = cam.dropped();
            sent_f  = sender.sent_frames();
            sent_b  = sender.sent_bytes();
        }
        char buf[256];
        snprintf(buf, sizeof(buf),
                 "{\"grabbing\":%s,\"grabbed\":%llu,\"dropped\":%llu,"
                 "\"sent_frames\":%llu,\"sent_bytes\":%llu}",
                 g ? "true" : "false",
                 (unsigned long long)grabbed,
                 (unsigned long long)dropped,
                 (unsigned long long)sent_f,
                 (unsigned long long)sent_b);
        return buf;
    });

    // ---- 啟動 ----
    if (!ctrl.start()) {
        fprintf(stderr, "[main] ControlServer 啟動失敗\n");
        return 1;
    }
    printf("[main] cfaoi_grab 就緒  ctrl_port=%d  rdma→%s:%s  cam_id=%u\n",
           ctrl_port, rdma_host.c_str(), rdma_port.c_str(), cam_id);

    // ---- 主迴圈：等信號 ----
    while (!g_shutdown) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    printf("[main] 收到停止信號，清理中...\n");
    {
        std::lock_guard<std::mutex> lk(state_mtx);
        if (grabbing) {
            cam.stop();
            sender.disconnect();
            grabbing = false;
        }
    }
    ctrl.stop();
    printf("[main] 結束\n");
    return 0;
}
