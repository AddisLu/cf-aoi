#ifndef CFAOI_ALIGN_ENGINE_H
#define CFAOI_ALIGN_ENGINE_H

/**
 * ============================================================================
 * AlignEngine — OpenCV matchTemplate 對位引擎（取代 MIL MpatFind）
 * ============================================================================
 *
 * 接受 Control 端預裁的搜尋 ROI（search_roi），與 golden pattern 比對，
 * 回傳 ShiftX/Y（相對搜尋 ROI 中心的偏移量，用於套回所有 DetectRoi）。
 *
 * MIL → OpenCV 差距對策：
 *   旋轉：多角度搜尋（angle_range_deg ± step），不用 M_ROTATION
 *   次像素：拋物線擬合（parabolic fit），不用 M_BILINEAR
 *   正規化：TM_CCOEFF_NORMED 部分等效 M_NORMALIZED
 *
 * 對位頻率：每片一次（CF_GRAB_START → CHECK_ALIGN），套回後沿用到下次 LOAD_RECIPE。
 * 失敗策略：score < threshold → ok=false，呼叫方回 ERR 給 Control 決定。
 *
 * ── 不變式 F2（搜尋窗裁切契約）─────────────────────────────────────────────
 *   ShiftX/Y = (Mark 中心) − (search_roi 中心)，其物理意義「Mark 相對 Refer 的偏移」
 *   只有在 **搜尋窗中心 == Refer** 時才成立。故 Control/生產端裁搜尋窗時：
 *     • 窗中心必須 = ReferX/ReferY；
 *     • 近影像邊界時以 **補零（pad）維持 SearchW×SearchH**，不得 clamp 縮小窗
 *       （legacy MIL checkBoundary 會夾小 → 中心偏移 → shift 語意跑掉，本實作不沿用）。
 *   ⚠️ 從實拍幀做「中心裁切並呼叫 run_align」的生產 wiring 待 Step4 取像端；
 *      現 offline 由測試/Control 預裁傳入（CHECK_ALIGN payload）。
 *
 * ── 限制 F3（旋轉）───────────────────────────────────────────────────────
 *   • angle 僅「回報」(AlignResult.angle_deg)，**不套回 ROI**（model A 純平移，與 legacy 同）。
 *   • 旋轉搜尋範圍僅 ±angle_range_deg（預設 ±3°）。面板擺放角度超出此範圍時，
 *     ±range 內最佳 TM_CCOEFF_NORMED 分數會偏低 → score < threshold → ok=false → 回 ERR，
 *     **不會回出一個錯誤的 shift**（即 score_threshold 擋得掉大角度誤匹配；align_verify Stage 3A
 *     15° 案實證）。若產線需大角度，須擴 angle_range_deg 或改機構保證 ±3°。
 * ============================================================================
 */

#include <opencv2/core.hpp>

struct AlignRoiConfig {
    bool    align_enable    = false;
    cv::Mat golden;              // 8-bit 灰階，LOAD_RECIPE 時由 base64 decode 填入
    int     refer_x         = -1;  // 供 log/debug 用（Control 裁 ROI 時已用過）
    int     refer_y         = -1;
    int     search_width    = 500;  // Control 端搜尋 ROI 尺寸（CHECK_ALIGN payload W/H）
    int     search_height   = 500;
    float   score_threshold = 0.6f;
    float   angle_range_deg = 3.0f;  // ±N 度，可調（recipe 或命令列）
    float   angle_step_deg  = 0.5f;
};

struct AlignResult {
    bool   ok        = false;
    double shift_x   = 0.0;   // pixel，正 = 影像偏右（Mark 在 ReferX 右方）
    double shift_y   = 0.0;   // pixel，正 = 影像偏下
    double score     = 0.0;   // 0-1.0，TM_CCOEFF_NORMED 最佳分數
    double angle_deg = 0.0;   // 最佳旋轉角（度）
    std::string error_msg;    // ok=false 時填原因
};

/**
 * 在預裁的搜尋 ROI 中找 golden pattern。
 *
 * @param search_roi  Control 端裁好的搜尋區域（8-bit 灰階 Mat）
 * @param cfg         對位參數（golden 已 decode）
 * @return AlignResult（ok=false 且 error_msg 非空 → 回 ERR 給 Control）
 */
AlignResult run_align(const cv::Mat& search_roi, const AlignRoiConfig& cfg);

#endif // CFAOI_ALIGN_ENGINE_H
