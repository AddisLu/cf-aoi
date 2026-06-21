#ifndef CFAOI_DEFECT_INFO_H
#define CFAOI_DEFECT_INFO_H

// 缺陷資訊（純 POD，CUDA-free）。從 cuda_kernels.h 抽出，供 CPU 後處理（defect_rules）
// 與單元測試（rules_verify，純 g++ 不需 nvcc）重用；欄位與佈局保持位元一致。
struct DefectInfo {
    int label;
    float center_x;
    float center_y;
    int size;
    float avg_brightness;
    int min_x, max_x, min_y, max_y;
    int is_bright;  // 1 for bright, 0 for dark
};

#endif  // CFAOI_DEFECT_INFO_H
