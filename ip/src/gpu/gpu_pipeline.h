#ifndef CFAOI_GPU_PIPELINE_H
#define CFAOI_GPU_PIPELINE_H

/**
 * ============================================================================
 * GpuPipeline — 遷移自 Reference/gpu_algo/src/batch_detector.cpp
 * ============================================================================
 *
 * 變更：移除 FileReceiver / SocketReceiver / Rivermax 依賴，改為純函式入口。
 * 保留：GPUMemoryManager 記憶體管理、8-Way kernel、CCL、BlobStats、AI filter
 *       等全部 GPU 邏輯（一字不改 kernel）。
 *
 * 對外只暴露一個函式：
 *   DetectionResult process_frame(const uint8_t* img, int w, int h,
 *                                 const ZoneConfig& cfg)
 *
 * 記憶體策略：建構時偵測 cudaDeviceProp.integrated
 *   - integrated（ARM GB10）→ zero-copy mapped
 *   - discrete  （x86 RTX）  → cudaMalloc + async
 * ============================================================================
 */

#include <cstdint>
#include <string>
#include <vector>

#include "cuda_kernels.h"            // ✅ 直接複製來的（DefectInfo / KernelParams）
#include "config/zone_config_adapter.h"

// 對外結果結構（取代 batch_detector 內的 DetectionResult）
struct DetectionResult {
    std::string panel_id;
    int num_defects = 0;
    int num_bright  = 0;
    int num_dark    = 0;
    bool pass = true;                 // num_defects == 0
    double process_time_ms = 0.0;     // GPU event 計時
    int image_width = 0;
    int image_height = 0;
    std::vector<DefectInfo> defects;
};

// 前置宣告（PIMPL，避免在 header 洩漏 CUDA / cublas 型別）
class GpuPipelineImpl;

class GpuPipeline {
public:
    // ai_model_dir：Tensor Core 分類器模型目錄；找不到則停用 AI 過濾。
    explicit GpuPipeline(const std::string& ai_model_dir = "models/gpu_model");
    ~GpuPipeline();

    GpuPipeline(const GpuPipeline&) = delete;
    GpuPipeline& operator=(const GpuPipeline&) = delete;

    // 核心：處理一張已在 CPU 記憶體的 8-bit grayscale 影像。
    // img 指向 w*h bytes（row-major, Mono8）。
    DetectionResult process_frame(const uint8_t* img, int w, int h,
                                  const ZoneConfig& cfg);

    // AI 分類過濾的執行期開關。模型仍會載入（保留架構），但 active=false 時
    // pipeline 不做推論、不過濾，缺陷一律輸出待人工複核。預設停用（訓練資料不足）。
    void set_ai_active(bool active);

    bool ai_enabled() const;   // 有效狀態 = 模型已載入 && active
    bool ai_model_loaded() const;
    bool is_zero_copy() const;

private:
    GpuPipelineImpl* impl_;
};

#endif // CFAOI_GPU_PIPELINE_H
