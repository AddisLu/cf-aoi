// ─────────────────────────────────────────────────────────────────────────────
// B1 行為測試 — 用 pylon stub 注入例外，證明 grab thread 的例外真的被關住。
//
// 這支測試最重要的性質：**如果 B1 修法失效，測試不是 FAIL 而是整支程式被
// std::terminate 殺掉**（正是線上會發生的事）。所以「跑到最後印出結果」本身
// 就是主張成立的證據。
//
// 涵蓋：拔線在 ARM 當下 / 拔線在取像途中 / frame_cb 自己擲 / 收尾 StopGrabbing 也擲 /
//       正常路徑不誤標故障 / 重新 start 會清掉上次的故障標記。
// 不涵蓋：真 pylon API 契約、真相機行為 → 仍需 damac 補驗。
// ─────────────────────────────────────────────────────────────────────────────
#include "cam_pylon.h"
#include <pylon/PylonIncludes.h>

#include <chrono>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <thread>

static int g_fail = 0;

static void check(bool ok, const std::string& name, const std::string& detail = "") {
    printf("  %s  %s%s\n", ok ? "PASS" : "FAIL", name.c_str(),
           detail.empty() ? "" : ("  [" + detail + "]").c_str());
    if (!ok) ++g_fail;
}

// 跑一輪取像並等 grab thread **自行**結束（故障退出或收滿退出）。
// ⚠️ 不可 start() 後立刻 stop()：stop() 會先豎 stop_flag_，迴圈條件 !stop_flag_ 直接為假
//    → 迴圈根本沒進去，注入的例外永遠不會被觸發（此測試第一版就是這樣假通過的）。
static bool run_and_settle(CamPylon& c, const FrameCb& cb) {
    c.set_frame_callback(cb);
    c.start(0);
    for (int i = 0; i < 500 && c.is_running(); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    return !c.is_running();     // false = thread 卡住 5 秒沒退出，本身也是 bug
}

int main() {
    auto noop_cb = [](uint16_t, const uint8_t*, uint32_t, uint32_t, uint32_t) {};

    // ── 1) 拔線發生在 ARM 當下：StartGrabbing 擲例外 ──────────────────────
    {
        PylonStub::reset();
        PylonStub::throw_on_start = 1;
        CamPylon cam;
        check(cam.open("auto", 8192), "1a. stub 相機 open 成功");
        check(run_and_settle(cam, noop_cb), "1a2. grab thread 自行退出（未卡住）");
        check(cam.is_faulted(), "1b. StartGrabbing 擲例外 → 標記 faulted");
        check(cam.fault_message().rfind("pylon: ", 0) == 0,
              "1c. 故障訊息歸類為 pylon 層", cam.fault_message());
        check(!cam.is_running(), "1d. thread 已退出（running=false）");
    }

    // ── 2) 拔線發生在取像途中：RetrieveResult 擲例外 ──────────────────────
    {
        PylonStub::reset();
        PylonStub::grabbing_ticks    = 100;   // 迴圈跑得起來
        PylonStub::throw_on_retrieve = 1;
        CamPylon cam;
        cam.open("auto", 8192);
        check(run_and_settle(cam, noop_cb), "2a0. thread 自行退出");
        check(cam.is_faulted(), "2a. RetrieveResult 擲例外 → 標記 faulted");
        check(!cam.is_running(), "2b. thread 已退出");
    }

    // ── 3) frame_cb 自己擲（模擬 RDMA 送幀失敗）→ 非 pylon 例外分支 ───────
    {
        PylonStub::reset();
        PylonStub::grabbing_ticks = 100;
        PylonStub::deliver_frames = 5;
        CamPylon cam;
        cam.open("auto", 8192);
        check(run_and_settle(cam, [](uint16_t, const uint8_t*, uint32_t, uint32_t, uint32_t) {
            throw std::runtime_error("RDMA send failed");
        }), "3a0. thread 自行退出");
        check(cam.is_faulted(), "3a. frame_cb 擲例外 → 標記 faulted");
        check(cam.fault_message().rfind("exception: ", 0) == 0,
              "3b. 歸類為非 pylon 例外", cam.fault_message());
    }

    // ── 4) 最陰的一條：收尾的 StopGrabbing 自己也擲 ───────────────────────
    //     斷線後 StopGrabbing 會擲例外。若沒單獨包 try，catch 完會在收尾二次逸出
    //     → 仍然 std::terminate。這條若失效，整支測試會直接被殺掉。
    {
        PylonStub::reset();
        PylonStub::grabbing_ticks    = 100;
        PylonStub::throw_on_retrieve = 1;
        PylonStub::throw_on_stop     = 1;
        CamPylon cam;
        cam.open("auto", 8192);
        check(run_and_settle(cam, noop_cb), "4a0. thread 自行退出");
        check(cam.is_faulted(), "4a. 取像例外 + 收尾例外 → 行程仍存活且標記 faulted");
    }

    // ── 5) 正常路徑：不可誤標故障 ────────────────────────────────────────
    {
        PylonStub::reset();
        PylonStub::grabbing_ticks = 3;
        PylonStub::deliver_frames = 3;
        CamPylon cam;
        cam.open("auto", 8192);
        check(run_and_settle(cam, noop_cb), "5a0. thread 自行退出");
        check(!cam.is_faulted(), "5a. 正常收完不標故障");
        check(cam.fault_message().empty(), "5b. 正常路徑無故障訊息");
    }

    // ── 6) 重新 start 要清掉上次的故障標記（否則排除線路後永遠是紅的）─────
    {
        PylonStub::reset();
        PylonStub::throw_on_start = 1;
        CamPylon cam;
        cam.open("auto", 8192);
        run_and_settle(cam, noop_cb);
        check(cam.is_faulted(), "6a. 第一輪故障");

        // 不 stop()：stop() 會 Close 相機，第二次 start() 因 !opened_ 直接早退 =
        // 測不到清旗標。這裡走的正是「收滿自動停後再 start」的真實路徑（start 內會 join 舊 thread）。
        PylonStub::reset();
        PylonStub::grabbing_ticks = 2;
        check(run_and_settle(cam, noop_cb), "6a2. 第二輪 thread 自行退出");
        check(!cam.is_faulted(), "6b. 重新 start 後故障標記已清");
        check(cam.fault_message().empty(), "6c. 故障訊息也清了");
    }

    printf("\n%s  （能印到這行 = 例外從未逸出 thread 進入點）\n",
           g_fail == 0 ? "全數通過" : "有失敗項");
    return g_fail == 0 ? 0 : 1;
}
