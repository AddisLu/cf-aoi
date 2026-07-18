/**
 * edge_verify.cpp — 玻璃前緣/尾緣健檢（EdgeCheck）Stage 1 合成驗證（純 OpenCV，不需 CUDA）
 *
 * 驗證項（docs plan「37 CCD 觸發設計」）：
 *   1. 正常片：前/尾緣行號誤差 ≤3 行、實測行數正確、drift≈0
 *   2. Align Fail：前緣不在第一張（sensor→取像延遲超窗）→ leading_found=false
 *   3. 傳送異常：尾緣不存在（片未走完/張數不足）→ transport_ok=false
 *   4. 速度漂移：壓縮片長 → drift_pct 數值正確、超過閾值 → transport_ok=false
 *   5. 前緣範圍檢查：expected ± tolerance 內/外
 *   6. disabled → checked=false 全 OK（行為不變）
 *   7. 短影像（前/尾窗重疊）仍能區分前後緣
 *   8. 對比不足（< min_contrast）→ 不誤報；亮暗方向反轉仍可偵測
 *   9. INI [EdgeCheck] 解析
 * 編譯：CMakeLists.txt 的 edge_verify target；執行：./edge_verify
 */

#include <opencv2/opencv.hpp>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>

#include "edge_check.h"

static int g_pass = 0, g_fail = 0;
static void check(const std::string& name, bool cond, const std::string& detail) {
    printf("[%s] %s\n  %s\n", cond ? "PASS" : "FAIL", name.c_str(), detail.c_str());
    if (cond) g_pass++; else g_fail++;
}

// 造合成片：背景 bg 灰階，玻璃區 [glass_y0, glass_y1) 為 glass 灰階，加 ±3 固定種子雜訊。
// glass_y0 < 0 → 無前緣（影像頂就是玻璃內）；glass_y1 > H → 無尾緣（玻璃延伸出影像底）。
static cv::Mat make_panel(int W, int H, int glass_y0, int glass_y1, int bg, int glass) {
    cv::Mat img(H, W, CV_8UC1, cv::Scalar(bg));
    const int y0 = std::max(0, glass_y0), y1 = std::min(H, glass_y1);
    if (y1 > y0) img(cv::Rect(0, y0, W, y1 - y0)).setTo(cv::Scalar(glass));
    cv::Mat noise(H, W, CV_8SC1);
    cv::RNG rng(42);  // 固定種子 → 決定性
    rng.fill(noise, cv::RNG::UNIFORM, -3, 4);
    cv::Mat tmp; img.convertTo(tmp, CV_16SC1);
    cv::Mat n16; noise.convertTo(n16, CV_16SC1);
    tmp += n16;
    tmp.convertTo(img, CV_8UC1);
    return img;
}

int main() {
    printf("============================================================\n");
    printf("   玻璃前緣/尾緣健檢（Align Fail + 傳送片檢查）— edge_verify\n");
    printf("============================================================\n");

    EdgeCheckConfig cfg;
    cfg.enabled = true;
    cfg.frame_lines = 5000;
    cfg.min_contrast = 20;
    cfg.tail_search_frames = 2;

    // 1) 正常片：8160×15000（3 張），玻璃 [1200, 13800)
    {
        cv::Mat img = make_panel(8160, 15000, 1200, 13800, 30, 150);
        auto r = EdgeCheck::run(img.data, img.cols, img.rows, cfg);
        check("1a 前緣行號（期望 1200±3）",
              r.leading_found && std::abs(r.leading_line - 1200) <= 3,
              "leading_line=" + std::to_string(r.leading_line));
        check("1b 尾緣行號（期望 13800±3）",
              r.tail_found && std::abs(r.tail_line - 13800) <= 3,
              "tail_line=" + std::to_string(r.tail_line));
        check("1c 實測行數（期望 12600±6）",
              std::labs(r.measured_lines - 12600) <= 6,
              "measured_lines=" + std::to_string(r.measured_lines));
        EdgeCheckConfig c2 = cfg; c2.expected_panel_lines = 12600;
        auto r2 = EdgeCheck::run(img.data, img.cols, img.rows, c2);
        check("1d drift≈0 且 transport_ok（|drift|<=0.1%）",
              std::fabs(r2.drift_pct) <= 0.1 && r2.transport_ok(c2.drift_warn_pct),
              EdgeCheck::summary(r2, c2));
    }

    // 2) Align Fail：玻璃 6000 才進來（超出第一張 5000 窗口）
    {
        cv::Mat img = make_panel(2048, 15000, 6000, 13800, 30, 150);
        auto r = EdgeCheck::run(img.data, img.cols, img.rows, cfg);
        check("2 前緣不在第一張 → Align Fail",
              !r.leading_found && !r.align_ok(),
              "leading_found=" + std::to_string(r.leading_found) +
              " align_ok=" + std::to_string(r.align_ok()));
    }

    // 3) 傳送異常：玻璃延伸出影像底（尾緣不存在）
    {
        cv::Mat img = make_panel(2048, 15000, 1200, 99999, 30, 150);
        auto r = EdgeCheck::run(img.data, img.cols, img.rows, cfg);
        check("3 尾緣未見 → transport_ok=false（前緣正常）",
              r.leading_found && !r.tail_found && !r.transport_ok(cfg.drift_warn_pct),
              "leading=" + std::to_string(r.leading_line) +
              " tail_found=" + std::to_string(r.tail_found));
    }

    // 4) 速度漂移：理論 12600 行，實際玻璃只走 12450 行（快 ~1.19%）
    {
        EdgeCheckConfig c = cfg; c.expected_panel_lines = 12600; c.drift_warn_pct = 0.2;
        cv::Mat img = make_panel(2048, 15000, 1200, 13650, 30, 150);
        auto r = EdgeCheck::run(img.data, img.cols, img.rows, c);
        const double expect_drift = 100.0 * (12450.0 - 12600.0) / 12600.0;  // ≈ -1.190%
        check("4 drift 數值正確（期望 -1.19%±0.06）且超閾值 → transport_ok=false",
              r.tail_found && std::fabs(r.drift_pct - expect_drift) <= 0.06 &&
                  !r.transport_ok(c.drift_warn_pct),
              EdgeCheck::summary(r, c));
    }

    // 5) 前緣範圍檢查
    {
        cv::Mat img = make_panel(2048, 15000, 1200, 13800, 30, 150);
        EdgeCheckConfig cin = cfg; cin.expected_leading_line = 1200; cin.leading_tolerance = 300;
        auto rin = EdgeCheck::run(img.data, img.cols, img.rows, cin);
        EdgeCheckConfig cout_ = cfg; cout_.expected_leading_line = 200; cout_.leading_tolerance = 300;
        auto rout = EdgeCheck::run(img.data, img.cols, img.rows, cout_);
        check("5 expected±tol 內 → align_ok；外 → 時序漂移（align_ok=false）",
              rin.align_ok() && rin.leading_in_range && !rout.align_ok() && !rout.leading_in_range,
              "in_range(1200±300)=" + std::to_string(rin.leading_in_range) +
              " in_range(200±300)=" + std::to_string(rout.leading_in_range));
    }

    // 6) disabled → 行為不變
    {
        cv::Mat img = make_panel(1024, 6000, 6000, 99999, 30, 150);  // 故意做壞片
        EdgeCheckConfig off; off.enabled = false;
        auto r = EdgeCheck::run(img.data, img.cols, img.rows, off);
        check("6 disabled → checked=false 且 align_ok/transport_ok 恆 true",
              !r.checked && r.align_ok() && r.transport_ok(off.drift_warn_pct),
              "checked=" + std::to_string(r.checked));
    }

    // 7) 短影像（單張 5000，前/尾窗完全重疊）：玻璃 [1000, 4000)
    {
        cv::Mat img = make_panel(2048, 5000, 1000, 4000, 30, 150);
        auto r = EdgeCheck::run(img.data, img.cols, img.rows, cfg);
        check("7 重疊窗仍分得出前/尾緣（1000±3 / 4000±3, 實測 3000±6）",
              r.leading_found && std::abs(r.leading_line - 1000) <= 3 &&
                  r.tail_found && std::abs(r.tail_line - 4000) <= 3 &&
                  std::labs(r.measured_lines - 3000) <= 6,
              "leading=" + std::to_string(r.leading_line) +
              " tail=" + std::to_string(r.tail_line) +
              " measured=" + std::to_string(r.measured_lines));
    }

    // 8) 對比不足不誤報；亮暗反轉仍可偵測
    {
        cv::Mat low = make_panel(2048, 12000, 1200, 10800, 30, 42);  // Δ12 < min_contrast=20
        auto rl = EdgeCheck::run(low.data, low.cols, low.rows, cfg);
        cv::Mat inv = make_panel(2048, 12000, 1200, 10800, 200, 40); // 玻璃比背景暗
        auto ri = EdgeCheck::run(inv.data, inv.cols, inv.rows, cfg);
        check("8a 對比不足（Δ12<20）→ 不誤報前/尾緣",
              !rl.leading_found && !rl.tail_found,
              "leading_found=" + std::to_string(rl.leading_found));
        check("8b 玻璃比背景暗（方向反轉）→ 仍偵測（1200±3 / 10800±3）",
              ri.leading_found && std::abs(ri.leading_line - 1200) <= 3 &&
                  ri.tail_found && std::abs(ri.tail_line - 10800) <= 3,
              "leading=" + std::to_string(ri.leading_line) +
              " tail=" + std::to_string(ri.tail_line));
    }

    // 9) INI [EdgeCheck] 解析
    {
        const char* path = "./edge_verify_test.ini";
        {
            std::ofstream f(path);
            f << "[Pattern]\npitch_x = 26\n\n"
              << "[EdgeCheck]\n"
              << "enabled = 1\n"
              << "frame_lines = 5000\n"
              << "min_contrast = 25   # 註解\n"
              << "expected_leading_line = 1300\n"
              << "leading_tolerance = 400\n"
              << "expected_panel_lines = 126000\n"
              << "drift_warn_pct = 0.25\n"
              << "tail_search_frames = 3\n";
        }
        auto c = EdgeCheck::load_config(path);
        std::remove(path);
        auto cmiss = EdgeCheck::load_config("./no_such_file.ini");
        check("9 INI 解析（含註解/缺檔回預設）",
              c.enabled && c.frame_lines == 5000 && c.min_contrast == 25 &&
                  c.expected_leading_line == 1300 && c.leading_tolerance == 400 &&
                  c.expected_panel_lines == 126000 &&
                  std::fabs(c.drift_warn_pct - 0.25) < 1e-9 && c.tail_search_frames == 3 &&
                  !cmiss.enabled,
              "enabled=" + std::to_string(c.enabled) +
              " min_contrast=" + std::to_string(c.min_contrast) +
              " missing_file_enabled=" + std::to_string(cmiss.enabled));
    }

    printf("============================================================\n");
    printf("結果: PASS %d / FAIL %d\n", g_pass, g_fail);
    printf("============================================================\n");
    return g_fail > 0 ? 1 : 0;
}
