// ─────────────────────────────────────────────────────────────────────────────
// 拼接測試 — CamPylon 把 k 張相機幀拼成一張送出（GigE 單幀行數受機上緩衝限制）。
//
// 借用 b1 的 pylon stub：以腳本逐張交出幀（BlockID / skipped / 填值 / 大小），
// 並以 int_max 模擬相機 Height 上限（raL8192@8192 寬 = 3573）。
// 涵蓋：拼接張數推導、open 失敗路徑、拼接順序、掉幀/失敗幀作廢重對齊、
//       frames_per_panel 以送出幀計、幀大小不符 = 故障、不拼接時行為不變。
// 不涵蓋：真相機 BlockID 行為 → 需 damac 實機取像補驗。
// ─────────────────────────────────────────────────────────────────────────────
#include "cam_pylon.h"
#include <pylon/PylonIncludes.h>
#include <pylon/ParameterIncludes.h>

#include <chrono>
#include <cstdio>
#include <string>
#include <thread>
#include <vector>

static int g_fail = 0;

static void check(bool ok, const std::string& name, const std::string& detail = "") {
    printf("  %s  %s%s\n", ok ? "PASS" : "FAIL", name.c_str(),
           detail.empty() ? "" : ("  [" + detail + "]").c_str());
    if (!ok) ++g_fail;
}

struct Out {
    uint32_t bytes, w, h;
    std::vector<uint8_t> first_byte_of_part;   // 每段（相機幀）第一個 byte，驗拼接順序
};

// 小尺寸相機：寬 4、Height 上限 3 → 送出 6 行 = 2 張 × 3 行，每張 12 bytes
constexpr int64_t W = 4, HMAX = 3, OUT_H = 6, CHUNK = W * (OUT_H / 2);

static void setup_camera() {
    PylonStub::reset();
    PylonStub::int_max["Height"] = HMAX;
}

static PylonStub::ScriptFrame frame(int64_t bid, uint8_t fill, uint64_t skipped = 0) {
    PylonStub::ScriptFrame f;
    f.block_id = bid;
    f.fill = fill;
    f.skipped = skipped;
    f.size = (size_t)CHUNK;
    f.width = (uint32_t)W;
    f.height = (uint32_t)(OUT_H / 2);
    return f;
}

static std::vector<Out> run(CamPylon& cam, uint32_t parts_per_out, uint64_t max_frames = 0) {
    std::vector<Out> outs;
    cam.set_frame_callback([&](uint16_t, const uint8_t* d, uint32_t n, uint32_t w, uint32_t h) {
        Out o{n, w, h, {}};
        const uint32_t part = parts_per_out ? n / parts_per_out : n;
        for (uint32_t p = 0; part && p < parts_per_out; ++p) o.first_byte_of_part.push_back(d[p * part]);
        outs.push_back(o);
    });
    cam.set_max_frames(max_frames);
    PylonStub::grabbing_ticks = (int)PylonStub::script.size() + 5;
    cam.start(0);
    for (int i = 0; i < 500 && cam.is_running(); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    return outs;
}

int main() {
    printf("== open：拼接張數推導\n");
    {
        setup_camera();
        CamPylon cam;
        check(cam.open("auto", 8192, Roi{W, OUT_H}), "open 成功");
        check(cam.stitch_count() == 2, "6 行 / 上限 3 → 拼 2 張", std::to_string(cam.stitch_count()));
        check(PylonStub::ints["Height"] == 3, "相機 Height 設為 3");
        check(cam.payload_size() == W * OUT_H, "PayloadSize = 拼接後大小");
        cam.stop();
    }
    {
        setup_camera();
        PylonStub::int_max["Height"] = 4;   // 10 行：ceil(10/4)=3 不整除 → 5 張 × 2 行
        CamPylon cam;
        cam.open("auto", 8192, Roi{W, 10});
        check(cam.stitch_count() == 5 && PylonStub::ints["Height"] == 2,
              "10 行 / 上限 4 → 取整除的 5 張 × 2 行", std::to_string(cam.stitch_count()));
        cam.stop();
    }
    {
        setup_camera();
        CamPylon cam;
        cam.open("auto", 8192, Roi{W, 3});
        check(cam.stitch_count() == 1, "未超過上限 → 不拼接");
        cam.stop();
    }
    {
        setup_camera();
        PylonStub::int_max["Width"] = 2;
        CamPylon cam;
        check(!cam.open("auto", 8192, Roi{W, OUT_H}), "Width 超出上限 → open 失敗");
    }

    printf("== 取像：正常拼接\n");
    {
        setup_camera();
        CamPylon cam;
        cam.open("auto", 8192, Roi{W, OUT_H});
        PylonStub::script = {frame(1, 0xA1), frame(2, 0xA2), frame(3, 0xB1), frame(4, 0xB2)};
        auto outs = run(cam, 2);
        check(outs.size() == 2, "4 張相機幀 → 送出 2 張", std::to_string(outs.size()));
        if (outs.size() == 2) {
            check(outs[0].bytes == W * OUT_H && outs[0].w == W && outs[0].h == OUT_H, "送出尺寸 4×6");
            check(outs[0].first_byte_of_part == std::vector<uint8_t>{0xA1, 0xA2} &&
                  outs[1].first_byte_of_part == std::vector<uint8_t>{0xB1, 0xB2}, "拼接順序正確");
        }
        check(cam.grabbed() == 2 && cam.dropped() == 0, "grabbed=2 dropped=0");
        check(!cam.is_faulted(), "未標故障");
        cam.stop();
    }

    printf("== 取像：拼到一半掉幀（BlockID 缺口）→ 半張作廢、重新對齊\n");
    {
        setup_camera();
        CamPylon cam;
        cam.open("auto", 8192, Roi{W, OUT_H});
        // 1 收進第一段；3 前少了 2 → 作廢 1，3 當新開頭；4 補齊
        PylonStub::script = {frame(1, 0x11), frame(3, 0x33), frame(4, 0x44)};
        auto outs = run(cam, 2);
        check(outs.size() == 1 && outs[0].first_byte_of_part == std::vector<uint8_t>{0x33, 0x44},
              "只送出 [3,4]，不送出有斷層的 [1,3]");
        check(cam.dropped() == 2, "dropped = 遺失 1 + 作廢 1", std::to_string(cam.dropped()));
        cam.stop();
    }

    printf("== 取像：拼到一半 pylon 回報 skipped → 作廢\n");
    {
        setup_camera();
        CamPylon cam;
        cam.open("auto", 8192, Roi{W, OUT_H});
        PylonStub::script = {frame(1, 0x11), frame(2, 0x22, /*skipped*/ 1), frame(3, 0x33)};
        auto outs = run(cam, 2);
        check(outs.size() == 1 && outs[0].first_byte_of_part == std::vector<uint8_t>{0x22, 0x33},
              "送出 [2,3]");
        cam.stop();
    }

    printf("== 取像：拼到一半 GrabFailed → 作廢\n");
    {
        setup_camera();
        CamPylon cam;
        cam.open("auto", 8192, Roi{W, OUT_H});
        auto bad = frame(2, 0x22);
        bad.ok = false;
        PylonStub::script = {frame(1, 0x11), bad, frame(3, 0x33), frame(4, 0x44)};
        auto outs = run(cam, 2);
        check(outs.size() == 1 && outs[0].first_byte_of_part == std::vector<uint8_t>{0x33, 0x44},
              "失敗幀前的半張不送出，送出 [3,4]");
        check(cam.dropped() == 2, "dropped = 作廢 1 + 失敗幀的 BlockID 缺口 1", std::to_string(cam.dropped()));
        cam.stop();
    }

    printf("== 取像：frames_per_panel 以送出幀計\n");
    {
        setup_camera();
        CamPylon cam;
        cam.open("auto", 8192, Roi{W, OUT_H});
        for (int i = 1; i <= 8; ++i) PylonStub::script.push_back(frame(i, (uint8_t)i));
        auto outs = run(cam, 2, /*max_frames*/ 3);
        check(outs.size() == 3 && cam.grabbed() == 3, "max_frames=3 → 送出 3 張後停", std::to_string(outs.size()));
        check(PylonStub::script_pos == 6, "只消耗 6 張相機幀", std::to_string(PylonStub::script_pos));
        cam.stop();
    }

    printf("== 取像：相機幀大小不符 → 故障（不送出錯位影像）\n");
    {
        setup_camera();
        CamPylon cam;
        cam.open("auto", 8192, Roi{W, OUT_H});
        auto odd = frame(1, 0x11);
        odd.size = (size_t)CHUNK - 1;
        PylonStub::script = {odd};
        auto outs = run(cam, 2);
        check(outs.empty(), "未送出");
        check(cam.is_faulted() && cam.fault_message().find("拼接") != std::string::npos,
              "標記故障且訊息說明原因", cam.fault_message());
        cam.stop();
    }

    printf("== 取像：不拼接 → 每張相機幀直接送出（與舊行為相同）\n");
    {
        setup_camera();
        CamPylon cam;
        cam.open("auto", 8192, Roi{W, 3});
        PylonStub::script = {frame(1, 0x11), frame(3, 0x33)};
        auto outs = run(cam, 1);
        check(outs.size() == 2 && cam.grabbed() == 2 && cam.dropped() == 1,
              "2 張都送出，缺口照計 dropped=1");
        cam.stop();
    }

    printf(g_fail ? "\n%d 項失敗\n" : "\n全數通過\n", g_fail);
    return g_fail ? 1 : 0;
}
