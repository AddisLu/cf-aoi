/**
 * Tensor Core Accelerated Defect Classifier - Implementation
 * ===========================================================
 * 
 * GPU-accelerated AI inference for defect classification using:
 * - cuBLAS with Tensor Core math mode
 * - Custom CUDA kernels for feature extraction
 * - Random Forest inference on GPU (global memory)
 * - Async execution for pipeline overlap
 */

#include "tensor_core_classifier.h"
#include "rf_model_config.h"  // RF model configuration
#include <cmath>
#include <algorithm>
#include <cstring>

// ============================================================================
// GPU Feature Extraction Kernel
// Extracts 24 features from 32x32 patches around each defect
// ============================================================================

/**
 * Extract coordinates from DefectInfo array for feature extraction
 * Runs entirely on GPU - no CPU involvement
 */
__global__ void extractCoordsFromDefectsKernel(
    const DefectInfo* __restrict__ defects,
    float* __restrict__ coords,  // [N, 3]: x, y, size
    int num_defects
) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= num_defects) return;
    
    coords[idx * 3 + 0] = defects[idx].center_x;
    coords[idx * 3 + 1] = defects[idx].center_y;
    coords[idx * 3 + 2] = (float)defects[idx].size;
}

/**
 * Filter defects based on AI classification results
 * Uses atomic operations to build filtered output
 */
__global__ void filterDefectsKernel(
    const DefectInfo* __restrict__ input_defects,
    const float* __restrict__ predictions,  // AI output probabilities
    DefectInfo* __restrict__ output_defects,
    int* __restrict__ output_count,
    int num_defects,
    float threshold
) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= num_defects) return;
    
    // Check if this defect is classified as true defect (probability > threshold)
    if (predictions[idx] > threshold) {
        // Atomically get output index and increment counter
        int out_idx = atomicAdd(output_count, 1);
        output_defects[out_idx] = input_defects[idx];
    }
}

__device__ float atomicMinFloat(float* addr, float value) {
    int* addr_as_int = (int*)addr;
    int old = *addr_as_int, assumed;
    do {
        assumed = old;
        old = atomicCAS(addr_as_int, assumed, 
            __float_as_int(fminf(value, __int_as_float(assumed))));
    } while (assumed != old);
    return __int_as_float(old);
}

__device__ float atomicMaxFloat(float* addr, float value) {
    int* addr_as_int = (int*)addr;
    int old = *addr_as_int, assumed;
    do {
        assumed = old;
        old = atomicCAS(addr_as_int, assumed,
            __float_as_int(fmaxf(value, __int_as_float(assumed))));
    } while (assumed != old);
    return __int_as_float(old);
}

/**
 * Extract features for a single defect patch
 * Each thread block processes one defect
 */
__global__ void extractFeaturesKernel(
    const uint8_t* __restrict__ image,
    const float* __restrict__ defect_coords,  // [N, 3]: x, y, size
    float* __restrict__ features,              // [N, 24]
    int num_defects,
    int img_width,
    int img_height,
    int patch_size
) {
    int defect_idx = blockIdx.x;
    if (defect_idx >= num_defects) return;
    
    int tid = threadIdx.x;
    int half_patch = patch_size / 2;
    
    // Get defect center
    float cx = defect_coords[defect_idx * 3 + 0];
    float cy = defect_coords[defect_idx * 3 + 1];
    float defect_size = defect_coords[defect_idx * 3 + 2];
    
    int px = (int)cx;
    int py = (int)cy;
    
    // Compute patch bounds with clipping
    int x1 = max(0, px - half_patch);
    int y1 = max(0, py - half_patch);
    int x2 = min(img_width, px + half_patch);
    int y2 = min(img_height, py + half_patch);
    
    int patch_width = x2 - x1;
    int patch_height = y2 - y1;
    int patch_pixels = patch_width * patch_height;
    
    // Shared memory for reduction
    __shared__ float s_sum;
    __shared__ float s_sum_sq;
    __shared__ float s_min;
    __shared__ float s_max;
    __shared__ int s_hist[8];
    __shared__ float s_sobel_x_sum;
    __shared__ float s_sobel_y_sum;
    __shared__ float s_sobel_x_sq;
    __shared__ float s_sobel_y_sq;
    __shared__ float s_center_sum;
    __shared__ float s_center_sq;
    __shared__ int s_center_count;
    
    // Initialize shared memory
    if (tid == 0) {
        s_sum = 0.0f;
        s_sum_sq = 0.0f;
        s_min = 255.0f;
        s_max = 0.0f;
        s_sobel_x_sum = 0.0f;
        s_sobel_y_sum = 0.0f;
        s_sobel_x_sq = 0.0f;
        s_sobel_y_sq = 0.0f;
        s_center_sum = 0.0f;
        s_center_sq = 0.0f;
        s_center_count = 0;
        for (int i = 0; i < 8; i++) s_hist[i] = 0;
    }
    __syncthreads();
    
    // Each thread processes multiple pixels
    float local_sum = 0.0f;
    float local_sum_sq = 0.0f;
    float local_min = 255.0f;
    float local_max = 0.0f;
    int local_hist[8] = {0};
    float local_sobel_x = 0.0f;
    float local_sobel_y = 0.0f;
    float local_sobel_x_sq = 0.0f;
    float local_sobel_y_sq = 0.0f;
    float local_center_sum = 0.0f;
    float local_center_sq = 0.0f;
    int local_center_count = 0;
    
    // Process pixels assigned to this thread
    for (int i = tid; i < patch_pixels; i += blockDim.x) {
        int lx = i % patch_width;
        int ly = i / patch_width;
        int gx = x1 + lx;
        int gy = y1 + ly;
        
        float val = (float)image[gy * img_width + gx];
        
        // Basic stats
        local_sum += val;
        local_sum_sq += val * val;
        local_min = fminf(local_min, val);
        local_max = fmaxf(local_max, val);
        
        // Histogram bin
        int bin = min(7, (int)(val / 32.0f));
        local_hist[bin]++;
        
        // Sobel edge detection (simplified)
        if (lx > 0 && lx < patch_width - 1 && ly > 0 && ly < patch_height - 1) {
            float v00 = image[(gy-1) * img_width + (gx-1)];
            float v01 = image[(gy-1) * img_width + gx];
            float v02 = image[(gy-1) * img_width + (gx+1)];
            float v10 = image[gy * img_width + (gx-1)];
            float v12 = image[gy * img_width + (gx+1)];
            float v20 = image[(gy+1) * img_width + (gx-1)];
            float v21 = image[(gy+1) * img_width + gx];
            float v22 = image[(gy+1) * img_width + (gx+1)];
            
            float gx_val = -v00 + v02 - 2*v10 + 2*v12 - v20 + v22;
            float gy_val = -v00 - 2*v01 - v02 + v20 + 2*v21 + v22;
            
            local_sobel_x += fabsf(gx_val);
            local_sobel_y += fabsf(gy_val);
            local_sobel_x_sq += gx_val * gx_val;
            local_sobel_y_sq += gy_val * gy_val;
        }
        
        // Center region (inner half)
        int center_x1 = patch_width / 4;
        int center_x2 = 3 * patch_width / 4;
        int center_y1 = patch_height / 4;
        int center_y2 = 3 * patch_height / 4;
        
        if (lx >= center_x1 && lx < center_x2 && ly >= center_y1 && ly < center_y2) {
            local_center_sum += val;
            local_center_sq += val * val;
            local_center_count++;
        }
    }
    
    // Reduce across threads
    atomicAdd(&s_sum, local_sum);
    atomicAdd(&s_sum_sq, local_sum_sq);
    atomicMinFloat(&s_min, local_min);
    atomicMaxFloat(&s_max, local_max);
    atomicAdd(&s_sobel_x_sum, local_sobel_x);
    atomicAdd(&s_sobel_y_sum, local_sobel_y);
    atomicAdd(&s_sobel_x_sq, local_sobel_x_sq);
    atomicAdd(&s_sobel_y_sq, local_sobel_y_sq);
    atomicAdd(&s_center_sum, local_center_sum);
    atomicAdd(&s_center_sq, local_center_sq);
    atomicAdd(&s_center_count, local_center_count);
    
    for (int i = 0; i < 8; i++) {
        atomicAdd(&s_hist[i], local_hist[i]);
    }
    
    __syncthreads();
    
    // Thread 0 computes final features
    if (tid == 0) {
        float* out = &features[defect_idx * NUM_FEATURES];
        float n = (float)patch_pixels;
        
        // Features 0-3: Basic stats (mean, std, min, max)
        float mean = s_sum / n;
        float variance = (s_sum_sq / n) - (mean * mean);
        float std_dev = sqrtf(fmaxf(0.0f, variance));
        
        out[0] = mean;
        out[1] = std_dev;
        out[2] = s_min;
        out[3] = s_max;
        
        // Features 4-11: Histogram (8 bins, normalized)
        float hist_sum = (float)patch_pixels;
        for (int i = 0; i < 8; i++) {
            out[4 + i] = (float)s_hist[i] / hist_sum;
        }
        
        // Features 12-15: Edge features (sobel mean x, mean y, std x, std y)
        int edge_count = max(1, (patch_width - 2) * (patch_height - 2));
        float edge_n = (float)edge_count;
        out[12] = s_sobel_x_sum / edge_n;
        out[13] = s_sobel_y_sum / edge_n;
        out[14] = sqrtf(fmaxf(0.0f, s_sobel_x_sq / edge_n));
        out[15] = sqrtf(fmaxf(0.0f, s_sobel_y_sq / edge_n));
        
        // Features 16-17: Center region stats
        if (s_center_count > 0) {
            float center_n = (float)s_center_count;
            out[16] = s_center_sum / center_n;
            float center_var = (s_center_sq / center_n) - (out[16] * out[16]);
            out[17] = sqrtf(fmaxf(0.0f, center_var));
        } else {
            out[16] = 0.0f;
            out[17] = 0.0f;
        }
        
        // Features 18-19: Area and defect size
        out[18] = (float)(patch_width * patch_height);
        out[19] = defect_size;
        
        // Features 20-23: Texture (approx percentiles and variance)
        // Simplified: use histogram to estimate percentiles
        int cumsum = 0;
        float p25 = 0, p50 = 0, p75 = 0;
        for (int i = 0; i < 8; i++) {
            int prev_cumsum = cumsum;
            cumsum += s_hist[i];
            float bin_center = (i + 0.5f) * 32.0f;
            
            if (prev_cumsum < patch_pixels / 4 && cumsum >= patch_pixels / 4) {
                p25 = bin_center;
            }
            if (prev_cumsum < patch_pixels / 2 && cumsum >= patch_pixels / 2) {
                p50 = bin_center;
            }
            if (prev_cumsum < 3 * patch_pixels / 4 && cumsum >= 3 * patch_pixels / 4) {
                p75 = bin_center;
            }
        }
        
        out[20] = p25;
        out[21] = p75;
        out[22] = p50;
        out[23] = variance;
    }
}

// ============================================================================
// Neural Network Kernels
// ============================================================================

__global__ void reluKernel(float* data, int size) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < size) {
        data[idx] = fmaxf(0.0f, data[idx]);
    }
}

__global__ void addBiasKernel(float* data, const float* bias, int batch_size, int features) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    int total = batch_size * features;
    if (idx < total) {
        int feat = idx % features;
        data[idx] += bias[feat];
    }
}

__global__ void softmaxKernel(float* data, int batch_size, int num_classes) {
    int batch_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (batch_idx >= batch_size) return;
    
    float* row = &data[batch_idx * num_classes];
    
    // Find max for numerical stability
    float max_val = row[0];
    for (int i = 1; i < num_classes; i++) {
        max_val = fmaxf(max_val, row[i]);
    }
    
    // Compute exp and sum
    float sum = 0.0f;
    for (int i = 0; i < num_classes; i++) {
        row[i] = expf(row[i] - max_val);
        sum += row[i];
    }
    
    // Normalize
    for (int i = 0; i < num_classes; i++) {
        row[i] /= sum;
    }
}

__global__ void normalizeFeatureKernel(
    float* features,
    const float* mean,
    const float* scale,
    int batch_size,
    int num_features
) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    int total = batch_size * num_features;
    if (idx < total) {
        int feat = idx % num_features;
        features[idx] = (features[idx] - mean[feat]) / (scale[feat] + 1e-8f);
    }
}

// ============================================================================
// Random Forest Inference Kernel (uses global memory model)
// ============================================================================

__global__ void randomForestInferenceKernel(
    const float* __restrict__ features,       // [N, 24]
    const float* __restrict__ rf_thresholds,  // [total_nodes]
    const int* __restrict__ rf_features,      // [total_nodes]
    const int* __restrict__ rf_left,          // [total_nodes]
    const int* __restrict__ rf_right,         // [total_nodes]
    const float* __restrict__ rf_values,      // [total_nodes]
    const int* __restrict__ rf_offsets,       // [num_trees]
    float* __restrict__ predictions,          // [N]
    int num_samples,
    int num_trees,
    int max_depth
) {
    int sample_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (sample_idx >= num_samples) return;
    
    const float* sample = &features[sample_idx * NUM_FEATURES];
    float sum_prob = 0.0f;
    
    // Evaluate each tree
    for (int tree = 0; tree < num_trees; tree++) {
        int offset = rf_offsets[tree];
        int node = 0;  // Start at root
        
        // Traverse tree
        #pragma unroll 4
        for (int depth = 0; depth < max_depth; depth++) {
            int global_node = offset + node;
            int left = rf_left[global_node];
            
            if (left == -1) {
                // Leaf node - add probability
                sum_prob += rf_values[global_node];
                break;
            }
            
            // Internal node - compare feature to threshold
            int feat_idx = rf_features[global_node];
            float thresh = rf_thresholds[global_node];
            
            if (sample[feat_idx] <= thresh) {
                node = left;
            } else {
                node = rf_right[global_node];
            }
        }
    }
    
    // Average prediction across trees
    predictions[sample_idx] = sum_prob / (float)num_trees;
}

// ============================================================================
// TensorCoreClassifier Implementation
// ============================================================================

TensorCoreClassifier::TensorCoreClassifier(int max_batch)
    : max_batch_size(max_batch), initialized(false), stream(0),
      d_rf_thresholds(nullptr), d_rf_features(nullptr), d_rf_left(nullptr),
      d_rf_right(nullptr), d_rf_values(nullptr), d_rf_offsets(nullptr),
      rf_num_trees(0), rf_max_depth(0), rf_total_nodes(0) {
    
    // Create cuBLAS handle
    CUBLAS_CHECK(cublasCreate(&cublas_handle));
    
    // Select optimal math mode based on GPU compute capability
    // sm_80+: TF32 Tensor Cores available
    // sm_70-79: FP16 Tensor Cores available
    // sm_120+: Blackwell architecture (GB10 etc.) - TF32 supported
    {
        int device;
        cudaGetDevice(&device);
        int major = 0, minor = 0;
        cudaDeviceGetAttribute(&major, cudaDevAttrComputeCapabilityMajor, device);
        cudaDeviceGetAttribute(&minor, cudaDevAttrComputeCapabilityMinor, device);
        int cc = major * 10 + minor;
        
        if (cc >= 80) {
            // sm_80+ (Ampere, Hopper, Blackwell): TF32 Tensor Cores
            CUBLAS_CHECK(cublasSetMathMode(cublas_handle, CUBLAS_TF32_TENSOR_OP_MATH));
            std::cout << "[TensorCore] Using TF32 Tensor Core math mode (sm_" << cc << ")" << std::endl;
        } else if (cc >= 70) {
            // sm_70-79 (Volta, Turing): FP16 Tensor Cores
            CUBLAS_CHECK(cublasSetMathMode(cublas_handle, CUBLAS_TENSOR_OP_MATH));
            std::cout << "[TensorCore] Using FP16 Tensor Core math mode (sm_" << cc << ")" << std::endl;
        } else {
            // Older architectures: default math mode
            CUBLAS_CHECK(cublasSetMathMode(cublas_handle, CUBLAS_DEFAULT_MATH));
            std::cout << "[TensorCore] Using default math mode (sm_" << cc << ")" << std::endl;
        }
    }
    
    // Allocate weight buffers (for MLP backup)
    CUDA_CHECK(cudaMalloc(&d_W1, NUM_FEATURES * HIDDEN1_SIZE * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_b1, HIDDEN1_SIZE * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_W2, HIDDEN1_SIZE * HIDDEN2_SIZE * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_b2, HIDDEN2_SIZE * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_W3, HIDDEN2_SIZE * OUTPUT_SIZE * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_b3, OUTPUT_SIZE * sizeof(float)));
    
    // Allocate intermediate buffers
    CUDA_CHECK(cudaMalloc(&d_features, max_batch_size * NUM_FEATURES * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_hidden1, max_batch_size * HIDDEN1_SIZE * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_hidden2, max_batch_size * HIDDEN2_SIZE * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_output, max_batch_size * sizeof(float)));  // Single float per sample for RF
    CUDA_CHECK(cudaMalloc(&d_defect_coords, max_batch_size * 3 * sizeof(float)));
    
    // Feature normalization
    CUDA_CHECK(cudaMalloc(&d_feature_mean, NUM_FEATURES * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_feature_std, NUM_FEATURES * sizeof(float)));
    
    std::cout << "[TensorCore] Classifier initialized with max batch size: " << max_batch_size << std::endl;
}

TensorCoreClassifier::~TensorCoreClassifier() {
    cublasDestroy(cublas_handle);
    
    cudaFree(d_W1);
    cudaFree(d_b1);
    cudaFree(d_W2);
    cudaFree(d_b2);
    cudaFree(d_W3);
    cudaFree(d_b3);
    cudaFree(d_features);
    cudaFree(d_hidden1);
    cudaFree(d_hidden2);
    cudaFree(d_output);
    cudaFree(d_defect_coords);
    cudaFree(d_feature_mean);
    cudaFree(d_feature_std);
    
    // Free RF model
    if (d_rf_thresholds) cudaFree(d_rf_thresholds);
    if (d_rf_features) cudaFree(d_rf_features);
    if (d_rf_left) cudaFree(d_rf_left);
    if (d_rf_right) cudaFree(d_rf_right);
    if (d_rf_values) cudaFree(d_rf_values);
    if (d_rf_offsets) cudaFree(d_rf_offsets);
}

bool TensorCoreClassifier::initializeRandom() {
    // Initialize with random weights for testing
    std::vector<float> W1(NUM_FEATURES * HIDDEN1_SIZE);
    std::vector<float> b1(HIDDEN1_SIZE, 0.0f);
    std::vector<float> W2(HIDDEN1_SIZE * HIDDEN2_SIZE);
    std::vector<float> b2(HIDDEN2_SIZE, 0.0f);
    std::vector<float> W3(HIDDEN2_SIZE * OUTPUT_SIZE);
    std::vector<float> b3(OUTPUT_SIZE, 0.0f);
    
    // Xavier initialization
    float scale1 = sqrtf(2.0f / (NUM_FEATURES + HIDDEN1_SIZE));
    float scale2 = sqrtf(2.0f / (HIDDEN1_SIZE + HIDDEN2_SIZE));
    float scale3 = sqrtf(2.0f / (HIDDEN2_SIZE + OUTPUT_SIZE));
    
    for (auto& w : W1) w = ((rand() / (float)RAND_MAX) - 0.5f) * 2 * scale1;
    for (auto& w : W2) w = ((rand() / (float)RAND_MAX) - 0.5f) * 2 * scale2;
    for (auto& w : W3) w = ((rand() / (float)RAND_MAX) - 0.5f) * 2 * scale3;
    
    CUDA_CHECK(cudaMemcpy(d_W1, W1.data(), W1.size() * sizeof(float), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_b1, b1.data(), b1.size() * sizeof(float), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_W2, W2.data(), W2.size() * sizeof(float), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_b2, b2.data(), b2.size() * sizeof(float), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_W3, W3.data(), W3.size() * sizeof(float), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_b3, b3.data(), b3.size() * sizeof(float), cudaMemcpyHostToDevice));
    
    // Default normalization (identity)
    std::vector<float> mean(NUM_FEATURES, 0.0f);
    std::vector<float> std_val(NUM_FEATURES, 1.0f);
    CUDA_CHECK(cudaMemcpy(d_feature_mean, mean.data(), NUM_FEATURES * sizeof(float), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_feature_std, std_val.data(), NUM_FEATURES * sizeof(float), cudaMemcpyHostToDevice));
    
    initialized = true;
    std::cout << "[TensorCore] Model initialized with random weights" << std::endl;
    return true;
}

bool TensorCoreClassifier::initializeFromSklearn(const std::string& model_dir) {
    // Load Random Forest model from binary files
    std::string thresh_path = model_dir + "/rf_thresholds.bin";
    std::string feat_path = model_dir + "/rf_features.bin";
    std::string left_path = model_dir + "/rf_left.bin";
    std::string right_path = model_dir + "/rf_right.bin";
    std::string values_path = model_dir + "/rf_values.bin";
    std::string offsets_path = model_dir + "/rf_offsets.bin";
    
    auto loadBinaryFileFloat = [](const std::string& path, std::vector<float>& data) -> bool {
        std::ifstream file(path, std::ios::binary);
        if (!file.is_open()) {
            std::cerr << "[TensorCore] Failed to open: " << path << std::endl;
            return false;
        }
        file.seekg(0, std::ios::end);
        size_t file_size = file.tellg();
        file.seekg(0, std::ios::beg);
        data.resize(file_size / sizeof(float));
        file.read(reinterpret_cast<char*>(data.data()), file_size);
        return true;
    };
    
    auto loadBinaryFileInt = [](const std::string& path, std::vector<int>& data) -> bool {
        std::ifstream file(path, std::ios::binary);
        if (!file.is_open()) {
            std::cerr << "[TensorCore] Failed to open: " << path << std::endl;
            return false;
        }
        file.seekg(0, std::ios::end);
        size_t file_size = file.tellg();
        file.seekg(0, std::ios::beg);
        data.resize(file_size / sizeof(int));
        file.read(reinterpret_cast<char*>(data.data()), file_size);
        return true;
    };
    
    std::vector<float> thresholds, values;
    std::vector<int> features, left, right, offsets;
    
    if (!loadBinaryFileFloat(thresh_path, thresholds)) return false;
    if (!loadBinaryFileInt(feat_path, features)) return false;
    if (!loadBinaryFileInt(left_path, left)) return false;
    if (!loadBinaryFileInt(right_path, right)) return false;
    if (!loadBinaryFileFloat(values_path, values)) return false;
    if (!loadBinaryFileInt(offsets_path, offsets)) return false;
    
    rf_total_nodes = thresholds.size();
    rf_num_trees = offsets.size();
    rf_max_depth = RF_MAX_DEPTH;  // From config header
    
    // Allocate GPU memory for RF model
    CUDA_CHECK(cudaMalloc(&d_rf_thresholds, rf_total_nodes * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_rf_features, rf_total_nodes * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&d_rf_left, rf_total_nodes * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&d_rf_right, rf_total_nodes * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&d_rf_values, rf_total_nodes * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_rf_offsets, rf_num_trees * sizeof(int)));
    
    // Copy to GPU
    CUDA_CHECK(cudaMemcpy(d_rf_thresholds, thresholds.data(), rf_total_nodes * sizeof(float), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_rf_features, features.data(), rf_total_nodes * sizeof(int), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_rf_left, left.data(), rf_total_nodes * sizeof(int), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_rf_right, right.data(), rf_total_nodes * sizeof(int), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_rf_values, values.data(), rf_total_nodes * sizeof(float), cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(d_rf_offsets, offsets.data(), rf_num_trees * sizeof(int), cudaMemcpyHostToDevice));
    
    initialized = true;
    std::cout << "[TensorCore] RF model loaded from: " << model_dir << std::endl;
    std::cout << "[TensorCore] Trees: " << rf_num_trees << ", Nodes: " << rf_total_nodes << ", MaxDepth: " << rf_max_depth << std::endl;
    return true;
}

void TensorCoreClassifier::classifyDefects(
    const uint8_t* d_image,
    const std::vector<float>& defect_coords,
    int num_defects,
    int img_width,
    int img_height,
    std::vector<bool>& classifications,
    cudaStream_t ext_stream
) {
    if (!initialized || num_defects == 0) {
        classifications.clear();
        return;
    }
    
    cudaStream_t use_stream = ext_stream ? ext_stream : stream;
    
    // Copy defect coordinates to GPU
    CUDA_CHECK(cudaMemcpyAsync(d_defect_coords, defect_coords.data(),
                               num_defects * 3 * sizeof(float),
                               cudaMemcpyHostToDevice, use_stream));
    
    // Extract features on GPU
    int threads = 128;
    extractFeaturesKernel<<<num_defects, threads, 0, use_stream>>>(
        d_image, d_defect_coords, d_features,
        num_defects, img_width, img_height, 32
    );
    
    // Use Random Forest inference (global memory model)
    randomForestInferenceKernel<<<(num_defects + 255) / 256, 256, 0, use_stream>>>(
        d_features, d_rf_thresholds, d_rf_features, d_rf_left, d_rf_right,
        d_rf_values, d_rf_offsets, d_output, num_defects, rf_num_trees, rf_max_depth
    );
    
    // Copy results back
    std::vector<float> predictions(num_defects);
    CUDA_CHECK(cudaMemcpyAsync(predictions.data(), d_output,
                               num_defects * sizeof(float),
                               cudaMemcpyDeviceToHost, use_stream));
    CUDA_CHECK(cudaStreamSynchronize(use_stream));
    
    // Convert to classifications (probability > 0.5 = true defect)
    classifications.resize(num_defects);
    for (int i = 0; i < num_defects; i++) {
        classifications[i] = predictions[i] > 0.5f;
    }
}

void TensorCoreClassifier::getResults(std::vector<bool>& classifications, int num_defects) {
    std::vector<float> output(num_defects * OUTPUT_SIZE);
    CUDA_CHECK(cudaMemcpy(output.data(), d_output,
                          num_defects * OUTPUT_SIZE * sizeof(float),
                          cudaMemcpyDeviceToHost));
    
    classifications.resize(num_defects);
    for (int i = 0; i < num_defects; i++) {
        classifications[i] = output[i * OUTPUT_SIZE + 1] > 0.5f;
    }
}

void TensorCoreClassifier::classifyAndFilterGPU(
    const uint8_t* d_image,
    const DefectInfo* d_defects,
    const int* d_defect_count,
    DefectInfo* d_filtered_defects,
    int* d_filtered_count,
    int max_defects,
    int img_width,
    int img_height,
    cudaStream_t ext_stream
) {
    if (!initialized) return;
    
    cudaStream_t use_stream = ext_stream ? ext_stream : stream;
    
    // Get defect count from GPU
    int num_defects;
    CUDA_CHECK(cudaMemcpyAsync(&num_defects, d_defect_count, sizeof(int),
                               cudaMemcpyDeviceToHost, use_stream));
    CUDA_CHECK(cudaStreamSynchronize(use_stream));
    
    if (num_defects == 0) {
        CUDA_CHECK(cudaMemsetAsync(d_filtered_count, 0, sizeof(int), use_stream));
        return;
    }
    
    num_defects = min(num_defects, max_defects);
    
    // Step 1: Extract coordinates from DefectInfo on GPU
    int block_size = 256;
    int grid_size = (num_defects + block_size - 1) / block_size;
    extractCoordsFromDefectsKernel<<<grid_size, block_size, 0, use_stream>>>(
        d_defects, d_defect_coords, num_defects
    );
    
    // Step 2: Extract features on GPU
    int threads = 128;
    extractFeaturesKernel<<<num_defects, threads, 0, use_stream>>>(
        d_image, d_defect_coords, d_features,
        num_defects, img_width, img_height, 32
    );
    
    // Step 3: Random Forest inference on GPU
    randomForestInferenceKernel<<<(num_defects + 255) / 256, 256, 0, use_stream>>>(
        d_features, d_rf_thresholds, d_rf_features, d_rf_left, d_rf_right,
        d_rf_values, d_rf_offsets, d_output, num_defects, rf_num_trees, rf_max_depth
    );
    
    // Step 4: Reset filtered count
    CUDA_CHECK(cudaMemsetAsync(d_filtered_count, 0, sizeof(int), use_stream));
    
    // Step 5: Filter defects on GPU based on AI predictions
    filterDefectsKernel<<<grid_size, block_size, 0, use_stream>>>(
        d_defects, d_output, d_filtered_defects, d_filtered_count,
        num_defects, 0.5f  // threshold
    );
    
    // No sync needed - caller can sync when needed
}

// ============================================================================
// GPURandomForestClassifier - Convert sklearn model to GPU
// ============================================================================

// Kernel to evaluate Random Forest on GPU
__global__ void evaluateRandomForestKernel(
    const float* __restrict__ features,      // [N, 24]
    const float* __restrict__ thresholds,
    const int* __restrict__ feature_indices,
    const int* __restrict__ left_children,
    const int* __restrict__ right_children,
    const float* __restrict__ leaf_values,
    const int* __restrict__ tree_offsets,
    float* __restrict__ predictions,          // [N]
    int num_samples,
    int num_trees,
    int max_depth
) {
    int sample_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (sample_idx >= num_samples) return;
    
    const float* sample_features = &features[sample_idx * NUM_FEATURES];
    float sum = 0.0f;
    
    // Evaluate each tree
    for (int tree = 0; tree < num_trees; tree++) {
        int offset = tree_offsets[tree];
        int node = 0;  // Start at root
        
        // Traverse tree
        for (int depth = 0; depth < max_depth; depth++) {
            int global_node = offset + node;
            int left = left_children[global_node];
            
            if (left == -1) {
                // Leaf node
                sum += leaf_values[global_node];
                break;
            }
            
            // Internal node - compare feature to threshold
            int feat_idx = feature_indices[global_node];
            float thresh = thresholds[global_node];
            
            if (sample_features[feat_idx] <= thresh) {
                node = left;
            } else {
                node = right_children[global_node];
            }
        }
    }
    
    // Average prediction across trees
    predictions[sample_idx] = sum / (float)num_trees;
}

GPURandomForestClassifier::GPURandomForestClassifier(int max_batch)
    : max_batch_size(max_batch), initialized(false), stream(0),
      num_trees(0), max_nodes_per_tree(0), total_nodes(0) {
    
    CUDA_CHECK(cudaMalloc(&d_features, max_batch_size * NUM_FEATURES * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_predictions, max_batch_size * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_scaler_mean, NUM_FEATURES * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_scaler_scale, NUM_FEATURES * sizeof(float)));
    
    d_thresholds = nullptr;
    d_feature_indices = nullptr;
    d_left_children = nullptr;
    d_right_children = nullptr;
    d_leaf_values = nullptr;
    d_tree_offsets = nullptr;
}

GPURandomForestClassifier::~GPURandomForestClassifier() {
    cudaFree(d_features);
    cudaFree(d_predictions);
    cudaFree(d_scaler_mean);
    cudaFree(d_scaler_scale);
    
    if (d_thresholds) cudaFree(d_thresholds);
    if (d_feature_indices) cudaFree(d_feature_indices);
    if (d_left_children) cudaFree(d_left_children);
    if (d_right_children) cudaFree(d_right_children);
    if (d_leaf_values) cudaFree(d_leaf_values);
    if (d_tree_offsets) cudaFree(d_tree_offsets);
}

bool GPURandomForestClassifier::loadFromSklearn(const std::string& model_path) {
    // This would need Python interop or a custom export format
    // For now, initialize with placeholder
    std::cout << "[GPURandomForest] Model loading from sklearn not yet implemented" << std::endl;
    std::cout << "[GPURandomForest] Use TensorCoreClassifier with trained weights instead" << std::endl;
    return false;
}

void GPURandomForestClassifier::classifyDefects(
    const uint8_t* d_image,
    const float* d_defect_coords,
    int num_defects,
    int img_width,
    int img_height,
    std::vector<bool>& classifications,
    cudaStream_t ext_stream
) {
    if (!initialized || num_defects == 0) {
        classifications.clear();
        return;
    }
    
    // Extract features
    int threads = 128;
    extractFeaturesKernel<<<num_defects, threads, 0, ext_stream>>>(
        d_image, d_defect_coords, d_features,
        num_defects, img_width, img_height, 32
    );
    
    // Normalize
    int total = num_defects * NUM_FEATURES;
    normalizeFeatureKernel<<<(total + 255) / 256, 256, 0, ext_stream>>>(
        d_features, d_scaler_mean, d_scaler_scale, num_defects, NUM_FEATURES
    );
    
    // Evaluate forest
    evaluateRandomForestKernel<<<(num_defects + 255) / 256, 256, 0, ext_stream>>>(
        d_features, d_thresholds, d_feature_indices,
        d_left_children, d_right_children, d_leaf_values,
        d_tree_offsets, d_predictions,
        num_defects, num_trees, 20  // max_depth
    );
    
    // Copy results
    std::vector<float> preds(num_defects);
    CUDA_CHECK(cudaMemcpy(preds.data(), d_predictions,
                          num_defects * sizeof(float), cudaMemcpyDeviceToHost));
    
    classifications.resize(num_defects);
    for (int i = 0; i < num_defects; i++) {
        classifications[i] = preds[i] > 0.5f;
    }
}
