#ifndef CFAOI_ALIGN_ROI_CONFIG_H
#define CFAOI_ALIGN_ROI_CONFIG_H

// AlignRoiConfig — 對位參數（LOAD_RECIPE 解析後存入 ControlServer session）
//
// 來源：RecipeInfo.xml <M_AlignRoi> + golden_png_base64（Control 傳入）
// 生命週期：每次 LOAD_RECIPE 覆蓋；session 結束時清除。

#include "align_engine.h"   // AlignRoiConfig 定義在此

// AlignRoiConfig 已在 align_engine.h 完整定義，此 header 僅做轉發引用。

#endif // CFAOI_ALIGN_ROI_CONFIG_H
