/**
 * Tensor Core Accelerated Defect Classifier
 * ==========================================
 * ✅ 自 Reference/Demo/include/tensor_core_classifier.h 直接複製（對應 ai_kernels.cu，kernel 禁改）。
 *
 * 【用途】對 GPU 偵測出的每顆缺陷做二分類（真缺陷 / 誤報），用來**過濾**缺陷清單：
 *   process_frame Step 7 呼叫 classifyAndFilterGPU → 只輸出被判為真缺陷者。
 *   推論鏈：每缺陷裁 32×32 patch → GPU 抽 24 維特徵 → Random Forest（主，100 樹）
 *           / MLP 24→64→32→2（備援，cuBLAS Tensor Core）→ 過濾。
 *
 * 【⚠️ 現況：AI 預設停用（不變式 13）】訓練資料不足，誤判風險高於效益，故：
 *   - 模型**仍會載入**（`GpuPipeline` ctor 呼叫 initializeFromSklearn，保留架構與載入路徑），
 *     但 `set_ai_active(false)` → `ai_active=false` → **本類別的推論方法完全不被呼叫**。
 *   - 缺陷因此**一顆都不過濾、全數輸出**，`AiType` 一律標 **"待人工複核"**
 *     （result_saver.cpp::to_legacy），由人到 DefectSort 頁手動標 TrueDefect/Particle。
 *   - 那些人工標註（classification.json + 子夾）即未來重訓的標註資料。
 *   - `--use-ai` 才會啟用；啟用後 `AiType` 才會變成 "NoSet" 等實際分類結果。
 *   → 改動本類別前先確認：目前生產路徑**沒有跑到這裡**，效能/正確性問題多半不在此檔。
 *
 * Uses cuBLAS with Tensor Core acceleration for AI inference.
 * Pre-loads model weights to GPU memory for minimal latency.
 *
 * Supported architectures:
 * - sm_75 (Turing, RTX 2080): FP16 Tensor Cores
 * - sm_80+ (Ampere): TF32 Tensor Cores
 * - sm_87 (Jetson Orin): TF32 Tensor Cores
 * - sm_120/121 (Blackwell, GB10): TF32 Tensor Cores
 *
 * Math mode is auto-selected based on GPU compute capability.
 * （math mode 差異只影響數值精度/速度，不影響上述「預設不推論」的結論。）
 */

#ifndef TENSOR_CORE_CLASSIFIER_H
#define TENSOR_CORE_CLASSIFIER_H

#include <cuda_runtime.h>
#include <cublas_v2.h>
#include <cuda_fp16.h>
#include <vector>
#include <string>
#include <fstream>
#include <iostream>
#include "cuda_kernels.h"  // For DefectInfo struct

// Number of features per defect patch
#define NUM_FEATURES 24

// Simple 2-layer MLP architecture: 24 -> 64 -> 32 -> 2
#define HIDDEN1_SIZE 64
#define HIDDEN2_SIZE 32
#define OUTPUT_SIZE 2

#ifndef CUDA_CHECK
#define CUDA_CHECK(call) \
    do { \
        cudaError_t err = call; \
        if (err != cudaSuccess) { \
            std::cerr << "CUDA Error: " << cudaGetErrorString(err) << " at " << __FILE__ << ":" << __LINE__ << std::endl; \
        } \
    } while(0)
#endif

#define CUBLAS_CHECK(call) \
    do { \
        cublasStatus_t status = call; \
        if (status != CUBLAS_STATUS_SUCCESS) { \
            std::cerr << "cuBLAS Error: " << status << " at " << __FILE__ << ":" << __LINE__ << std::endl; \
        } \
    } while(0)

/**
 * GPU Feature Extractor Kernel
 * Extracts 24 features from image patches on GPU
 */
__global__ void extractFeaturesKernel(
    const uint8_t* __restrict__ image,
    const float* __restrict__ defect_coords,  // [N, 3]: x, y, size
    float* __restrict__ features,              // [N, 24]
    int num_defects,
    int img_width,
    int img_height,
    int patch_size
);

/**
 * ReLU Activation Kernel
 */
__global__ void reluKernel(float* data, int size);

/**
 * Softmax Kernel for final classification
 */
__global__ void softmaxKernel(float* data, int batch_size, int num_classes);

/**
 * Tensor Core Classifier Class
 * Pre-loads weights to GPU and performs inference using cuBLAS
 */
class TensorCoreClassifier {
private:
    cublasHandle_t cublas_handle;
    cudaStream_t stream;
    
    // Model weights on GPU (FP32 for broad compatibility)
    float* d_W1;  // [NUM_FEATURES, HIDDEN1_SIZE]
    float* d_b1;  // [HIDDEN1_SIZE]
    float* d_W2;  // [HIDDEN1_SIZE, HIDDEN2_SIZE]
    float* d_b2;  // [HIDDEN2_SIZE]
    float* d_W3;  // [HIDDEN2_SIZE, OUTPUT_SIZE]
    float* d_b3;  // [OUTPUT_SIZE]
    
    // Random Forest model on GPU
    float* d_rf_thresholds;
    int* d_rf_features;
    int* d_rf_left;
    int* d_rf_right;
    float* d_rf_values;
    int* d_rf_offsets;
    int rf_num_trees;
    int rf_max_depth;
    int rf_total_nodes;
    
    // Intermediate buffers
    float* d_hidden1;  // [batch, HIDDEN1_SIZE]
    float* d_hidden2;  // [batch, HIDDEN2_SIZE]
    float* d_output;   // [batch, OUTPUT_SIZE]
    float* d_features; // [batch, NUM_FEATURES]
    float* d_defect_coords;  // [batch, 3]
    
    int max_batch_size;
    bool initialized;
    
    // Feature normalization parameters
    float feature_mean[NUM_FEATURES];
    float feature_std[NUM_FEATURES];
    float* d_feature_mean;
    float* d_feature_std;
    
public:
    TensorCoreClassifier(int max_batch = 1024);
    ~TensorCoreClassifier();
    
    /**
     * Initialize model weights (can be loaded from sklearn or trained fresh)
     * For now, initialize with random weights (will be replaced with trained model)
     */
    bool initializeFromSklearn(const std::string& model_path);
    bool initializeRandom();
    
    /**
     * Extract features on GPU and classify
     * @param d_image Device pointer to grayscale image
     * @param defect_coords Host array of [x, y, size] for each defect
     * @param num_defects Number of defects to classify
     * @param img_width Image width
     * @param img_height Image height
     * @param classifications Output: 1 = true defect, 0 = false positive
     */
    void classifyDefects(
        const uint8_t* d_image,
        const std::vector<float>& defect_coords,
        int num_defects,
        int img_width,
        int img_height,
        std::vector<bool>& classifications,
        cudaStream_t ext_stream = 0
    );
    
    /**
     * Full GPU version - classify and filter defects entirely on GPU
     * ★ 這是 IP 生產路徑**唯一**會用到的推論入口（gpu_pipeline.cpp Step 7），
     *   且僅在 `ai_on && ai_active` 皆成立時呼叫——預設 ai_active=false → 不會進來（見檔頭）。
     *   啟用時輸出走 d_filtered_defects/d_filtered_count（下載端改讀 filtered 版本）。
     * @param d_image Device pointer to grayscale image
     * @param d_defects Device pointer to DefectInfo array (input)
     * @param d_defect_count Device pointer to defect count (input)
     * @param d_filtered_defects Device pointer to filtered DefectInfo array (output)
     * @param d_filtered_count Device pointer to filtered count (output)
     * @param max_defects Maximum number of defects
     * @param img_width Image width
     * @param img_height Image height
     * @param ext_stream CUDA stream to use
     */
    void classifyAndFilterGPU(
        const uint8_t* d_image,
        const DefectInfo* d_defects,
        const int* d_defect_count,
        DefectInfo* d_filtered_defects,
        int* d_filtered_count,
        int max_defects,
        int img_width,
        int img_height,
        cudaStream_t ext_stream = 0
    );
    
    /**
     * Async version - returns immediately, results available after sync
     */
    void classifyDefectsAsync(
        const uint8_t* d_image,
        const float* d_defect_coords,
        int num_defects,
        int img_width,
        int img_height,
        cudaStream_t ext_stream
    );
    
    /**
     * Get classification results after async call
     */
    void getResults(std::vector<bool>& classifications, int num_defects);
    
    void setStream(cudaStream_t s) { stream = s; }
    bool isInitialized() const { return initialized; }
};

/**
 * Simplified GPU-accelerated classifier using Random Forest decision rules
 * Converted from sklearn to CUDA kernels
 */
class GPURandomForestClassifier {
private:
    cudaStream_t stream;
    
    // Decision tree structure on GPU
    // Using flattened array representation
    float* d_thresholds;      // Split thresholds
    int* d_feature_indices;   // Feature index for each node
    int* d_left_children;     // Left child index (-1 for leaf)
    int* d_right_children;    // Right child index (-1 for leaf)
    float* d_leaf_values;     // Prediction values for leaves
    int* d_tree_offsets;      // Offset for each tree in the forest
    
    int num_trees;
    int max_nodes_per_tree;
    int total_nodes;
    
    // Intermediate buffers
    float* d_features;
    float* d_predictions;
    
    int max_batch_size;
    bool initialized;
    
    // Feature normalization (from sklearn scaler)
    float* d_scaler_mean;
    float* d_scaler_scale;
    
public:
    GPURandomForestClassifier(int max_batch = 1024);
    ~GPURandomForestClassifier();
    
    /**
     * Load trained Random Forest model from sklearn joblib file
     */
    bool loadFromSklearn(const std::string& model_path);
    
    /**
     * Extract features and classify on GPU
     */
    void classifyDefects(
        const uint8_t* d_image,
        const float* d_defect_coords,
        int num_defects,
        int img_width,
        int img_height,
        std::vector<bool>& classifications,
        cudaStream_t ext_stream = 0
    );
    
    bool isInitialized() const { return initialized; }
};

#endif // TENSOR_CORE_CLASSIFIER_H
