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
 *   BrightThreshold → BTH        DarkThreshold → DTH      （同名欄位，**依演算法域解讀**，見下）
 *   PitchX → pitch_x             PitchY → pitch_y
 *   SearchX → search_range_x     SearchY → search_range_y
 *   fast_search_range = clamp(SearchY, 0, 2)               （mode0 DIV kernel 實吃的垂直局部搜尋）
 *   PitchTime → pitch_times      ChooseAmount → choose_amount   （SUB/DIV-voting 投票路數與門檻）
 *   MeanLowThreshold → mean_low_threshold                  （DIV-voting 暗區棄權 dark_eps）
 *   M_ImagePreproc → preproc_remap   SmoothTimes2 → smooth_times2   （SUB/融合前處理）
 *   Blob{Min,Max}Size/BlobAllMergeDistance → blob_*        （Step E CPU 後處理，所有模式）
 *   StartX/StartY/EndX/EndY → ROI 範圍（-1 = 全幅）
 *
 * ★ 三域守門（2026-06-23 SUB 管線移植後；**取代舊「只接受 DIV」的敘述**，詳見 ip/CLAUDE.md §5）：
 *    權威欄位是 <M_AlgorithmWayCompare>（legacy enum），**不是** stale 的 <AlgorithmCompare> 字串——
 *    舊守門只比字串，曾被「掛 DIV 字串、實為 SUB」的 recipe 騙過 → 靜默假 PASS（血淚教訓，勿回退）。
 *      含 Sub                    → algo_mode=1 SUB（BTH/DTH = 灰階差，如 +17/−16）
 *      含 Div + 投票標記(star/way) → algo_mode=2 DIV-voting 融合（BTH/DTH = 比值域 BTH>1>DTH>0）
 *      含 Div / AlgorithmCompare="DIV" → algo_mode=0 DIV（比例式；DarkThreshold<0 = 域值誤標 → 拒載）
 *      皆判不出                   → 拋 RecipeError 拒載（不靜默預設）
 *    BTH/DTH **不做任何跨域近似轉換**（SUB 灰階差轉比例需依賴背景灰階，無固定公式）。
 *    ⚠️ 已知限制（docs/code_review_20260802.md I5）：legacy enum 值全為 Awc_*_Way_*_Div（皆含 "Way"）
 *       → 任何帶 awc 的 legacy DIV recipe 都路由到 mode2，純 mode0 幾乎不可達；語意待裁示。
 *
 * ZoneConfig 是純 CPU 結構（不依賴 CUDA），由 GpuPipeline 映射成 KernelParams。
 * ============================================================================
 */

#include <string>
#include <vector>
#include <stdexcept>
#include <cmath>

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

    // === 演算法模式 ===
    // 0 = DIV（比例式 center/mean₈ vs BTH/DTH，gpu_algo kernel）
    // 1 = SUB（灰階差 8-Way-Star 投票，legacy 移植；BTH/DTH 為灰階差，配 pitch_times/choose_amount）
    // 2 = DIV-voting（融合：DIV 比值逐路 + legacy 投票 + 暗區棄權；BTH/DTH 為比值域）
    int algo_mode = 0;
    int mean_low_threshold = 40;  // ← MeanLowThreshold：DIV-voting 暗區棄權門檻(dark_eps)
    int pitch_times   = 1;       // ← legacy PitchTime（SUB：每方向 pitch 倍數；DIV 忽略）
    int choose_amount = 1;       // ← legacy ChooseAmount（SUB：≥幾路超標才算缺陷；DIV 忽略）

    // === SUB 前處理（legacy 偵測前；DIV 忽略）===
    bool preproc_remap = false;  // ← M_ImagePreproc==Ip_Remap：MimRemap(M_FIT_SRC_DATA) 對比拉伸
    int  smooth_times  = 0;      // ← SmoothTimes（5×5 高斯次數；目前僅支援 3×3，5×5 待補）
    int  smooth_times2 = 0;      // ← SmoothTimes2（3×3 高斯 Gau3x3_8 次數）

    // === Blob 過濾（Step E：偵測後 CPU 後處理；0 = 關，向下相容）===
    int  blob_min_size      = 0; // ← BlobMinSize：連通元件 size < 此值 → 丟（壓 size 1-2 雜訊 FP 主力）
    int  blob_max_size      = 0; // ← BlobMaxSize：size > 此值 → 丟（0 = 不限）
    int  blob_merge_distance = 0;// ← BlobAllMergeDistance：中心距 ≤ 此值之同型缺陷合併成一顆

    // === Threshold ===
    // DIV 域：BTH=BrightThreshold(比例,如1.2)、DTH=DarkThreshold(比例,如0.7)
    // SUB 域：BTH=BrightThreshold(灰階差,如+17)、DTH=DarkThreshold(灰階差,如-16)
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

    // === 對位套回欄位（-1 = 尚未對位，eff_* 自動 fallback 到 roi_*）===
    // SET_ALIGN 命令套回後填入；LOAD_RECIPE 時重設為 -1。
    int aligned_start_x = -1;
    int aligned_start_y = -1;
    int aligned_end_x   = -1;
    int aligned_end_y   = -1;

    // 偵測路徑統一使用 eff_*（對位前後均正確）
    int eff_start_x() const { return aligned_start_x >= 0 ? aligned_start_x : roi_start_x; }
    int eff_start_y() const { return aligned_start_y >= 0 ? aligned_start_y : roi_start_y; }
    int eff_end_x()   const { return aligned_end_x   >= 0 ? aligned_end_x   : roi_end_x;   }
    int eff_end_y()   const { return aligned_end_y   >= 0 ? aligned_end_y   : roi_end_y;   }

    // ROI 是否為全幅（任一邊界 < 0 視為全幅）。使用 eff_* 考慮對位後結果。
    bool is_full_frame() const {
        return eff_start_x() < 0 || eff_start_y() < 0 || eff_end_x() < 0 || eff_end_y() < 0;
    }
};

// SET_ALIGN：把 ShiftX/Y 套回所有 DetectRoi 的 aligned_* 欄位（偵測走 eff_*）。
// F1：全幅 zone（roi_*=-1）必須跳過——否則 -1+shift≥0 會讓 is_full_frame() 翻 false、
//     zone_rect 由全幅塌成 ~1px。保留 -1 sentinel 才能維持「全幅」語意。
// main.cpp 的 SET_ALIGN handler 與 align_verify 共用此函式（單一真相、可單元測）。
inline void apply_align_shift(std::vector<ZoneConfig>& zones, double shift_x, double shift_y) {
    const int sx = (int)std::round(shift_x);
    const int sy = (int)std::round(shift_y);
    for (auto& z : zones) {
        if (z.is_full_frame()) continue;          // 全幅不套位移（保留 -1）
        z.aligned_start_x = z.roi_start_x + sx;
        z.aligned_start_y = z.roi_start_y + sy;
        z.aligned_end_x   = z.roi_end_x   + sx;
        z.aligned_end_y   = z.roi_end_y   + sy;
    }
}

// 機器層光學參數（不隨 recipe 換，不塞進 ZoneConfig）。
struct OpticalParams {
    double opt_res_x = 0.0;   // μm/pixel；0.0 = 未設定，GlobalPos_um 輸出 0.0
    double opt_res_y = 0.0;
    int ccd_index = 0;         // CCD 位置索引；預留多 CCD 拼接，值固定 0
};

// #23 興趣區（Interest ROI）：legacy DetectIoiList 的矩形（像素座標，與偵測無關，僅供存圖/監看）。
struct IoiRect {
    int start_x = -1, start_y = -1, end_x = -1, end_y = -1;
};

// from_recipe_xml 無法判定演算法域、域值錯配（DIV 但 DTH<0）、或解析失敗（無 DetectRoi）時丟出此例外。
// 呼叫端一律「拒載 + 報錯 + 記 recipe_load incident」，**不得靜默套用預設值繼續跑**（假 PASS 教訓）。
class RecipeError : public std::runtime_error {
public:
    explicit RecipeError(const std::string& msg) : std::runtime_error(msg) {}
};

namespace ZoneConfigAdapter {

// 讀取 default_zone.ini，回傳單一全幅 zone（找不到檔案則用內建預設並印警告）。
ZoneConfig from_ini(const std::string& path);

// 從 INI 讀取機器層光學參數（不依賴 ZoneConfig；缺 section / 垃圾值 → 0.0，不 crash）。
OpticalParams load_optical_params(const std::string& ini_path);

// 解析 legacy RecipeInfo.xml（序列化的 Recipe），回傳 DetectRoiList 每個 DetectRoi 一個 ZoneConfig。
// defaults 提供 recipe 未涵蓋之欄位（multiscale/LSC/block_dim 等），通常傳入 from_ini 的結果。
// ⚠️ 任一 zone **判不出演算法域**（M_AlgorithmWayCompare 不含 Sub/Div 且無 AlgorithmCompare="DIV"）
//    或域值錯配（標 DIV 但 DarkThreshold<0）→ 丟 RecipeError 拒載（三域守門，見檔頭）。
//    「非 DIV 一律拒絕」是**舊行為**，SUB/DIV-voting 現為合法域，勿依此舊假設寫程式。
std::vector<ZoneConfig> from_recipe_xml(const std::string& xml_path,
                                        const ZoneConfig& defaults = ZoneConfig{});

// 同上，但直接吃 XML 內容字串（跨機器：Control 經 TCP 送配方內容，IP 端不需共用檔案系統）。
// #23 解析 <DetectIoiList> 內的 <DetectIoi> 矩形（StartX/StartY/EndX/EndY）。無則回空。
std::vector<IoiRect> parse_ioi_list(const std::string& xml_content);
std::vector<IoiRect> parse_ioi_list_from_file(const std::string& xml_path);

std::vector<ZoneConfig> from_recipe_xml_content(const std::string& xml,
                                                const ZoneConfig& defaults = ZoneConfig{});

}  // namespace ZoneConfigAdapter

#endif // CFAOI_ZONE_CONFIG_ADAPTER_H
