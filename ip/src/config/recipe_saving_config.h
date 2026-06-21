#ifndef CFAOI_RECIPE_SAVING_CONFIG_H
#define CFAOI_RECIPE_SAVING_CONFIG_H

// 存圖與截斷設定（由 Control LOAD_RECIPE recipe_saving 欄位傳入，取代 IP 硬寫死值）。
// 所有欄位預設 -1 = 向下相容（無上限/不截斷）。
struct RecipeSavingConfig {
    int max_save_defect_count = -1;   // -1 = 無上限；>= 0 = 只存前 N 張缺陷小圖
    int save_defect_width     = 100;  // 缺陷小圖寬（px）
    int save_defect_height    = 100;  // 缺陷小圖高（px）
    int max_defect_count_pass = -1;   // -1 = 不截斷；>= 0 = 累計缺陷超過 N 停止後續 zone

    // #32 邊界略過（影像邊緣 N px 內的缺陷濾除；對齊 legacy BypassEdgeX/Y）。預設 0 = 不濾（向下相容）。
    int bypass_edge_x = 0;
    int bypass_edge_y = 0;

    // #16 Rule 改判（依缺陷小圖均值/長寬比把 NG 改判 OK；NgSize 強制 NG）。預設停用（向下相容）。
    bool   image_rule_enable  = false;
    double mean_low_threshold = 40.0;    // 缺陷 patch 均值 < 此 → 改判 OK（MeanOK）
    double hdivw_threshold    = 4.0;     // Height/Width > 此 → 改判 OK（HwRatioOK）
    double ng_size_threshold  = 4096.0;  // size > 此 → 強制 NG（不被上述 OK 規則改判）
};

#endif // CFAOI_RECIPE_SAVING_CONFIG_H
