// ebus_frame_check — 經 CamManager（--camera ebus 同一條路徑）收 ebus_sim 的影像，逐張驗內容（2026-10-05）。
//   CFAOI_EBUS_NO_SERIAL=1 ebus_frame_check <裝置 IP/序號> [張數=20]
// ebus_sim 每張：第 y 行全為 (y + 幀序號) & 0xFF，幀序號（uint64）寫在第 0 行前 8 bytes。
// 驗：每行內容正確（沒錯位/混幀）、序號遞增（跳號 = 掉幀，需與 dropped 對得上；倒退/重複 = 錯）。
#include "../../src/cam_manager.h"
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>

int main(int argc, char** argv) {
    if (argc < 2) { std::fprintf(stderr, "用法：%s <裝置 IP/序號> [張數]\n", argv[0]); return 2; }
    const uint64_t want = argc > 2 ? std::strtoull(argv[2], nullptr, 10) : 20;
    CamManager mgr;
    mgr.set_backend(CamBackend::Ebus);
    mgr.set_roi(Roi{8160, 5000});
    mgr.set_line_rate(0);
    std::string err;
    if (!mgr.open_all(1, argv[1], 9000, err)) { std::fprintf(stderr, "open 失敗：%s\n", err.c_str()); return 2; }
    std::mutex mu;
    uint64_t frames = 0, bad_rows = 0, bad_frames = 0, gaps = 0, backwards = 0;
    int64_t last = -1;
    std::atomic<bool> done{false};
    mgr.start_all(want, [&](uint16_t, const uint8_t* d, uint32_t bytes, uint32_t w, uint32_t h) {
        uint64_t seq = 0;
        std::memcpy(&seq, d, 8);
        uint64_t br = 0;
        for (uint32_t y = 0; y < h; ++y) {
            const uint8_t v = (uint8_t)(y + seq);
            const uint8_t* row = d + (size_t)y * w;
            for (uint32_t x = (y == 0 ? 8 : 0); x < w; ++x)
                if (row[x] != v) { ++br; break; }
        }
        std::lock_guard<std::mutex> lk(mu);
        ++frames;
        if (bytes != w * h || br) { ++bad_frames; bad_rows += br; }
        if (last >= 0 && (int64_t)seq <= last) ++backwards;
        else if (last >= 0 && (int64_t)seq > last + 1) gaps += (uint64_t)((int64_t)seq - last - 1);
        last = (int64_t)seq;
        if (frames >= want) done = true;
    });
    const auto t0 = std::chrono::steady_clock::now();
    while (!done && std::chrono::steady_clock::now() - t0 < std::chrono::seconds(60))
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    const uint64_t dropped = mgr.total_dropped();
    mgr.stop_all();
    std::printf("收 %llu 張；內容錯誤 %llu 張（%llu 行）；序號跳號 %llu、倒退/重複 %llu；grab 記 dropped=%llu\n",
                (unsigned long long)frames, (unsigned long long)bad_frames, (unsigned long long)bad_rows,
                (unsigned long long)gaps, (unsigned long long)backwards, (unsigned long long)dropped);
    const bool ok = frames >= want && bad_frames == 0 && backwards == 0 && gaps <= dropped;
    std::printf(ok ? "✓ PASS：每張內容正確、無錯位/混幀/重複；跳號皆已計入 dropped\n" : "✗ FAIL\n");
    return ok ? 0 : 1;
}
