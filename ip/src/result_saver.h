#ifndef CFAOI_RESULT_SAVER_H
#define CFAOI_RESULT_SAVER_H

/**
 * ============================================================================
 * ResultSaver — 輸出檢測結果（JSON + legacy XML 雙寫）
 * ============================================================================
 *
 * 每張影像（可能含多個 ROI/zone）輸出：
 *   <out>/<yyyyMMdd>/<panelId>_<recipeName>/         ← 日期夾 / 一塊 panel 一夾
 *      Defect_<IpName>_Slice<ff>_Roi<rr>_Run<nn>_X<xxxx>_Y<yyyyyy>_Dr<reason>.png   缺陷小圖（全域座標）
 *      <panelId>_<recipeName>_ResultInfo.json        新版 JSON（欄位沿用 legacy 名稱）
 *      <panelId>_<recipeName>_ResultInfo.xml         legacy JudgeResult schema（上位機鏈相容）
 *      <panelId>_<recipeName>_result.png             overlay（亮缺陷紅框、暗缺陷藍框；PNG 低壓縮）
 *
 * 缺陷座標在 ZoneResult 內為 ROI-local（GC_X/GC_Y）；
 * GlobalPosX/Y = ROI offset + local，patch/overlay 用全域座標。
 * ============================================================================
 */

#include <cstdint>
#include <string>
#include <vector>

#include "gpu/gpu_pipeline.h"            // DetectionResult
#include "config/zone_config_adapter.h"  // ZoneConfig

// 單一 zone 的檢測結果（缺陷座標為 ROI-local）。
struct ZoneResult {
    int zone_index = 0;
    int roi_offset_x = 0;   // ROI 原點（全幅時為 0）
    int roi_offset_y = 0;
    ZoneConfig zone;        // 該 zone 的參數（供 death-margin 計算/log 用）
    DetectionResult result; // result.defects 座標相對於該 ROI 子影像
};

// 一張影像（panel）的完整檢測結果（聚合所有 zone）。
struct InspectionResult {
    std::string panel_id;
    std::string recipe_name = "DEFAULT";
    int image_width = 0;
    int image_height = 0;
    int frame_height = 0;   // 單一 CCD frame 高度（多 frame 拼接時用於 Slice 計算；0 → 用 image_height）
    bool ai_used = false;   // 本次是否實際做 AI 分類（停用時缺陷標待人工複核）
    double opt_res_x = 0.0;  // 機器光學解析度（μm/pixel）；0.0 = 未設定，GlobalPos_um 輸出 0.0
    double opt_res_y = 0.0;
    int ccd_index = 0;        // CCD 位置索引（預留多 CCD 拼接，值固定 0）
    double total_time_ms = 0.0;
    std::vector<ZoneResult> zones;

    int total_defects() const {
        int n = 0;
        for (const auto& z : zones) n += z.result.num_defects;
        return n;
    }
    bool pass() const { return total_defects() == 0; }
};

// 存圖選項（調參階段可關圖/限張數加速；overlay 用 PNG、patch 多緒平行寫）。
struct SaveOptions {
    bool save_patches  = true;   // false → 只存 ResultInfo，不存任何缺陷小圖
    bool save_overlay  = true;   // false → 不存 overlay 全圖
    int  max_patches   = -1;     // >=0 → 只存前 N 張缺陷小圖；對應 recipe_saving.max_save_defect_count
    int  threads       = 0;      // 缺陷小圖平行寫入緒數；0 → 自動（hardware_concurrency）
    int  save_width    = 100;    // 缺陷小圖寬（px）；對應 recipe_saving.save_defect_width
    int  save_height   = 100;    // 缺陷小圖高（px）；對應 recipe_saving.save_defect_height
};

namespace ResultSaver {

// 寫出 <out>/<yyyyMMdd>/<panelId>_<recipeName>/ 下的 ResultInfo(json/xml) + Defect 小圖 + overlay。
// img 為原始 grayscale 全影像 (w*h, Mono8)；ip_name 進缺陷檔名。回傳寫出的缺陷小圖數量。
// out_panel_dir 回傳實際輸出的 panel 資料夾（供 log / 測試），可為 nullptr。
int save(const InspectionResult& result,
         const uint8_t* img, int w, int h,
         const std::string& out_dir,
         const std::string& ip_name,
         const SaveOptions& opt = {},
         std::string* out_panel_dir = nullptr);

// 新版 JSON 字串（欄位沿用 legacy 名稱）。供 GET_STATUS / 回報給 Control 用。
std::string to_json(const InspectionResult& result);

}  // namespace ResultSaver

#endif // CFAOI_RESULT_SAVER_H
