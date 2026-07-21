#ifndef CFAOI_EDGE_CHECK_H
#define CFAOI_EDGE_CHECK_H

/**
 * ============================================================================
 * EdgeCheck — 玻璃前緣/尾緣健檢（Align Fail 警告 + 傳送片檢查）
 * ============================================================================
 *
 * 設計依據（docs plan「37 CCD 觸發設計」）：軟體觸發架構下，座標精度靠玻璃邊
 * 對位吸收啟動時間差；trigger 唯一責任是讓玻璃前緣落在第一張（frame_lines=5000
 * 條 = 40mm 行程窗口）內。本模組把這兩個前提變成每片實測：
 *
 *   前緣（Align Fail）：第一張窗口內找玻璃前緣行號。
 *     找不到 → 「進片 sensor → 取像」延遲漂移超窗（或 sensor/傳送異常）。
 *     行號超出 expected ± tolerance → 時序漂移早期預警（尚未 fail）。
 *   尾緣（傳送健檢）：最後 tail_search_frames 張窗口內找玻璃尾緣行號。
 *     找不到 → 片未走完 / 速度過慢 / 取像張數不足。
 *     (尾緣−前緣) vs 理論行數（玻璃長/8µm）→ speed_drift_pct，每片實測傳送速度。
 *
 * 偵測法：列平均亮度剖面（行方向、欄下採樣）→ 平滑 → 帶間隔差分 →
 * 前緣取窗內「第一個」超過 min_contrast 的躍變峰、尾緣取「最後一個」——
 * 兩窗重疊（短影像）時仍能區分前/尾緣。純 CPU、決定性、不碰 GPU 計時區
 * （不違反不變式 5/16）。方向無關（|躍變|），不假設玻璃比背景亮或暗。
 *
 * 現行單影像 = 整片 panel（單張或多張拼接）；未來 RDMA 逐 slice 到貨時，
 * 首/末 slice 各跑一窗即可（行號換算為全域行號），量測定義不變。
 * ============================================================================
 */

#include <cstdint>
#include <string>

// 機器層設定（INI [EdgeCheck]；不隨 recipe 換）。預設 enabled=false → 全行為不變。
struct EdgeCheckConfig {
    bool   enabled = false;
    int    frame_lines = 5000;          // 單張 slice 行數（前緣搜尋窗 = 第一張）
    int    min_contrast = 20;           // 邊緣躍變最小亮度差（灰階，帶間隔差分後）
    int    expected_leading_line = -1;  // 前緣預期行號；-1 = 只檢查存在，不檢查範圍
    int    leading_tolerance = 1500;    // expected ± tolerance（行）
    long   expected_panel_lines = 0;    // 理論玻璃行數（玻璃長mm/行解析度µm）；0 = 不算 drift
    double drift_warn_pct = 0.2;        // |speed_drift| 超過此 %（0.2 = 0.2%）→ 傳送速度漂移警告
    int    tail_search_frames = 2;      // 尾緣搜尋窗 = 最後 N 張
};

struct EdgeCheckResult {
    bool checked = false;          // enabled 且有跑（false → JSON 不輸出、全行為不變）
    // 前緣（Align Fail）
    bool leading_found = false;
    int  leading_line  = -1;       // 全域行號（影像座標）
    bool leading_in_range = true;  // expected 範圍檢查（未設 expected 恆 true）
    // 尾緣（傳送健檢）
    bool tail_found = false;
    int  tail_line  = -1;
    // 量測（前後緣皆找到才有效）
    long   measured_lines = 0;     // tail_line - leading_line
    long   expected_lines = 0;     // = cfg.expected_panel_lines（0 = 未設定）
    double drift_pct = 0.0;        // (measured-expected)/expected*100

    bool align_ok() const {
        return !checked || (leading_found && leading_in_range);
    }
    bool transport_ok(double drift_warn_pct) const {
        if (!checked) return true;
        if (!tail_found || tail_line <= leading_line) return false;
        if (expected_lines <= 0) return true;          // 未設理論值 → 只檢查尾緣存在
        return drift_pct >= -drift_warn_pct && drift_pct <= drift_warn_pct;
    }
};

namespace EdgeCheck {

// 讀 INI [EdgeCheck] section（缺 section/垃圾值 → 預設值，不 crash；與 load_optical_params 同精神）。
EdgeCheckConfig load_config(const std::string& ini_path);

// 對整片 panel 影像（Mono8, w*h, row-major）跑前緣+尾緣檢查。
// cfg.enabled=false → 回 checked=false 的空結果（零成本早退）。
EdgeCheckResult run(const uint8_t* img, int width, int height, const EdgeCheckConfig& cfg);

// 逐 slice 模式（RDMA 多 slice 到貨：sliceIndex==0 找前緣、==totalSlice-1 找尾緣）：
// 在「單張 slice」內找第一個/最後一個邊緣，回傳 slice 內行號（-1 = 未找到）。
// 全域行號 = sliceIndex×slice高 + 回傳值，由呼叫端換算。
int find_leading(const uint8_t* img, int width, int height, const EdgeCheckConfig& cfg);
int find_tail(const uint8_t* img, int width, int height, const EdgeCheckConfig& cfg);

// 人讀摘要（log / incident detail 用）。
std::string summary(const EdgeCheckResult& r, const EdgeCheckConfig& cfg);

}  // namespace EdgeCheck

#endif  // CFAOI_EDGE_CHECK_H
