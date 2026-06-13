#ifndef CFAOI_ZONE_CONFIG_ADAPTER_H
#define CFAOI_ZONE_CONFIG_ADAPTER_H

/**
 * ============================================================================
 * ZoneConfig — 演算法參數橋接層
 * ============================================================================
 *
 * 兩種來源 → GPU pipeline 直接吃的參數：
 *   1. default_zone.ini（gpu_algo INI）→ from_ini()  → 單一全幅 zone
 *   2. legacy RecipeInfo.xml 的 Recipe/DetectRoiList → from_recipe_xml() → 多 zone
 *
 * legacy DetectRoi → ZoneConfig 對應（已逐檔驗證原始碼）：
 *   BrightThreshold → BTH        DarkThreshold → DTH      （僅 AlgorithmCompare=="DIV"）
 *   PitchX → pitch_x             PitchY → pitch_y
 *   SearchX → search_range_x     SearchY → search_range_y
 *   fast_search_range = clamp(SearchY, 0, 2)
 *   StartX/StartY/EndX/EndY → ROI 範圍（-1 = 全幅）
 *
 * ⚠️ 只接受 DIV 模式：gpu_algo kernel 是比例式（center/mean₈ vs BTH/DTH），
 *    legacy DIV 同定義域故可直接對應；SUB 模式是灰階差、轉比例需背景灰階 → 直接拒絕。
 *
 * ZoneConfig 是純 CPU 結構（不依賴 CUDA），由 GpuPipeline 映射成 KernelParams。
 * ============================================================================
 */

#include <string>
#include <vector>
#include <stdexcept>

struct ZoneConfig {
    // === 影像 ===
    int width  = 8192;
    int height = 5000;

    // === ROI 範圍（-1 = 全幅；來自 legacy StartX/StartY/EndX/EndY）===
    int zone_index = 0;
    int roi_start_x = -1;
    int roi_start_y = -1;
    int roi_end_x   = -1;
    int roi_end_y   = -1;

    // === Pattern ===
    int pitch_x = 26;
    int pitch_y = 19;
    int search_range_x = 3;
    int search_range_y = 3;
    int fast_search_range = 1;   // 0=none, 1=±1px, 2=±2px
    int enable_multiscale = 1;   // 0=off, 1=2x, 2=2x+4x

    // === Threshold（比例式，DIV 域）===
    float BTH = 1.20f;           // ← BrightThreshold
    float DTH = 0.70f;           // ← DarkThreshold

    // === Lens Shading Correction ===
    bool  enable_lsc = false;
    float lsc_k1 = 0.15f;
    float lsc_k2 = 0.05f;
    float lsc_k3 = 0.00f;
    float lsc_max_gain = 1.5f;
    bool  lsc_auto_calibrate = false;

    // === GPU（內部固定）===
    int block_dim_x = 16;
    int block_dim_y = 16;

    // === Metadata ===
    std::string recipe_name = "DEFAULT";
    std::string panel_id;

    // ROI 是否為全幅（任一邊界 < 0 視為全幅）。
    bool is_full_frame() const {
        return roi_start_x < 0 || roi_start_y < 0 || roi_end_x < 0 || roi_end_y < 0;
    }
};

// from_recipe_xml 遇到非 DIV 模式或解析失敗時丟出此例外。
class RecipeError : public std::runtime_error {
public:
    explicit RecipeError(const std::string& msg) : std::runtime_error(msg) {}
};

namespace ZoneConfigAdapter {

// 讀取 default_zone.ini，回傳單一全幅 zone（找不到檔案則用內建預設並印警告）。
ZoneConfig from_ini(const std::string& path);

// 解析 legacy RecipeInfo.xml（序列化的 Recipe），回傳 DetectRoiList 每個 DetectRoi 一個 ZoneConfig。
// defaults 提供 recipe 未涵蓋之欄位（multiscale/LSC/block_dim 等），通常傳入 from_ini 的結果。
// 任一 zone 的 AlgorithmCompare != "DIV" → 丟 RecipeError。
std::vector<ZoneConfig> from_recipe_xml(const std::string& xml_path,
                                        const ZoneConfig& defaults = ZoneConfig{});

// 同上，但直接吃 XML 內容字串（跨機器：Control 經 TCP 送配方內容，IP 端不需共用檔案系統）。
std::vector<ZoneConfig> from_recipe_xml_content(const std::string& xml,
                                                const ZoneConfig& defaults = ZoneConfig{});

}  // namespace ZoneConfigAdapter

#endif // CFAOI_ZONE_CONFIG_ADAPTER_H
