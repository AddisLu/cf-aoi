#ifndef CFAOI_DEFECT_RULES_H
#define CFAOI_DEFECT_RULES_H

/**
 * defect_rules — #32 邊界略過 + #16 Rule 改判（CPU 後處理，決定性，CUDA-free）
 *
 * 在每個 zone 的 GPU 偵測完成後、計數/存圖之前套用：
 *   #32 BypassEdge：全域中心落在影像邊緣 bypass_edge_x/y 內 → 丟棄（對齊 legacy BypassEdgeX/Y）
 *   #16 Rule 改判：size>ng_size 強制 NG；否則 patch 均值<mean_low 或 H/W>hdivw → 改判 OK（丟棄）
 *
 * 兩者預設停用（bypass_edge=0 / image_rule_enable=false）→ 完全不改變既有結果（不破 bit-exact 不變式 #10）。
 * 純 CPU、對已 canonical 排序的 defects 線性過濾 → 同輸入同輸出，決定性。
 * 只依賴 DefectInfo（CUDA-free）+ OpenCV → 可用純 g++ 單元測（rules_verify），不需 nvcc。
 */

#include <algorithm>
#include <vector>

#include <opencv2/core.hpp>

#include "gpu/defect_info.h"              // struct DefectInfo（CUDA-free）
#include "config/recipe_saving_config.h"  // RecipeSavingConfig

namespace defect_rules {

// 就地過濾 defects（缺陷 center 為 ROI-local）。
// roi_off_x/y：該 zone ROI 在全影像的原點 → 全域中心 = roi_off + local center。
// gray：全影像 Mono8（#16 patch 均值來源）；img_w/h：全影像尺寸（#32 邊界）。
inline void apply(std::vector<DefectInfo>& defects, int roi_off_x, int roi_off_y,
                  const cv::Mat& gray, int img_w, int img_h,
                  const RecipeSavingConfig& cfg) {
    const int  bx = cfg.bypass_edge_x, by = cfg.bypass_edge_y;
    const bool do_edge = (bx > 0 || by > 0);
    const bool do_rule = cfg.image_rule_enable;
    if (!do_edge && !do_rule) return;   // 全關 → 原樣（向下相容，不破 bit-exact）

    const int pw = cfg.save_defect_width  > 0 ? cfg.save_defect_width  : 100;
    const int ph = cfg.save_defect_height > 0 ? cfg.save_defect_height : 100;

    std::vector<DefectInfo> kept;
    kept.reserve(defects.size());
    for (const auto& d : defects) {
        const int gcx = roi_off_x + static_cast<int>(d.center_x);
        const int gcy = roi_off_y + static_cast<int>(d.center_y);

        // #32 邊界略過（對齊 legacy：<=bx 或 >=(W-1)-bx 視為邊界缺陷 → 丟）
        if (do_edge) {
            if (gcx <= bx || gcx >= (img_w - 1) - bx ||
                gcy <= by || gcy >= (img_h - 1) - by)
                continue;
        }

        // #16 Rule 改判
        if (do_rule) {
            const int w = d.max_x - d.min_x + 1;
            const int h = d.max_y - d.min_y + 1;
            const double hw = (w > 0) ? static_cast<double>(h) / static_cast<double>(w) : 0.0;
            const bool force_ng = static_cast<double>(d.size) > cfg.ng_size_threshold;
            if (!force_ng) {
                // patch 均值（save_width×save_height 視窗，以全域中心為中心，夾邊界）
                int x1 = std::max(0, gcx - pw / 2), y1 = std::max(0, gcy - ph / 2);
                int x2 = std::min(img_w, x1 + pw),  y2 = std::min(img_h, y1 + ph);
                double mean = 0.0;
                if (x2 > x1 && y2 > y1 && !gray.empty())
                    mean = cv::mean(gray(cv::Rect(x1, y1, x2 - x1, y2 - y1)))[0];
                if (mean < cfg.mean_low_threshold || hw > cfg.hdivw_threshold)
                    continue;   // 改判 OK → 不計缺陷
            }
        }
        kept.push_back(d);
    }
    defects = std::move(kept);
}

// Step E — Blob 過濾：size 範圍過濾 + 鄰近合併（對齊 legacy BlobMinSize/MaxSize/AllMergeDistance）。
// defects 為連通元件（blob 分析輸出，含 size 與 bbox），且已 canonical 排序 → 決定性、CUDA-free。
//   ① size 過濾：size < min 或 > max → 丟（min 為 FP 抑制主力：去 size 1-2 雜訊；實測 IP04 338→65）。
//   ② 合併：中心距 ≤ merge_distance 之同型缺陷以 union-find 併成一顆（bbox 聯集、size 相加、bbox 中心）。
// 全 0 → 原樣（向下相容，不破 bit-exact 不變式 #10）。
inline void apply_blob(std::vector<DefectInfo>& defects,
                       int blob_min_size, int blob_max_size, int merge_distance) {
    if (blob_min_size <= 0 && blob_max_size <= 0 && merge_distance <= 0) return;

    // ① size 過濾
    if (blob_min_size > 0 || blob_max_size > 0) {
        std::vector<DefectInfo> kept; kept.reserve(defects.size());
        for (const auto& d : defects) {
            if (blob_min_size > 0 && d.size < blob_min_size) continue;
            if (blob_max_size > 0 && d.size > blob_max_size) continue;
            kept.push_back(d);
        }
        defects = std::move(kept);
    }

    // ② 鄰近合併（union-find by center distance，同型 bright/dark）
    if (merge_distance > 0 && defects.size() > 1) {
        const long md2 = (long)merge_distance * (long)merge_distance;
        const int n = (int)defects.size();
        std::vector<int> parent(n);
        for (int i = 0; i < n; ++i) parent[i] = i;
        auto find = [&parent](int a) { while (parent[a] != a) { parent[a] = parent[parent[a]]; a = parent[a]; } return a; };
        for (int i = 0; i < n; ++i)
            for (int j = i + 1; j < n; ++j) {
                if (defects[i].is_bright != defects[j].is_bright) continue;
                long dx = (long)(defects[i].center_x - defects[j].center_x);
                long dy = (long)(defects[i].center_y - defects[j].center_y);
                if (dx * dx + dy * dy <= md2) { int ri = find(i), rj = find(j); if (ri != rj) parent[ri] = rj; }
            }
        std::vector<DefectInfo> merged;
        std::vector<int> rootIdx(n, -1);
        for (int i = 0; i < n; ++i) {
            int r = find(i);
            if (rootIdx[r] < 0) { rootIdx[r] = (int)merged.size(); merged.push_back(defects[i]); }
            else {
                DefectInfo& m = merged[rootIdx[r]];
                m.size  += defects[i].size;
                m.min_x  = std::min(m.min_x, defects[i].min_x);
                m.min_y  = std::min(m.min_y, defects[i].min_y);
                m.max_x  = std::max(m.max_x, defects[i].max_x);
                m.max_y  = std::max(m.max_y, defects[i].max_y);
            }
        }
        for (auto& m : merged) {
            m.center_x = (m.min_x + m.max_x) * 0.5f;
            m.center_y = (m.min_y + m.max_y) * 0.5f;
        }
        defects = std::move(merged);
    }
}

// 重算 bright/dark 計數（過濾後）。回傳缺陷總數。
inline int recount(const std::vector<DefectInfo>& defects, int& num_bright, int& num_dark) {
    num_bright = 0; num_dark = 0;
    for (const auto& d : defects) { if (d.is_bright) ++num_bright; else ++num_dark; }
    return static_cast<int>(defects.size());
}

}  // namespace defect_rules

#endif  // CFAOI_DEFECT_RULES_H
