#ifndef CFAOI_RECIPE_SAVING_CONFIG_H
#define CFAOI_RECIPE_SAVING_CONFIG_H

// 存圖與截斷設定（由 Control LOAD_RECIPE recipe_saving 欄位傳入，取代 IP 硬寫死值）。
// 所有欄位預設 -1 = 向下相容（無上限/不截斷）。
struct RecipeSavingConfig {
    int max_save_defect_count = -1;   // -1 = 無上限；>= 0 = 只存前 N 張缺陷小圖
    int save_defect_width     = 100;  // 缺陷小圖寬（px）
    int save_defect_height    = 100;  // 缺陷小圖高（px）
    int max_defect_count_pass = -1;   // -1 = 不截斷；>= 0 = 累計缺陷超過 N 停止後續 zone
};

#endif // CFAOI_RECIPE_SAVING_CONFIG_H
