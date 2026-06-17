/**
 * align_verify.cpp — Gap #1 對位 pipeline 精度驗證
 *
 * 用合成影像驗 run_align() 準確度（不需 CUDA / 相機 / 真實面板）：
 *   Stage 1: 已知偏移 → ShiftX/Y 誤差 < 0.5px，angle 誤差 < 0.5°
 *   Stage 3: 無 Mark 影像 → score < threshold → ok=false（ERR 路徑）
 *
 * 編譯：見 CMakeLists.txt 的 align_verify target（只需 OpenCV，不需 CUDA）
 * 執行：./align_verify
 */

#include <opencv2/opencv.hpp>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <algorithm>

#include "align_engine.h"

// ────────────────────────────────────────────────────────────────────────────
// 合成工具
// ────────────────────────────────────────────────────────────────────────────

// 建一張 WxH 的 8-bit 灰階影像：背景 128 + 棋盤 Mark（60×60，10px 格）置中。
static cv::Mat make_search_roi(int W = 500, int H = 500)
{
    cv::Mat img(H, W, CV_8UC1, cv::Scalar(128));
    int mw = 60, mh = 60;
    int mx = W / 2 - mw / 2;   // mark 左上角，中心 = (W/2, H/2)
    int my = H / 2 - mh / 2;
    for (int r = 0; r < mh; r++)
        for (int c = 0; c < mw; c++)
            img.at<uint8_t>(my + r, mx + c) = ((r / 10 + c / 10) % 2 == 0) ? 220 : 40;
    return img;
}

// 截 Mark 為 Golden（60×60，與 make_search_roi 中置中的棋盤一致）。
static cv::Mat extract_golden(const cv::Mat& roi)
{
    int mw = 60, mh = 60;
    int mx = roi.cols / 2 - mw / 2;
    int my = roi.rows / 2 - mh / 2;
    return roi(cv::Rect(mx, my, mw, mh)).clone();
}

// 把影像平移 (dx, dy)（次像素用 warpAffine bilinear）。
static cv::Mat shift_image(const cv::Mat& src, double dx, double dy)
{
    cv::Mat M = (cv::Mat_<double>(2, 3) << 1, 0, dx, 0, 1, dy);
    cv::Mat dst;
    cv::warpAffine(src, dst, M, src.size(), cv::INTER_LINEAR, cv::BORDER_REPLICATE);
    return dst;
}

// 把影像旋轉 angle_deg（繞中心）。
static cv::Mat rotate_image(const cv::Mat& src, double angle_deg)
{
    cv::Point2f center(src.cols / 2.0f, src.rows / 2.0f);
    cv::Mat M = cv::getRotationMatrix2D(center, angle_deg, 1.0);
    cv::Mat dst;
    cv::warpAffine(src, dst, M, src.size(), cv::INTER_LINEAR, cv::BORDER_REPLICATE);
    return dst;
}

// 設定標準 AlignRoiConfig（golden 由外部傳入）。
static AlignRoiConfig make_cfg(const cv::Mat& golden)
{
    AlignRoiConfig cfg;
    cfg.align_enable    = true;
    cfg.golden          = golden.clone();
    cfg.search_width    = 500;
    cfg.search_height   = 500;
    cfg.score_threshold = 0.55f;
    cfg.angle_range_deg = 3.0f;
    cfg.angle_step_deg  = 0.5f;
    return cfg;
}

// ────────────────────────────────────────────────────────────────────────────
// 測試框架
// ────────────────────────────────────────────────────────────────────────────

struct TestResult {
    std::string name;
    bool        pass;
    std::string detail;
};

static std::vector<TestResult> g_results;

static void check(const std::string& name, bool cond, const std::string& detail)
{
    g_results.push_back({name, cond, detail});
    printf("[%s] %s\n  %s\n", cond ? "PASS" : "FAIL", name.c_str(), detail.c_str());
}

// ────────────────────────────────────────────────────────────────────────────
// Stage 1A — 純平移（多組次像素 + 整數）
// ────────────────────────────────────────────────────────────────────────────

static void run_stage1a()
{
    printf("\n=== Stage 1A: 純平移精度 ===\n");

    cv::Mat base = make_search_roi();
    cv::Mat golden = extract_golden(base);
    AlignRoiConfig cfg = make_cfg(golden);

    // 以 (gt_x, gt_y, tolerance_px) 為一組
    struct Case { double gx, gy; std::string label; };
    std::vector<Case> cases = {
        { 7.2, -3.5,  "次像素 (7.2, -3.5)" },
        { 5.0,  3.0,  "整數   (5.0,  3.0)" },
        { 7.3,  2.1,  "次像素 (7.3,  2.1)" },
        {-4.6,  8.9,  "次像素 (-4.6, 8.9)" },
        { 0.0,  0.0,  "零偏移 (0.0,  0.0)" },
    };

    for (auto& c : cases) {
        cv::Mat shifted = shift_image(base, c.gx, c.gy);
        AlignResult r = run_align(shifted, cfg);
        double ex = r.shift_x - c.gx;
        double ey = r.shift_y - c.gy;
        double err = std::sqrt(ex * ex + ey * ey);
        bool pass = r.ok && err < 0.5;
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "gt=(%.1f,%.1f) got=(%.3f,%.3f) err=(%.3f,%.3f) L2=%.3fpx score=%.4f angle=%.2f°",
            c.gx, c.gy, r.shift_x, r.shift_y, ex, ey, err, r.score, r.angle_deg);
        check("Stage1A: " + c.label, pass, buf);
    }
}

// ────────────────────────────────────────────────────────────────────────────
// Stage 1B — 旋轉 + 平移
// ────────────────────────────────────────────────────────────────────────────

static void run_stage1b()
{
    printf("\n=== Stage 1B: 旋轉 + 平移精度 ===\n");

    cv::Mat base = make_search_roi();
    cv::Mat golden = extract_golden(base);
    AlignRoiConfig cfg = make_cfg(golden);

    struct Case { double gx, gy, ga; };
    std::vector<Case> cases = {
        { 3.0, -2.0,  1.5 },
        { 0.0,  0.0,  2.0 },
        {-2.5,  1.8, -1.0 },
    };

    for (auto& c : cases) {
        cv::Mat rot = rotate_image(base, c.ga);
        cv::Mat shifted = shift_image(rot, c.gx, c.gy);
        AlignResult r = run_align(shifted, cfg);
        double ex = r.shift_x - c.gx;
        double ey = r.shift_y - c.gy;
        double ea = r.angle_deg - c.ga;
        double err_px = std::sqrt(ex * ex + ey * ey);
        bool pass = r.ok && err_px < 0.5 && std::abs(ea) < 0.5;
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "gt=(%.1f,%.1f,%.1f°) got=(%.3f,%.3f,%.2f°) err=(%.3f,%.3f) px=%.3f ae=%.3f°",
            c.gx, c.gy, c.ga, r.shift_x, r.shift_y, r.angle_deg, ex, ey, err_px, ea);
        check("Stage1B: rot+shift(" + std::to_string((int)std::round(c.ga)) + "°)", pass, buf);
    }
}

// ────────────────────────────────────────────────────────────────────────────
// Stage 3A — 失敗策略：無 Mark 影像 → ok=false + score 低
// ────────────────────────────────────────────────────────────────────────────

static void run_stage3a()
{
    printf("\n=== Stage 3A: 失敗策略（無 Mark）===\n");

    cv::Mat base = make_search_roi();
    cv::Mat golden = extract_golden(base);
    AlignRoiConfig cfg = make_cfg(golden);
    cfg.score_threshold = 0.55f;

    // Case 1: 純灰底（無 Mark）
    {
        cv::Mat blank(500, 500, CV_8UC1, cv::Scalar(128));
        AlignResult r = run_align(blank, cfg);
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "blank: ok=%d score=%.4f msg='%s' (expect ok=false)",
            (int)r.ok, r.score, r.error_msg.c_str());
        check("Stage3A: 純灰底 → ok=false", !r.ok, buf);
    }

    // Case 2: 完全隨機雜訊（無結構）
    {
        cv::Mat noise(500, 500, CV_8UC1);
        cv::randu(noise, 0, 255);
        AlignResult r = run_align(noise, cfg);
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "noise: ok=%d score=%.4f (expect ok=false)",
            (int)r.ok, r.score);
        check("Stage3A: 隨機雜訊 → ok=false", !r.ok, buf);
    }
}

// ────────────────────────────────────────────────────────────────────────────
// Stage 3B — AlignEnable=false fallback（eff_* 行為驗證，純 struct 邏輯）
// ────────────────────────────────────────────────────────────────────────────

// ZoneConfig 邏輯可直接在這裡 include 驗（不用 GPU pipeline）
#include "config/zone_config_adapter.h"

static void run_stage3b()
{
    printf("\n=== Stage 3B: eff_* fallback 邏輯 ===\n");

    // aligned_* = -1（尚未對位）→ eff_* 應 fallback 到 roi_*
    {
        ZoneConfig z;
        z.roi_start_x = 100; z.roi_start_y = 200;
        z.roi_end_x   = 900; z.roi_end_y   = 800;
        // aligned_* = -1（預設）
        bool pass = z.eff_start_x() == 100 && z.eff_start_y() == 200
                 && z.eff_end_x() == 900   && z.eff_end_y() == 800
                 && !z.is_full_frame();
        check("Stage3B: aligned=-1 → eff_* fallback roi_*", pass,
              "eff_start_x=" + std::to_string(z.eff_start_x()) +
              " eff_start_y=" + std::to_string(z.eff_start_y()) +
              " is_full_frame=" + std::to_string(z.is_full_frame()));
    }

    // SET_ALIGN ShiftX=5 ShiftY=3 → aligned_* 套回
    {
        ZoneConfig z;
        z.roi_start_x = 100; z.roi_start_y = 200;
        z.roi_end_x   = 900; z.roi_end_y   = 800;
        double shift_x = 5.0, shift_y = 3.0;
        z.aligned_start_x = z.roi_start_x + (int)std::round(shift_x);  // 105
        z.aligned_start_y = z.roi_start_y + (int)std::round(shift_y);  // 203
        z.aligned_end_x   = z.roi_end_x   + (int)std::round(shift_x);  // 905
        z.aligned_end_y   = z.roi_end_y   + (int)std::round(shift_y);  // 803
        bool pass = z.eff_start_x() == 105 && z.eff_start_y() == 203
                 && z.eff_end_x()   == 905 && z.eff_end_y()   == 803;
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "ShiftX=5,ShiftY=3: eff_start=(%d,%d) eff_end=(%d,%d) (expect 105,203,905,803)",
            z.eff_start_x(), z.eff_start_y(), z.eff_end_x(), z.eff_end_y());
        check("Stage3B: SET_ALIGN(5,3) → eff_* 套回", pass, buf);
    }

    // LOAD_RECIPE 後 aligned_* 應 reset（預設 -1）
    {
        ZoneConfig z;
        z.roi_start_x = 100; z.roi_start_y = 200;
        z.roi_end_x   = 900; z.roi_end_y   = 800;
        z.aligned_start_x = 105; z.aligned_start_y = 203;
        z.aligned_end_x   = 905; z.aligned_end_y   = 803;
        // 模擬 LOAD_RECIPE 覆蓋 → 新 ZoneConfig 預設 aligned=-1
        ZoneConfig fresh;
        fresh.roi_start_x = 100; fresh.roi_start_y = 200;
        fresh.roi_end_x   = 900; fresh.roi_end_y   = 800;
        bool pass = fresh.aligned_start_x == -1 && fresh.aligned_start_y == -1
                 && fresh.eff_start_x() == 100;
        check("Stage3B: LOAD_RECIPE 後 aligned=-1（eff_* fallback）", pass,
              "aligned_start_x=" + std::to_string(fresh.aligned_start_x) +
              " eff_start_x=" + std::to_string(fresh.eff_start_x()));
    }

    // AlignEnable=false（roi_start_x=-1）→ is_full_frame=true，eff_* 回 roi_*（全幅）
    {
        ZoneConfig z;
        // 全幅預設（roi_start_x=-1）
        bool pass = z.is_full_frame() && z.eff_start_x() == -1;
        check("Stage3B: AlignEnable=false 全幅 → is_full_frame=true", pass,
              "is_full_frame=" + std::to_string(z.is_full_frame()) +
              " eff_start_x=" + std::to_string(z.eff_start_x()));
    }
}

// ────────────────────────────────────────────────────────────────────────────
// main
// ────────────────────────────────────────────────────────────────────────────

int main()
{
    printf("============================================================\n");
    printf("   Gap #1 對位 Pipeline — align_verify\n");
    printf("============================================================\n");

    run_stage1a();
    run_stage1b();
    run_stage3a();
    run_stage3b();

    // 統計
    int pass_cnt = 0, fail_cnt = 0;
    printf("\n============================================================\n");
    for (auto& r : g_results) {
        if (r.pass) pass_cnt++; else fail_cnt++;
    }
    printf("結果: PASS %d / FAIL %d (共 %d)\n",
           pass_cnt, fail_cnt, (int)g_results.size());
    if (fail_cnt > 0) {
        printf("\nFAIL 清單：\n");
        for (auto& r : g_results)
            if (!r.pass) printf("  ✗ %s\n    %s\n", r.name.c_str(), r.detail.c_str());
    }
    printf("============================================================\n");
    return fail_cnt > 0 ? 1 : 0;
}
