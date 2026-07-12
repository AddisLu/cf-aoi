// ═══ 📖 手冊對照（docs/html/cf-aoi-training.html，開啟後 ⌘K 搜章節）═══
// [手冊 ch4] 對位動畫（CHECK→SET、全幅跳過、F1 bug 重演）＋對位流程 R4-F5
// [手冊 p4] run_align 導師卡（失敗不回位移=誠實失敗）
// ═══════════════════════════════════════════════════════════════
#include "align_engine.h"

#include <cmath>
#include <cstdio>
#include <stdexcept>
#include <string>

#include <opencv2/imgproc.hpp>

namespace {

// 繞中心旋轉 golden template angle 度（正 = 逆時針）
cv::Mat rotate_template(const cv::Mat& tmpl, double angle_deg) {
    if (std::abs(angle_deg) < 1e-6) return tmpl;
    cv::Point2f center(tmpl.cols * 0.5f, tmpl.rows * 0.5f);
    cv::Mat M = cv::getRotationMatrix2D(center, angle_deg, 1.0);
    cv::Mat rotated;
    cv::warpAffine(tmpl, rotated, M, tmpl.size(),
                   cv::INTER_LINEAR, cv::BORDER_CONSTANT, cv::Scalar(0));
    return rotated;
}

// 次像素擬合採 3-point parabolic，內聯於 run_align 的峰值分支（見下方 X/Y 各自 fit）。

}  // namespace

AlignResult run_align(const cv::Mat& search_roi, const AlignRoiConfig& cfg) {
    AlignResult res;

    // 基本驗證
    if (cfg.golden.empty()) {
        res.error_msg = "align_failed: golden not loaded";
        return res;
    }
    if (search_roi.empty() || search_roi.type() != CV_8UC1) {
        res.error_msg = "align_failed: invalid search_roi (must be 8-bit gray)";
        return res;
    }
    if (cfg.golden.cols > search_roi.cols || cfg.golden.rows > search_roi.rows) {
        char buf[128];
        std::snprintf(buf, sizeof(buf),
                      "align_failed: golden(%dx%d) larger than search_roi(%dx%d)",
                      cfg.golden.cols, cfg.golden.rows,
                      search_roi.cols, search_roi.rows);
        res.error_msg = buf;
        return res;
    }

    double best_score = -1.0;
    double best_col   = 0.0;
    double best_row   = 0.0;
    double best_angle = 0.0;

    // 多角度搜尋：從 -range 到 +range，step 度
    float  range = cfg.angle_range_deg;
    float  step  = cfg.angle_step_deg > 0 ? cfg.angle_step_deg : 0.5f;
    int    n_ang = static_cast<int>(std::round(2.0f * range / step)) + 1;

    for (int ai = 0; ai < n_ang; ++ai) {
        double angle = -range + ai * step;

        cv::Mat rotated_golden = rotate_template(cfg.golden, angle);

        // matchTemplate → result 尺寸 = (roi - golden + 1)
        cv::Mat result;
        cv::matchTemplate(search_roi, rotated_golden, result, cv::TM_CCOEFF_NORMED);

        double min_val, max_val;
        cv::Point min_loc, max_loc;
        cv::minMaxLoc(result, &min_val, &max_val, &min_loc, &max_loc);

        if (max_val > best_score) {
            best_score = max_val;
            best_angle = angle;

            // 拋物線次像素擬合（分別在 X / Y 方向獨立 3-point fit）
            int bc = max_loc.x;
            int br = max_loc.y;

            double sub_c = static_cast<double>(bc);
            double sub_r = static_cast<double>(br);

            if (bc > 0 && bc < result.cols - 1) {
                float fm1 = result.at<float>(br, bc - 1);
                float f0  = result.at<float>(br, bc);
                float fp1 = result.at<float>(br, bc + 1);
                float denom = fm1 - 2.0f * f0 + fp1;
                if (std::abs(denom) > 1e-9f)
                    sub_c = bc - 0.5f * (fp1 - fm1) / denom;
            }
            if (br > 0 && br < result.rows - 1) {
                float fm1 = result.at<float>(br - 1, bc);
                float f0  = result.at<float>(br,     bc);
                float fp1 = result.at<float>(br + 1, bc);
                float denom = fm1 - 2.0f * f0 + fp1;
                if (std::abs(denom) > 1e-9f)
                    sub_r = br - 0.5f * (fp1 - fm1) / denom;
            }

            best_col = sub_c;
            best_row = sub_r;
        }
    }

    res.score     = best_score;
    res.angle_deg = best_angle;

    if (best_score < cfg.score_threshold) {
        char buf[128];
        std::snprintf(buf, sizeof(buf),
                      "align_failed: score=%.3f < threshold=%.3f",
                      best_score, static_cast<double>(cfg.score_threshold));
        res.error_msg = buf;
        return res;
    }

    // match 中心（相對 search_roi 左上角）：
    //   matchTemplate 結果的 (col, row) 是 rotated_golden 左上角位置
    //   → 加上 golden 半尺寸得到 golden 中心在 search_roi 中的座標
    double match_cx = best_col + cfg.golden.cols * 0.5;
    double match_cy = best_row + cfg.golden.rows * 0.5;

    // ShiftX/Y = 找到的 Mark 中心 - 搜尋 ROI 中心
    // （等效 legacy：tmpMarkCx - childSizeX/2）
    res.shift_x = match_cx - search_roi.cols * 0.5;
    res.shift_y = match_cy - search_roi.rows * 0.5;
    res.ok      = true;

    return res;
}
