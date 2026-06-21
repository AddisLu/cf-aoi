/**
 * rules_verify.cpp — #16 Rule 改判 + #32 邊界略過 單元驗證（純 OpenCV，不需 CUDA）
 *
 * 用合成 DefectInfo + 合成灰階影像驗 defect_rules::apply 各分支：
 *   #32 邊界略過、#16 MeanOK / HwRatioOK / NgSize 強制NG、預設全關 passthrough、recount。
 * 編譯：CMakeLists.txt 的 rules_verify target；執行：./rules_verify
 */

#include <opencv2/opencv.hpp>
#include <cstdio>
#include <string>
#include <vector>

#include "defect_rules.h"

static int g_pass = 0, g_fail = 0;
static void check(const std::string& name, bool cond, const std::string& detail) {
    printf("[%s] %s\n  %s\n", cond ? "PASS" : "FAIL", name.c_str(), detail.c_str());
    if (cond) g_pass++; else g_fail++;
}

// 造一個 DefectInfo：中心 (cx,cy)、blob 尺寸 size、bounding w×h、亮/暗。
static DefectInfo mk(float cx, float cy, int size, int w, int h, int bright) {
    DefectInfo d{};
    d.center_x = cx; d.center_y = cy; d.size = size;
    d.min_x = static_cast<int>(cx) - w / 2; d.max_x = d.min_x + w - 1;
    d.min_y = static_cast<int>(cy) - h / 2; d.max_y = d.min_y + h - 1;
    d.is_bright = bright; d.avg_brightness = bright ? 220.f : 40.f;
    return d;
}

int main() {
    printf("============================================================\n");
    printf("   #16 Rule 改判 + #32 邊界略過 — rules_verify\n");
    printf("============================================================\n");

    const int W = 1000, H = 1000;
    cv::Mat gray(H, W, CV_8UC1, cv::Scalar(128));
    cv::rectangle(gray, cv::Rect(300, 300, 120, 120), cv::Scalar(20), -1);  // 暗區 (300..419)

    // #32 邊界略過：bx=by=50
    {
        RecipeSavingConfig cfg; cfg.bypass_edge_x = 50; cfg.bypass_edge_y = 50;
        std::vector<DefectInfo> v = {
            mk(500, 500, 25, 5, 5, 1),   // 中心 → 保留
            mk(30, 500, 25, 5, 5, 1),    // 左邊界內 (30<=50) → 丟
            mk(500, 970, 25, 5, 5, 1),   // 下邊界 (970>=949) → 丟
        };
        defect_rules::apply(v, 0, 0, gray, W, H, cfg);
        check("#32 邊界略過 3→1（中心留, 左/下邊界丟）",
              v.size() == 1 && static_cast<int>(v[0].center_x) == 500,
              "kept=" + std::to_string(v.size()));
    }

    // #16 MeanOK：暗區缺陷 patch 均值 < mean_low → 改判 OK
    {
        RecipeSavingConfig cfg; cfg.image_rule_enable = true;
        cfg.mean_low_threshold = 40; cfg.hdivw_threshold = 100; cfg.ng_size_threshold = 1e9;
        cfg.save_defect_width = 100; cfg.save_defect_height = 100;
        std::vector<DefectInfo> v = {
            mk(500, 500, 25, 5, 5, 1),   // patch 均值 128 → 保留
            mk(355, 355, 25, 5, 5, 1),   // 暗區 patch 均值 20<40 → MeanOK 丟
        };
        defect_rules::apply(v, 0, 0, gray, W, H, cfg);
        check("#16 MeanOK：暗區缺陷改判 OK 2→1",
              v.size() == 1 && static_cast<int>(v[0].center_x) == 500,
              "kept=" + std::to_string(v.size()));
    }

    // #16 HwRatioOK：H/W > hdivw → 改判 OK
    {
        RecipeSavingConfig cfg; cfg.image_rule_enable = true;
        cfg.mean_low_threshold = 0; cfg.hdivw_threshold = 4; cfg.ng_size_threshold = 1e9;
        std::vector<DefectInfo> v = {
            mk(500, 500, 25, 5, 5, 1),    // H/W=1 → 保留
            mk(600, 600, 40, 2, 20, 1),   // H/W=10>4 → HwRatioOK 丟
        };
        defect_rules::apply(v, 0, 0, gray, W, H, cfg);
        check("#16 HwRatioOK：細長缺陷改判 OK 2→1",
              v.size() == 1 && static_cast<int>(v[0].center_x) == 500,
              "kept=" + std::to_string(v.size()));
    }

    // #16 NgSize 強制 NG：size>ng_size 即使符合 MeanOK 也保留
    {
        RecipeSavingConfig cfg; cfg.image_rule_enable = true;
        cfg.mean_low_threshold = 40; cfg.hdivw_threshold = 4; cfg.ng_size_threshold = 4096;
        cfg.save_defect_width = 100; cfg.save_defect_height = 100;
        std::vector<DefectInfo> v = {
            mk(360, 360, 5000, 5, 5, 1),  // 暗區(會 MeanOK) 但 size 5000>4096 → 強制 NG 保留
            mk(355, 355, 25, 5, 5, 1),    // 暗區小缺陷 → MeanOK 丟
        };
        defect_rules::apply(v, 0, 0, gray, W, H, cfg);
        check("#16 NgSize 強制 NG：大缺陷保留即使符合 MeanOK 2→1",
              v.size() == 1 && v[0].size == 5000,
              "kept=" + std::to_string(v.size()));
    }

    // 預設全關 → passthrough（不破 bit-exact）
    {
        RecipeSavingConfig cfg;   // 全部預設 off
        std::vector<DefectInfo> v = { mk(5, 5, 25, 5, 5, 1), mk(355, 355, 25, 5, 5, 1), mk(600, 600, 40, 2, 20, 1) };
        size_t before = v.size();
        defect_rules::apply(v, 0, 0, gray, W, H, cfg);
        check("預設全關 passthrough（不破 bit-exact）",
              v.size() == before, "before=" + std::to_string(before) + " after=" + std::to_string(v.size()));
    }

    // recount bright/dark
    {
        std::vector<DefectInfo> v = { mk(1, 1, 1, 1, 1, 1), mk(2, 2, 1, 1, 1, 0), mk(3, 3, 1, 1, 1, 1) };
        int nb, nd; int n = defect_rules::recount(v, nb, nd);
        check("recount bright/dark", n == 3 && nb == 2 && nd == 1,
              "n=" + std::to_string(n) + " bright=" + std::to_string(nb) + " dark=" + std::to_string(nd));
    }

    printf("\n============================================================\n");
    printf("結果: PASS %d / FAIL %d\n", g_pass, g_fail);
    printf("============================================================\n");
    return g_fail > 0 ? 1 : 0;
}
