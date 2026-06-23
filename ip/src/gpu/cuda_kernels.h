#ifndef CUDA_KERNELS_H
#define CUDA_KERNELS_H

#include <cuda_runtime.h>
#include <cstdint>

#include "defect_info.h"   // struct DefectInfo（抽成 CUDA-free header，位元不變；供 CPU 後處理/單元測試重用）

// Structure for kernel parameters
struct KernelParams {
    int width;
    int height;
    int pitch_x;
    int pitch_y;
    int search_range_x;
    int search_range_y;
    float BTH;
    float DTH;
    // SUB(灰階差投票)模式用：DIV 模式忽略。BTH/DTH 在 SUB 模式為灰階差(如 +17/-16)、DIV 為比例(如 1.2/0.7)。
    int pitch_times = 1;     // legacy PitchTimes：每方向比較幾個 pitch 倍數（8 方向 × pitch_times = 投票路數）
    int choose_amount = 1;   // legacy ChooseAmount：≥幾路超 ThB/ThD 才判缺陷
};

// CUDA error checking macro
#define CUDA_CHECK(call) \
    do { \
        cudaError_t err = call; \
        if (err != cudaSuccess) { \
            fprintf(stderr, "CUDA error at %s:%d: %s\n", __FILE__, __LINE__, \
                    cudaGetErrorString(err)); \
            exit(EXIT_FAILURE); \
        } \
    } while(0)

#ifdef __cplusplus
extern "C" {
#endif

// ============================================================================
// Ultra-Fast Kernels (<5ms total on GB10, <12ms on RTX 2080)
// ============================================================================

// Fast 8-way comparison with optional lightweight local search
// search_range: 0=no search, 1=±1 pixel, 2=±2 pixels (for camera skew tolerance)
void launchFast8WayKernel(
    const uint8_t* d_input,
    uint8_t* d_binary,
    const KernelParams& params,
    dim3 blockDim,
    cudaStream_t stream = 0,
    int search_range = 1  // Default ±1 pixel search for skew tolerance
);

// SUB(灰階差)8-Way-Star 投票 kernel — 忠實移植 legacy Algo_8WAY_STAR_SUB_8bits。
// 每像素 16 路(8方向×pitch_times)逐路灰階差，每路 3×3 SAD 局部最佳匹配；
// ≥choose_amount 路 diff>=BTH→亮(255)、<=DTH→暗(128)。輸出與 DIV kernel 同(下游相容)。
void launchSubVotingKernel(
    const uint8_t* d_input,
    uint8_t* d_binary,
    const KernelParams& params,
    dim3 blockDim,
    cudaStream_t stream = 0
);

// Fast CCL kernel
void launchFastCCLKernel(
    const uint8_t* d_binary,
    int* d_labels,
    int width,
    int height,
    dim3 blockDim,
    cudaStream_t stream = 0
);

// Fast blob analysis
void launchFastBlobAnalysis(
    const uint8_t* d_input,
    const uint8_t* d_binary,
    const int* d_labels,
    DefectInfo* d_defects,
    int* d_defect_count,
    int width,
    int height,
    int max_defects,
    cudaStream_t stream = 0
);

// ============================================================================
// Multi-Scale Detection (for large defects > 2 pitch)
// ============================================================================

// Downsample image by 2x using area averaging
void launchDownsample2x(
    const uint8_t* d_input,
    uint8_t* d_output,
    int src_width, int src_height,
    int dst_width, int dst_height,
    cudaStream_t stream = 0
);

// Downsample image by 4x using area averaging
void launchDownsample4x(
    const uint8_t* d_input,
    uint8_t* d_output,
    int src_width, int src_height,
    int dst_width, int dst_height,
    cudaStream_t stream = 0
);

// Upscale binary mask by 2x and merge into full-res binary (OR operation)
void launchUpscaleBinaryMask2x(
    const uint8_t* d_small_binary,
    uint8_t* d_full_binary,
    int small_width, int small_height,
    int full_width, int full_height,
    cudaStream_t stream = 0
);

// Upscale binary mask by 4x and merge into full-res binary (OR operation)
void launchUpscaleBinaryMask4x(
    const uint8_t* d_small_binary,
    uint8_t* d_full_binary,
    int small_width, int small_height,
    int full_width, int full_height,
    cudaStream_t stream = 0
);

// ============================================================================
// Lens Shading Correction (LSC) - Corrects radial vignetting
// ============================================================================

// LSC parameters for polynomial gain model: gain(r) = 1 + k1*r² + k2*r⁴ + k3*r⁶
struct LensShadingParams {
    float center_x;     // Optical center X (typically width/2)
    float center_y;     // Optical center Y (typically height/2)
    float k1;           // Quadratic coefficient (primary vignette term)
    float k2;           // Quartic coefficient (secondary correction)
    float k3;           // Sextic coefficient (fine-tuning)
    float max_gain;     // Maximum gain clamp (prevent over-correction, typically 1.5-2.0)
    
    // Default constructor with typical values for LCD inspection lens
    LensShadingParams() : center_x(0), center_y(0), k1(0.15f), k2(0.05f), k3(0.0f), max_gain(1.5f) {}
};

// Apply lens shading correction to image (in-place modification)
// Estimated time: ~0.3-0.5ms for 8192x5000 image on RTX 2080 SUPER
void launchLensShadingCorrection(
    uint8_t* d_image,           // Device image buffer (modified in-place)
    int width, int height,
    const LensShadingParams& params,
    dim3 blockDim,
    cudaStream_t stream = 0
);

// Auto-calibrate LSC parameters from image (samples corners vs center)
// Call once during setup or when camera/lens changes
void calibrateLensShadingFromImage(
    const uint8_t* d_image,
    int width, int height,
    LensShadingParams& params,
    cudaStream_t stream = 0
);

// Multi-scale detection helper struct
struct MultiScaleBuffers {
    uint8_t* d_image_2x;     // 1/2 resolution image
    uint8_t* d_image_4x;     // 1/4 resolution image
    uint8_t* d_binary_2x;    // 1/2 resolution binary
    uint8_t* d_binary_4x;    // 1/4 resolution binary
    int width_2x, height_2x;
    int width_4x, height_4x;
    bool allocated;
};

// Allocate/Free multi-scale buffers
void allocateMultiScaleBuffers(MultiScaleBuffers& buffers, int full_width, int full_height);
void freeMultiScaleBuffers(MultiScaleBuffers& buffers);

// Initialize/Free persistent buffers
void initPersistentBuffers(size_t total_pixels);
void freePersistentBuffers();

#ifdef __cplusplus
}
#endif

#endif // CUDA_KERNELS_H
