// ═══ 📖 手冊對照（docs/html/cf-aoi-training.html，開啟後 ⌘K 搜章節）═══
// [手冊 ch3] 8 方向取樣動畫（判定式逐步算）＋ CCL 收斂動畫（labels 迭代到不動點）
// [手冊 g3] CUDA 語法急救包（<<<>>>/__shared__/atomicMin 白話對照本檔）
// ⚠ __global__ kernel 本體禁改（不變式1）；host wrapper 的收斂迴圈=決定性支柱（不變式7）
// ═══════════════════════════════════════════════════════════════
/**
 * LCD CF Pattern Defect Detection - Ultra-Fast CUDA Kernels
 * ==========================================================
 * 
 * Highly optimized GPU kernels achieving <5ms total processing time
 * on NVIDIA GB10 integrated GPU (sm_121, 48 SMs, 24MB L2).
 * 
 * Key optimizations:
 * - Texture memory for 2D cache locality (leverages 24MB L2)
 * - Fused Flatten+BlobStats kernel (saves one full-image scan)
 * - Sparse blob analysis (65K hash buffer vs 40M pixel buffer)
 * - cudaMemset-based CCL init (DMA engine, skips 99.9% pixels)
 * - Multi-scale detection for large defects (2x/4x downsampling)
 */

#include "cuda_kernels.h"
#include <cstdio>
#include <cfloat>
#include <vector>
#include <cmath>

// Maximum number of unique labels we expect (should be much smaller than 40M)
// This enables sparse buffer optimization: 65K elements vs 40M elements
#define MAX_UNIQUE_LABELS 65536

// Global texture object for 2D image access (optimizes non-coalesced neighbor access)
static cudaTextureObject_t g_tex_input = 0;
static uint8_t* g_tex_bound_ptr = nullptr;

// ============================================================================
// Lens Shading Correction (LSC) Kernel
// Corrects radial brightness falloff caused by lens vignetting
// Uses polynomial model: gain(r) = 1 + k1*r² + k2*r⁴ + k3*r⁶
// ============================================================================

// LSC parameters stored in constant memory for fast broadcast access
__constant__ float c_lsc_center_x;      // Optical center X (typically image_width/2)
__constant__ float c_lsc_center_y;      // Optical center Y (typically image_height/2)
__constant__ float c_lsc_max_radius;    // Maximum radius for normalization
__constant__ float c_lsc_k1;            // Quadratic coefficient
__constant__ float c_lsc_k2;            // Quartic coefficient  
__constant__ float c_lsc_k3;            // Sextic coefficient
__constant__ float c_lsc_max_gain;      // Maximum gain clamp (prevent over-correction)

/**
 * Lens Shading Correction Kernel (In-place)
 * 
 * Corrects radial brightness falloff using polynomial gain model.
 * Designed for LCD CF inspection where lens periphery is darker.
 * 
 * Performance: ~0.3ms for 8192x5000 on RTX 2080 SUPER (memory-bound)
 */
__global__ void kernelLensShadingCorrection(
    uint8_t* __restrict__ d_image,
    const int width, const int height
) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (x >= width || y >= height) return;
    
    // Calculate normalized radius from optical center
    const float dx = (float)x - c_lsc_center_x;
    const float dy = (float)y - c_lsc_center_y;
    const float r = sqrtf(dx * dx + dy * dy) / c_lsc_max_radius;  // Normalized [0, 1+]
    
    // Polynomial gain model: gain = 1 + k1*r² + k2*r⁴ + k3*r⁶
    const float r2 = r * r;
    const float r4 = r2 * r2;
    const float r6 = r4 * r2;
    float gain = 1.0f + c_lsc_k1 * r2 + c_lsc_k2 * r4 + c_lsc_k3 * r6;
    
    // Clamp gain to prevent over-correction
    gain = fminf(gain, c_lsc_max_gain);
    
    // Apply correction
    const int idx = y * width + x;
    const float corrected = (float)d_image[idx] * gain;
    d_image[idx] = (uint8_t)fminf(255.0f, fmaxf(0.0f, corrected));
}

/**
 * Optimized LSC using precomputed gain lookup table (LUT)
 * 
 * For repeated processing of same-size images, precompute radial gain LUT
 * and use texture fetch for faster access.
 * 
 * Performance: ~0.25ms for 8192x5000 (better memory access pattern)
 */
__global__ void kernelLensShadingCorrectionLUT(
    uint8_t* __restrict__ d_image,
    cudaTextureObject_t tex_gain_lut,  // 1D texture: gain[radius_index]
    const int width, const int height,
    const float center_x, const float center_y,
    const float inv_max_radius,        // 1.0 / max_radius
    const int lut_size
) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (x >= width || y >= height) return;
    
    // Calculate radius and map to LUT index
    const float dx = (float)x - center_x;
    const float dy = (float)y - center_y;
    const float r_norm = sqrtf(dx * dx + dy * dy) * inv_max_radius;
    
    // Fetch gain from LUT using linear interpolation (texture handles bounds)
    const float lut_coord = r_norm * (float)(lut_size - 1);
    const float gain = tex1D<float>(tex_gain_lut, lut_coord + 0.5f);
    
    // Apply correction
    const int idx = y * width + x;
    const float corrected = (float)d_image[idx] * gain;
    d_image[idx] = (uint8_t)fminf(255.0f, fmaxf(0.0f, corrected));
}

// ============================================================================
// Texture-based 8-way Comparison Kernel (for better cache utilization)
// ============================================================================

__launch_bounds__(1024)  // Max 1024 threads per block
__global__ void kernelFast8WayTexture(
    cudaTextureObject_t tex_input,
    uint8_t* __restrict__ d_binary,
    const int width, const int height,
    const int pitch_x, const int pitch_y,
    const float BTH, const float DTH,
    const int search_range
) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    
    // Boundary check with margin
    const int margin_x = pitch_x * 2 + search_range;
    const int margin_y = pitch_y * 2 + search_range;
    
    if (x >= width || y >= height) return;
    
    if (x < margin_x || x >= width - margin_x || 
        y < margin_y || y >= height - margin_y) {
        d_binary[y * width + x] = 0;
        return;
    }
    
    // Use texture fetch for 2D locality optimization
    const int center_val = tex2D<uint8_t>(tex_input, x, y);
    
    int neighbor_sum;
    
    // Vertical-only local search: check center and ±1 in Y, pick closest to center_val
    // This tolerates line-scan camera skew with minimal texture fetch overhead (24 total)
    #define FIND_BEST_V(bx, by) ({ \
        int _c = tex2D<uint8_t>(tex_input, bx, by); \
        int _v1 = tex2D<uint8_t>(tex_input, bx, by-1); \
        int _v2 = tex2D<uint8_t>(tex_input, bx, by+1); \
        int _d0 = abs(center_val - _c); \
        int _d1 = abs(center_val - _v1); \
        int _d2 = abs(center_val - _v2); \
        (_d1 < _d0) ? ((_d2 < _d1) ? _v2 : _v1) : ((_d2 < _d0) ? _v2 : _c); \
    })
    
    if (search_range == 0) {
        // Direct 8-way neighbor access using texture (hardware-optimized 2D cache)
        neighbor_sum = 
            tex2D<uint8_t>(tex_input, x - pitch_x, y - pitch_y) +
            tex2D<uint8_t>(tex_input, x,          y - pitch_y) +
            tex2D<uint8_t>(tex_input, x + pitch_x, y - pitch_y) +
            tex2D<uint8_t>(tex_input, x - pitch_x, y) +
            tex2D<uint8_t>(tex_input, x + pitch_x, y) +
            tex2D<uint8_t>(tex_input, x - pitch_x, y + pitch_y) +
            tex2D<uint8_t>(tex_input, x,          y + pitch_y) +
            tex2D<uint8_t>(tex_input, x + pitch_x, y + pitch_y);
    } else {
        // With local search - branchless version for better GPU performance
        neighbor_sum = 
            FIND_BEST_V(x - pitch_x, y - pitch_y) +
            FIND_BEST_V(x,          y - pitch_y) +
            FIND_BEST_V(x + pitch_x, y - pitch_y) +
            FIND_BEST_V(x - pitch_x, y) +
            FIND_BEST_V(x + pitch_x, y) +
            FIND_BEST_V(x - pitch_x, y + pitch_y) +
            FIND_BEST_V(x,          y + pitch_y) +
            FIND_BEST_V(x + pitch_x, y + pitch_y);
    }
    
    #undef FIND_BEST_V
    
    // Integer threshold comparison (avoid floating point)
    const int center_check = (center_val << 3) * 1000;
    const int bth_scaled = __float2int_rn(BTH * 1000.0f);
    const int dth_scaled = __float2int_rn(DTH * 1000.0f);
    
    uint8_t result = 0;
    if (center_check > neighbor_sum * bth_scaled) {
        result = 255;
    } else if (center_check < neighbor_sum * dth_scaled) {
        result = 128;
    }
    
    d_binary[y * width + x] = result;
}

// ============================================================================
// Ultra-Fast 8-way Comparison Kernel with Lightweight Local Search
// ============================================================================
// To handle line-scan camera skew, we do a mini search (±1 pixel) for each neighbor
// Strategy: Find the value closest to center (best match) to tolerate alignment errors

__device__ __forceinline__ int findBestNeighborFast(
    const uint8_t* __restrict__ d_input,
    const int center_val,
    const int base_x, const int base_y,
    const int width,
    const int search_range
) {
    int center = d_input[base_y * width + base_x];
    
    if (search_range == 0) {
        return center;
    }
    
    // Vertical-only search - optimized for line-scan camera skew
    int best_val = center;
    int best_diff = abs(center_val - center);
    
    int up   = d_input[(base_y - 1) * width + base_x];
    int down = d_input[(base_y + 1) * width + base_x];
    
    int diff_up = abs(center_val - up);
    int diff_down = abs(center_val - down);
    
    if (diff_up < best_diff) { best_diff = diff_up; best_val = up; }
    if (diff_down < best_diff) { best_val = down; }
    
    return best_val;
}

__global__ void kernelFast8WayComparison(
    const uint8_t* __restrict__ d_input,
    uint8_t* __restrict__ d_binary,
    const int width, const int height,
    const int pitch_x, const int pitch_y,
    const float BTH, const float DTH,
    const int search_range  // 0=no search, 1=±1 pixel, 2=±2 pixels
) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    
    // Boundary check with margin
    const int margin_x = pitch_x * 2 + search_range;
    const int margin_y = pitch_y * 2 + search_range;
    
    if (x >= width || y >= height) return;
    
    if (x < margin_x || x >= width - margin_x || 
        y < margin_y || y >= height - margin_y) {
        d_binary[y * width + x] = 0;
        return;
    }
    
    // Get center pixel value
    const int idx = y * width + x;
    const int center_val = d_input[idx];
    
    int neighbor_sum;
    
    if (search_range == 0) {
        // Direct 8-way neighbor access (no local search for speed)
        neighbor_sum = 
            d_input[(y - pitch_y) * width + (x - pitch_x)] +  // Top-left
            d_input[(y - pitch_y) * width + x] +               // Top
            d_input[(y - pitch_y) * width + (x + pitch_x)] +  // Top-right
            d_input[y * width + (x - pitch_x)] +               // Left
            d_input[y * width + (x + pitch_x)] +               // Right
            d_input[(y + pitch_y) * width + (x - pitch_x)] +  // Bottom-left
            d_input[(y + pitch_y) * width + x] +               // Bottom
            d_input[(y + pitch_y) * width + (x + pitch_x)];   // Bottom-right
    } else {
        // Lightweight local search: find best match for skew tolerance
        neighbor_sum = 
            findBestNeighborFast(d_input, center_val, x - pitch_x, y - pitch_y, width, search_range) +
            findBestNeighborFast(d_input, center_val, x,          y - pitch_y, width, search_range) +
            findBestNeighborFast(d_input, center_val, x + pitch_x, y - pitch_y, width, search_range) +
            findBestNeighborFast(d_input, center_val, x - pitch_x, y,          width, search_range) +
            findBestNeighborFast(d_input, center_val, x + pitch_x, y,          width, search_range) +
            findBestNeighborFast(d_input, center_val, x - pitch_x, y + pitch_y, width, search_range) +
            findBestNeighborFast(d_input, center_val, x,          y + pitch_y, width, search_range) +
            findBestNeighborFast(d_input, center_val, x + pitch_x, y + pitch_y, width, search_range);
    }
    
    // Compare center*8 with neighbor sum
    int center_scaled = center_val << 3;  // center_val * 8
    
    // Use integer comparison to avoid float division
    int bth_scaled = (int)(BTH * 1000.0f);
    int dth_scaled = (int)(DTH * 1000.0f);
    int center_check = center_scaled * 1000;
    int neighbor_bth = neighbor_sum * bth_scaled;
    int neighbor_dth = neighbor_sum * dth_scaled;
    
    uint8_t result = 0;
    if (center_check > neighbor_bth) {
        result = 255;  // Bright defect
    } else if (center_check < neighbor_dth) {
        result = 128;  // Dark defect
    }
    
    d_binary[idx] = result;
}

// ============================================================================
// Shared Memory Optimized Fast Kernel
// ============================================================================

// Shared memory version with lightweight local search support
__device__ __forceinline__ int findBestNeighborSharedFast(
    const uint8_t* s_tile,
    const int center_val,
    const int lx, const int ly,
    const int s_width,
    const int search_range
) {
    int center = s_tile[ly * s_width + lx];
    
    if (search_range == 0) {
        return center;
    }
    
    // Vertical-only search - optimized for line-scan camera skew
    int best_val = center;
    int best_diff = abs(center_val - center);
    
    int up   = s_tile[(ly - 1) * s_width + lx];
    int down = s_tile[(ly + 1) * s_width + lx];
    
    int diff_up = abs(center_val - up);
    int diff_down = abs(center_val - down);
    
    if (diff_up < best_diff) { best_diff = diff_up; best_val = up; }
    if (diff_down < best_diff) { best_val = down; }
    
    return best_val;
}

__global__ void kernelFast8WayShared(
    const uint8_t* __restrict__ d_input,
    uint8_t* __restrict__ d_binary,
    const int width, const int height,
    const int pitch_x, const int pitch_y,
    const float BTH, const float DTH,
    const int search_range
) {
    extern __shared__ uint8_t s_tile[];
    
    const int tx = threadIdx.x;
    const int ty = threadIdx.y;
    const int bx = blockIdx.x * blockDim.x;
    const int by = blockIdx.y * blockDim.y;
    const int x = bx + tx;
    const int y = by + ty;
    
    // Shared memory tile dimensions - need extra halo for search
    const int halo_x = pitch_x + search_range;
    const int halo_y = pitch_y + search_range;
    const int s_width = blockDim.x + 2 * halo_x;
    const int s_height = blockDim.y + 2 * halo_y;
    
    // Pre-compute BTH/DTH as integer multipliers to use FMA-friendly operations
    const int bth_scaled = __float2int_rn(BTH * 1000.0f);
    const int dth_scaled = __float2int_rn(DTH * 1000.0f);
    
    // Cooperative loading - each thread may load multiple elements
    // Use linear indexing for better memory coalescing
    const int total_elements = s_width * s_height;
    const int total_threads = blockDim.x * blockDim.y;
    const int tid = ty * blockDim.x + tx;
    
    for (int i = tid; i < total_elements; i += total_threads) {
        int sy = i / s_width;
        int sx = i % s_width;
        int gx = bx + sx - halo_x;
        int gy = by + sy - halo_y;
        
        // Clamp to image bounds
        gx = max(0, min(gx, width - 1));
        gy = max(0, min(gy, height - 1));
        
        // Use __ldg for read-only L1 cache optimization
        s_tile[i] = __ldg(&d_input[gy * width + gx]);
    }
    
    __syncthreads();
    
    if (x >= width || y >= height) return;
    
    // Boundary check
    const int margin_x = pitch_x * 2 + search_range;
    const int margin_y = pitch_y * 2 + search_range;
    
    if (x < margin_x || x >= width - margin_x || 
        y < margin_y || y >= height - margin_y) {
        d_binary[y * width + x] = 0;
        return;
    }
    
    // Local coordinates in shared memory
    const int lx = tx + halo_x;
    const int ly = ty + halo_y;
    
    const int center_val = s_tile[ly * s_width + lx];
    
    int neighbor_sum;
    
    if (search_range == 0) {
        // 8-way neighbors from shared memory (no search)
        neighbor_sum = 
            s_tile[(ly - pitch_y) * s_width + (lx - pitch_x)] +
            s_tile[(ly - pitch_y) * s_width + lx] +
            s_tile[(ly - pitch_y) * s_width + (lx + pitch_x)] +
            s_tile[ly * s_width + (lx - pitch_x)] +
            s_tile[ly * s_width + (lx + pitch_x)] +
            s_tile[(ly + pitch_y) * s_width + (lx - pitch_x)] +
            s_tile[(ly + pitch_y) * s_width + lx] +
            s_tile[(ly + pitch_y) * s_width + (lx + pitch_x)];
    } else {
        // Lightweight local search in shared memory
        neighbor_sum = 
            findBestNeighborSharedFast(s_tile, center_val, lx - pitch_x, ly - pitch_y, s_width, search_range) +
            findBestNeighborSharedFast(s_tile, center_val, lx,          ly - pitch_y, s_width, search_range) +
            findBestNeighborSharedFast(s_tile, center_val, lx + pitch_x, ly - pitch_y, s_width, search_range) +
            findBestNeighborSharedFast(s_tile, center_val, lx - pitch_x, ly,          s_width, search_range) +
            findBestNeighborSharedFast(s_tile, center_val, lx + pitch_x, ly,          s_width, search_range) +
            findBestNeighborSharedFast(s_tile, center_val, lx - pitch_x, ly + pitch_y, s_width, search_range) +
            findBestNeighborSharedFast(s_tile, center_val, lx,          ly + pitch_y, s_width, search_range) +
            findBestNeighborSharedFast(s_tile, center_val, lx + pitch_x, ly + pitch_y, s_width, search_range);
    }
    
    // Use pre-computed scaled thresholds
    int center_check = (center_val << 3) * 1000;  // center_val * 8 * 1000
    
    uint8_t result = 0;
    if (center_check > neighbor_sum * bth_scaled) {
        result = 255;
    } else if (center_check < neighbor_sum * dth_scaled) {
        result = 128;
    }
    
    d_binary[y * width + x] = result;
}

// ============================================================================
// Ultra-Fast CCL using Block-based Label Propagation
// ============================================================================

// Fused Init + Merge kernel: only processes defect pixels (binary>0)
// Labels buffer is pre-filled with -1 via cudaMemset before this kernel
__global__ void kernelFastCCLInitMerge(
    const uint8_t* __restrict__ d_binary,
    int* __restrict__ d_labels,
    const int width, const int height
) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (x >= width || y >= height) return;
    
    const int idx = y * width + x;
    const uint8_t bval = __ldg(&d_binary[idx]);
    
    // Skip non-defect pixels (labels already = -1 from cudaMemset)
    if (bval == 0) return;
    
    // Initialize label to self
    d_labels[idx] = idx;
}

__global__ void kernelFastCCLMerge(
    const uint8_t* __restrict__ d_binary,
    int* __restrict__ d_labels,
    const int width, const int height,
    int* __restrict__ d_changed
) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (x >= width || y >= height) return;
    
    const int idx = y * width + x;
    if (d_binary[idx] == 0) return;
    
    const uint8_t my_type = d_binary[idx];
    int my_label = d_labels[idx];
    
    // Find root with simple path compression
    int root = my_label;
    while (d_labels[root] != root && d_labels[root] >= 0) {
        root = d_labels[root];
    }
    
    // Path compression: update our label to point directly to root
    if (my_label != root) {
        d_labels[idx] = root;
        my_label = root;
    }
    
    // Check 8 neighbors - unrolled for performance
    #pragma unroll
    for (int dy = -1; dy <= 1; dy++) {
        #pragma unroll
        for (int dx = -1; dx <= 1; dx++) {
            if (dx == 0 && dy == 0) continue;
            
            int nx = x + dx;
            int ny = y + dy;
            
            if (nx >= 0 && nx < width && ny >= 0 && ny < height) {
                int nidx = ny * width + nx;
                if (d_binary[nidx] == my_type) {
                    // Find neighbor's root
                    int n_label = d_labels[nidx];
                    while (d_labels[n_label] != n_label && d_labels[n_label] >= 0) {
                        n_label = d_labels[n_label];
                    }
                    
                    // Union by smaller label
                    if (n_label < my_label) {
                        atomicMin(&d_labels[my_label], n_label);
                        *d_changed = 1;
                    } else if (my_label < n_label) {
                        atomicMin(&d_labels[n_label], my_label);
                        *d_changed = 1;
                    }
                }
            }
        }
    }
}

__global__ void kernelFastCCLFlatten(
    const uint8_t* __restrict__ d_binary,
    int* __restrict__ d_labels,
    const int width, const int height
) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (x >= width || y >= height) return;
    
    const int idx = y * width + x;
    
    // Early exit: check binary first (1-byte read) before label (4-byte read)
    // Skips ~99.9% of pixels that have no defect
    if (d_binary[idx] == 0) return;
    
    int label = d_labels[idx];
    
    if (label >= 0) {
        while (d_labels[label] != label) {
            label = d_labels[label];
        }
        d_labels[idx] = label;
    }
}

// ============================================================================
// Fused Flatten + BlobStats Kernel
// Combines CCL path compression and blob statistics in a single pass
// Saves one full-image scan (~0.5ms for 40M pixels)
// ============================================================================
__global__ void kernelFusedFlattenBlobStats(
    const uint8_t* __restrict__ d_input,
    const uint8_t* __restrict__ d_binary,
    int* __restrict__ d_labels,
    int* __restrict__ d_counts,
    float* __restrict__ d_sum_x,
    float* __restrict__ d_sum_y,
    float* __restrict__ d_sum_brightness,
    int* __restrict__ d_min_x,
    int* __restrict__ d_max_x,
    int* __restrict__ d_min_y,
    int* __restrict__ d_max_y,
    int* __restrict__ d_is_bright,
    int* __restrict__ d_unique_labels,
    int* __restrict__ d_unique_count,
    const int width, const int height
) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (x >= width || y >= height) return;
    
    const int idx = y * width + x;
    
    // Early exit: check binary first (1-byte read)
    // Skips ~99.9% of pixels
    if (d_binary[idx] == 0) return;
    
    // === Flatten phase: path compression ===
    int label = d_labels[idx];
    if (label < 0) return;
    
    // Path compression to root
    while (d_labels[label] != label) {
        label = d_labels[label];
    }
    d_labels[idx] = label;
    
    // === BlobStats phase: accumulate statistics ===
    const int hash_idx = label % MAX_UNIQUE_LABELS;
    
    // First thread for this label records it
    int old_count = atomicAdd(&d_counts[hash_idx], 1);
    if (old_count == 0) {
        int unique_idx = atomicAdd(d_unique_count, 1);
        if (unique_idx < MAX_UNIQUE_LABELS) {
            d_unique_labels[unique_idx] = label;
        }
    }
    
    // Accumulate stats
    atomicAdd(&d_sum_x[hash_idx], (float)x);
    atomicAdd(&d_sum_y[hash_idx], (float)y);
    atomicAdd(&d_sum_brightness[hash_idx], (float)d_input[idx]);
    atomicMin(&d_min_x[hash_idx], x);
    atomicMax(&d_max_x[hash_idx], x);
    atomicMin(&d_min_y[hash_idx], y);
    atomicMax(&d_max_y[hash_idx], y);
    
    if (d_binary[idx] == 255) {
        d_is_bright[hash_idx] = 1;
    }
}

// ============================================================================
// Multi-Scale Detection Kernels (for large defects > 2 pitch)
// Strategy: Downsample image, detect at lower resolution, then upscale results
// ============================================================================

// Downsample kernel using area averaging (2x2 block -> 1 pixel)
__global__ void kernelDownsample2x(
    const uint8_t* __restrict__ d_input,
    uint8_t* __restrict__ d_output,
    const int src_width, const int src_height,
    const int dst_width, const int dst_height
) {
    const int dx = blockIdx.x * blockDim.x + threadIdx.x;
    const int dy = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (dx >= dst_width || dy >= dst_height) return;
    
    const int sx = dx * 2;
    const int sy = dy * 2;
    
    // Area averaging: mean of 2x2 block
    int sum = 0;
    int count = 0;
    
    for (int j = 0; j < 2 && (sy + j) < src_height; j++) {
        for (int i = 0; i < 2 && (sx + i) < src_width; i++) {
            sum += d_input[(sy + j) * src_width + (sx + i)];
            count++;
        }
    }
    
    d_output[dy * dst_width + dx] = (uint8_t)(sum / count);
}

// Downsample kernel using area averaging (4x4 block -> 1 pixel)
__global__ void kernelDownsample4x(
    const uint8_t* __restrict__ d_input,
    uint8_t* __restrict__ d_output,
    const int src_width, const int src_height,
    const int dst_width, const int dst_height
) {
    const int dx = blockIdx.x * blockDim.x + threadIdx.x;
    const int dy = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (dx >= dst_width || dy >= dst_height) return;
    
    const int sx = dx * 4;
    const int sy = dy * 4;
    
    // Area averaging: mean of 4x4 block
    int sum = 0;
    int count = 0;
    
    for (int j = 0; j < 4 && (sy + j) < src_height; j++) {
        for (int i = 0; i < 4 && (sx + i) < src_width; i++) {
            sum += d_input[(sy + j) * src_width + (sx + i)];
            count++;
        }
    }
    
    d_output[dy * dst_width + dx] = (uint8_t)(sum / count);
}

// Upscale binary mask from downsampled detection (nearest neighbor)
// OPTIMIZED: iterate over small image (1/4 pixels), only write when defect found
// This reduces thread count from 40M to 10M with <0.1% actual writes
__global__ void kernelUpscaleBinaryMask2x(
    const uint8_t* __restrict__ d_small_binary,
    uint8_t* __restrict__ d_full_binary,
    const int small_width, const int small_height,
    const int full_width, const int full_height
) {
    const int sx = blockIdx.x * blockDim.x + threadIdx.x;
    const int sy = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (sx >= small_width || sy >= small_height) return;
    
    const uint8_t small_val = d_small_binary[sy * small_width + sx];
    
    // Early exit for non-defect pixels (vast majority)
    if (small_val == 0) return;
    
    // Write to 2x2 block in full-res image
    const int fx = sx * 2;
    const int fy = sy * 2;
    
    #pragma unroll
    for (int dy = 0; dy < 2; dy++) {
        #pragma unroll
        for (int dx = 0; dx < 2; dx++) {
            const int gx = fx + dx;
            const int gy = fy + dy;
            if (gx < full_width && gy < full_height) {
                const int full_idx = gy * full_width + gx;
                if (d_full_binary[full_idx] == 0) {
                    d_full_binary[full_idx] = small_val;
                }
            }
        }
    }
}

// Upscale binary mask from 4x downsampled detection
// OPTIMIZED: iterate over small image (1/16 pixels)
__global__ void kernelUpscaleBinaryMask4x(
    const uint8_t* __restrict__ d_small_binary,
    uint8_t* __restrict__ d_full_binary,
    const int small_width, const int small_height,
    const int full_width, const int full_height
) {
    const int sx = blockIdx.x * blockDim.x + threadIdx.x;
    const int sy = blockIdx.y * blockDim.y + threadIdx.y;
    
    if (sx >= small_width || sy >= small_height) return;
    
    const uint8_t small_val = d_small_binary[sy * small_width + sx];
    if (small_val == 0) return;
    
    const int fx = sx * 4;
    const int fy = sy * 4;
    
    #pragma unroll
    for (int dy = 0; dy < 4; dy++) {
        #pragma unroll
        for (int dx = 0; dx < 4; dx++) {
            const int gx = fx + dx;
            const int gy = fy + dy;
            if (gx < full_width && gy < full_height) {
                const int full_idx = gy * full_width + gx;
                if (d_full_binary[full_idx] == 0) {
                    d_full_binary[full_idx] = small_val;
                }
            }
        }
    }
}

// ============================================================================
// Ultra-Fast Sparse Blob Analysis Kernels
// Key optimization: Use hash-based indexing for O(1) lookup with small buffers
// ============================================================================

// Hash function for label -> buffer index
__device__ __forceinline__ int hashLabel(int label) {
    // Simple modulo hash - works because defect count << MAX_UNIQUE_LABELS
    return label % MAX_UNIQUE_LABELS;
}

// Sparse defect collection - iterate over unique labels only
__global__ void kernelSparseCollectDefects(
    const int* __restrict__ d_unique_labels,
    const int unique_count,
    const int* __restrict__ d_counts,
    const float* __restrict__ d_sum_x,
    const float* __restrict__ d_sum_y,
    const float* __restrict__ d_sum_brightness,
    const int* __restrict__ d_min_x,
    const int* __restrict__ d_max_x,
    const int* __restrict__ d_min_y,
    const int* __restrict__ d_max_y,
    const int* __restrict__ d_is_bright,
    DefectInfo* __restrict__ d_defects,
    int* __restrict__ d_defect_count,
    const int max_defects,
    const int min_blob_size,
    const int max_blob_size,
    const float max_aspect_ratio
) {
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= unique_count) return;
    
    // Get the original label and its hash index
    const int label = d_unique_labels[tid];
    const int hash_idx = label % MAX_UNIQUE_LABELS;
    
    const int count = d_counts[hash_idx];
    if (count < min_blob_size || count > max_blob_size) return;
    if (d_max_x[hash_idx] == -1) return;
    
    const int blob_width = d_max_x[hash_idx] - d_min_x[hash_idx] + 1;
    const int blob_height = d_max_y[hash_idx] - d_min_y[hash_idx] + 1;
    
    const float aspect = (blob_height > 0) ? (float)blob_width / (float)blob_height : 999.0f;
    const int area = blob_width * blob_height;
    const float density = (area > 0) ? (float)count / (float)area : 0.0f;
    
    // Filter elongated sparse blobs
    if ((aspect > max_aspect_ratio && density < 0.3f) || 
        (aspect < (1.0f / max_aspect_ratio) && density < 0.3f) ||
        aspect > 8.0f || aspect < 0.125f) {
        return;
    }
    
    const int def_idx = atomicAdd(d_defect_count, 1);
    if (def_idx >= max_defects) return;
    
    d_defects[def_idx].label = label;  // Use original label
    d_defects[def_idx].size = count;
    d_defects[def_idx].center_x = d_sum_x[hash_idx] / count;
    d_defects[def_idx].center_y = d_sum_y[hash_idx] / count;
    d_defects[def_idx].avg_brightness = d_sum_brightness[hash_idx] / count;
    d_defects[def_idx].min_x = d_min_x[hash_idx];
    d_defects[def_idx].max_x = d_max_x[hash_idx];
    d_defects[def_idx].min_y = d_min_y[hash_idx];
    d_defects[def_idx].max_y = d_max_y[hash_idx];
    d_defects[def_idx].is_bright = d_is_bright[hash_idx];
}

// ============================================================================
// Ultra-Fast Sparse Buffer Manager
// Key insight: Only ~0.1% of pixels are defects, so use sparse approach
// ============================================================================

struct SparseBuffers {
    // For CCL
    int *d_changed;
    
    // Sparse approach: fixed-size buffers for unique labels only
    // Index by label % MAX_UNIQUE_LABELS (hash table approach)
    float *d_sum_x, *d_sum_y, *d_sum_brightness;
    int *d_counts, *d_min_x, *d_max_x, *d_min_y, *d_max_y, *d_is_bright;
    
    // Unique label collection
    int *d_unique_labels;
    int *d_unique_count;
    
    bool initialized;
};

static SparseBuffers g_sparse = {nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, false};

void initPersistentBuffers(size_t total_pixels) {
    (void)total_pixels;  // No longer used - we use fixed sparse size
    
    if (g_sparse.initialized) return;
    
    // Allocate fixed-size sparse buffers (65536 elements = 256KB each)
    CUDA_CHECK(cudaMalloc(&g_sparse.d_sum_x, MAX_UNIQUE_LABELS * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_sum_y, MAX_UNIQUE_LABELS * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_sum_brightness, MAX_UNIQUE_LABELS * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_counts, MAX_UNIQUE_LABELS * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_min_x, MAX_UNIQUE_LABELS * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_max_x, MAX_UNIQUE_LABELS * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_min_y, MAX_UNIQUE_LABELS * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_max_y, MAX_UNIQUE_LABELS * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_is_bright, MAX_UNIQUE_LABELS * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_changed, sizeof(int)));
    
    // Unique label tracking
    CUDA_CHECK(cudaMalloc(&g_sparse.d_unique_labels, MAX_UNIQUE_LABELS * sizeof(int)));
    CUDA_CHECK(cudaMalloc(&g_sparse.d_unique_count, sizeof(int)));
    
    g_sparse.initialized = true;
}

void freePersistentBuffers() {
    if (!g_sparse.initialized) return;
    
    cudaFree(g_sparse.d_sum_x);
    cudaFree(g_sparse.d_sum_y);
    cudaFree(g_sparse.d_sum_brightness);
    cudaFree(g_sparse.d_counts);
    cudaFree(g_sparse.d_min_x);
    cudaFree(g_sparse.d_max_x);
    cudaFree(g_sparse.d_min_y);
    cudaFree(g_sparse.d_max_y);
    cudaFree(g_sparse.d_is_bright);
    cudaFree(g_sparse.d_changed);
    cudaFree(g_sparse.d_unique_labels);
    cudaFree(g_sparse.d_unique_count);
    
    g_sparse.initialized = false;
}

// Helper function to create/bind texture object
static void bindTextureObject(const uint8_t* d_input, int width, int height) {
    // Only recreate if pointer changed
    if (g_tex_bound_ptr == d_input && g_tex_input != 0) {
        return;
    }
    
    // Destroy old texture if exists
    if (g_tex_input != 0) {
        cudaDestroyTextureObject(g_tex_input);
        g_tex_input = 0;
    }
    
    // Create resource descriptor
    cudaResourceDesc resDesc = {};
    resDesc.resType = cudaResourceTypePitch2D;
    resDesc.res.pitch2D.devPtr = const_cast<uint8_t*>(d_input);
    resDesc.res.pitch2D.desc = cudaCreateChannelDesc<uint8_t>();
    resDesc.res.pitch2D.width = width;
    resDesc.res.pitch2D.height = height;
    resDesc.res.pitch2D.pitchInBytes = width * sizeof(uint8_t);
    
    // Create texture descriptor
    cudaTextureDesc texDesc = {};
    texDesc.addressMode[0] = cudaAddressModeClamp;
    texDesc.addressMode[1] = cudaAddressModeClamp;
    texDesc.filterMode = cudaFilterModePoint;
    texDesc.readMode = cudaReadModeElementType;
    texDesc.normalizedCoords = 0;
    
    // Create texture object
    cudaCreateTextureObject(&g_tex_input, &resDesc, &texDesc, nullptr);
    g_tex_bound_ptr = const_cast<uint8_t*>(d_input);
}

// ============================================================================
// Host Interface - Lens Shading Correction
// ============================================================================

void launchLensShadingCorrection(
    uint8_t* d_image,
    int width, int height,
    const LensShadingParams& params,
    dim3 blockDim,
    cudaStream_t stream
) {
    // Copy parameters to constant memory (only once per unique param set)
    static LensShadingParams s_cached_params;
    static bool s_params_initialized = false;
    
    // Check if parameters changed
    bool params_changed = !s_params_initialized ||
        s_cached_params.center_x != params.center_x ||
        s_cached_params.center_y != params.center_y ||
        s_cached_params.k1 != params.k1 ||
        s_cached_params.k2 != params.k2 ||
        s_cached_params.k3 != params.k3 ||
        s_cached_params.max_gain != params.max_gain;
    
    if (params_changed) {
        // Calculate max radius (diagonal distance from center to corner)
        float max_radius = sqrtf(
            fmaxf(params.center_x * params.center_x, (width - params.center_x) * (width - params.center_x)) +
            fmaxf(params.center_y * params.center_y, (height - params.center_y) * (height - params.center_y))
        );
        
        CUDA_CHECK(cudaMemcpyToSymbol(c_lsc_center_x, &params.center_x, sizeof(float)));
        CUDA_CHECK(cudaMemcpyToSymbol(c_lsc_center_y, &params.center_y, sizeof(float)));
        CUDA_CHECK(cudaMemcpyToSymbol(c_lsc_max_radius, &max_radius, sizeof(float)));
        CUDA_CHECK(cudaMemcpyToSymbol(c_lsc_k1, &params.k1, sizeof(float)));
        CUDA_CHECK(cudaMemcpyToSymbol(c_lsc_k2, &params.k2, sizeof(float)));
        CUDA_CHECK(cudaMemcpyToSymbol(c_lsc_k3, &params.k3, sizeof(float)));
        CUDA_CHECK(cudaMemcpyToSymbol(c_lsc_max_gain, &params.max_gain, sizeof(float)));
        
        s_cached_params = params;
        s_params_initialized = true;
    }
    
    dim3 gridDim(
        (width + blockDim.x - 1) / blockDim.x,
        (height + blockDim.y - 1) / blockDim.y
    );
    
    kernelLensShadingCorrection<<<gridDim, blockDim, 0, stream>>>(
        d_image, width, height
    );
    CUDA_CHECK(cudaGetLastError());
}

// Auto-calibrate LSC parameters from image corners
void calibrateLensShadingFromImage(
    const uint8_t* d_image,
    int width, int height,
    LensShadingParams& params,
    cudaStream_t stream
) {
    // Simple calibration: sample brightness at corners vs center
    // This is a CPU-side calibration that should be done once per camera setup
    
    const int sample_size = 100;  // Sample 100x100 region
    const int h_sample_size = sample_size / 2;
    
    // Allocate temporary buffer for sample regions
    std::vector<uint8_t> h_samples(sample_size * sample_size * 5);  // 5 regions
    
    // Define sample regions: center, 4 corners
    struct SampleRegion { int x, y; };
    SampleRegion regions[5] = {
        {width/2 - h_sample_size, height/2 - h_sample_size},      // Center
        {h_sample_size, h_sample_size},                            // Top-left
        {width - sample_size - h_sample_size, h_sample_size},      // Top-right
        {h_sample_size, height - sample_size - h_sample_size},     // Bottom-left
        {width - sample_size - h_sample_size, height - sample_size - h_sample_size}  // Bottom-right
    };
    
    float avg_brightness[5] = {0};
    
    cudaStreamSynchronize(stream);
    
    for (int r = 0; r < 5; r++) {
        uint8_t* h_region = &h_samples[r * sample_size * sample_size];
        
        // Copy region (row by row)
        for (int row = 0; row < sample_size; row++) {
            int src_offset = (regions[r].y + row) * width + regions[r].x;
            CUDA_CHECK(cudaMemcpy(h_region + row * sample_size, 
                                  d_image + src_offset, 
                                  sample_size * sizeof(uint8_t),
                                  cudaMemcpyDeviceToHost));
        }
        
        // Calculate average
        float sum = 0;
        for (int i = 0; i < sample_size * sample_size; i++) {
            sum += h_region[i];
        }
        avg_brightness[r] = sum / (sample_size * sample_size);
    }
    
    // Calculate vignette ratio: corner brightness / center brightness
    float center_brightness = avg_brightness[0];
    float corner_avg = (avg_brightness[1] + avg_brightness[2] + avg_brightness[3] + avg_brightness[4]) / 4.0f;
    
    // Estimate k1 based on brightness falloff
    // At r=1 (corner), gain should be center_brightness / corner_brightness
    float required_gain = center_brightness / fmaxf(corner_avg, 1.0f);
    
    // Simple model: use only k1 (quadratic)
    // gain(1) = 1 + k1 = required_gain
    params.k1 = required_gain - 1.0f;
    params.k2 = 0.0f;  // Higher order terms set to 0 for simplicity
    params.k3 = 0.0f;
    params.max_gain = fminf(required_gain * 1.1f, 2.0f);  // 10% headroom, max 2x
    params.center_x = width / 2.0f;
    params.center_y = height / 2.0f;
    
    printf("[LSC] Auto-calibration results:\n");
    printf("  Center brightness: %.1f\n", center_brightness);
    printf("  Corner avg brightness: %.1f\n", corner_avg);
    printf("  Required gain at corners: %.3f\n", required_gain);
    printf("  k1=%.4f, k2=%.4f, k3=%.4f, max_gain=%.2f\n",
           params.k1, params.k2, params.k3, params.max_gain);
}

// ============================================================================
// Host Interface - Ultra Fast Version
// ============================================================================

// ============================================================================
// SUB(灰階差)8-Way-Star 投票 kernel — 忠實移植 legacy Algo_8WAY_STAR_SUB_8bits
//   出處：Reference/PrjCfAoi/CudaCore/CUDA_Kernel.cu:1044-1564（gather+投票）
//        + GetCompareIndex（CUDA_KernelFunction.cu:204-242，3×3 SAD 局部匹配）。
//   ⚠️ 新增 kernel，不改既有 DIV/AI kernel（§7 不變式 1）。輸出 255/128/0 與 DIV 同。
//   本版實作內部像素(Type-1 全 8-way)；邊界 margin skip（本 recipe BypassEdge=50 ≥ pitch reach
//   52 → 邊界 zone-type 差異落在 bypass 內，見 docs/SUB_pipeline_port_plan.md）。
// ============================================================================

// 中心 3×3 取樣偏移，順序與 legacy RescentData(CUDA_Kernel.cu:1077-1085)一致：
//   (0,0)(0,-1)(+1,-1)(+1,0)(+1,+1)(0,+1)(-1,+1)(-1,0)(-1,-1)
__constant__ int c_sub_rdx[9] = { 0, 0, 1, 1, 1, 0,-1,-1,-1};
__constant__ int c_sub_rdy[9] = { 0,-1,-1, 0, 1, 1, 1, 0,-1};

// 3×3 SAD 局部最佳匹配：在 [-searchX..searchX]×[-searchY..searchY] 找與中心 3×3 最相似之候選，
// 回傳該候選中心像素之 linear index（= legacy GetCompareIndex）。
__device__ __forceinline__ int subGetCompareIndex(
    const uint8_t* __restrict__ d_input, const int width,
    const int cx, const int cy,          // 中心像素
    const int compX, const int compY,    // 目標鄰點（未加搜尋偏移）
    const int searchX, const int searchY)
{
    uint8_t rc[9];
    #pragma unroll
    for (int k = 0; k < 9; ++k)
        rc[k] = d_input[(cy + c_sub_rdy[k]) * width + (cx + c_sub_rdx[k])];

    unsigned int best = 0xFFFFFFFFu;
    int bx = 0, by = 0;
    for (int sy = -searchY; sy <= searchY; ++sy) {
        for (int sx = -searchX; sx <= searchX; ++sx) {
            int sad = 0;
            #pragma unroll
            for (int k = 0; k < 9; ++k) {
                int v = d_input[(compY + sy + c_sub_rdy[k]) * width + (compX + sx + c_sub_rdx[k])];
                sad += abs((int)rc[k] - v);
            }
            if ((unsigned)sad < best) { best = (unsigned)sad; bx = sx; by = sy; }
        }
    }
    return (compY + by) * width + (compX + bx);
}

__global__ void kernelSub8WayStarVoting(
    const uint8_t* __restrict__ d_input,
    uint8_t* __restrict__ d_binary,
    const int width, const int height,
    const int pitch_x, const int pitch_y,
    const int pitch_times, const int choose_amount,
    const float ThB, const float ThD,
    const int search_x, const int search_y)
{
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= width || y >= height) return;

    // 最遠取樣 = pitch*pitch_times（最遠方向）+ search（搜尋窗）+ 1（3×3 鄰域）
    const int margin_x = pitch_x * pitch_times + search_x + 1;
    const int margin_y = pitch_y * pitch_times + search_y + 1;
    const int idx = y * width + x;
    if (x < margin_x || x >= width - margin_x ||
        y < margin_y || y >= height - margin_y) {
        d_binary[idx] = 0;
        return;
    }

    const int center = d_input[idx];

    // 8 方向單位偏移（上/右上/右/右下/下/左下/左/左上）— 與 legacy 同序
    const int wx[8] = { 0, 1, 1, 1, 0,-1,-1,-1};
    const int wy[8] = {-1,-1, 0, 1, 1, 1, 0,-1};

    int countBP = 0, countDP = 0;
    for (int p = 0; p < pitch_times; ++p) {
        const int pX = pitch_x * (p + 1);
        const int pY = pitch_y * (p + 1);
        #pragma unroll
        for (int w = 0; w < 8; ++w) {
            const int tx = x + wx[w] * pX;
            const int ty = y + wy[w] * pY;
            const int ci = subGetCompareIndex(d_input, width, x, y, tx, ty, search_x, search_y);
            const int diff = center - (int)d_input[ci];   // legacy: CompareResult = center - neighbor
            if ((float)diff >= ThB) ++countBP;             // legacy: >= ThBP
            if ((float)diff <= ThD) ++countDP;             // legacy: <= ThDP
        }
    }

    uint8_t result = 0;
    if (countBP >= choose_amount)      result = 255;  // Bright defect（與 DIV kernel 同值）
    else if (countDP >= choose_amount) result = 128;  // Dark defect
    d_binary[idx] = result;
}

// ============================================================================
// 融合偵測 kernel — DIV 比值 × legacy 逐路投票 × 暗區棄權（algo_mode=2, "DIV-voting"）
//   融合新舊優點：每路比較用 DIV 比值 center/neighbor（照度/背光乘性不變，新 Demo 優點）
//   + legacy 16 路(8方向×PitchTimes)+ 3×3 SAD 局部匹配 + 投票 robustness（舊 SUB 優點）。
//   ★ 邊緣 Defect 誤判改善（核心融合問題）：
//     ① 暗區棄權：鄰點 < dark_eps(=MeanLowThreshold) 該路不投票 → 杜絕暗邊界 mean→0 時比值爆衝的邊緣 FP。
//     ② 投票門檻按「有效路數」比例：need=ceil(choose/16 × valid)，邊緣抽走部分路時靈敏度一致（不會因抽路而漏，也不因抽路變敏感而誤判）。
//     ③ 有效路太少(valid<過半)→ 判背景：角落/深暗區兜底，不冒險判缺。
//   閾值 BTH/DTH 為比值域（如 1.40/0.60，recipe 直接給，不經 SUB→R 換算）。整數定點避免除法。
//   ★ 新增第三支 kernel，不改既有 DIV/SUB/AI kernel（§7 不變式 1）。輸出 255/128/0 下游相容。
// ============================================================================
__global__ void kernelDivVoting8WayStar(
    const uint8_t* __restrict__ d_input,
    uint8_t* __restrict__ d_binary,
    const int width, const int height,
    const int pitch_x, const int pitch_y,
    const int pitch_times, const int choose_amount,
    const float BTH, const float DTH,
    const int search_x, const int search_y,
    const int dark_eps)
{
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= width || y >= height) return;

    const int margin_x = pitch_x * pitch_times + search_x + 1;
    const int margin_y = pitch_y * pitch_times + search_y + 1;
    const int idx = y * width + x;
    if (x < margin_x || x >= width - margin_x ||
        y < margin_y || y >= height - margin_y) {
        d_binary[idx] = 0;
        return;
    }

    const int center = d_input[idx];
    const int wx[8] = { 0, 1, 1, 1, 0,-1,-1,-1};
    const int wy[8] = {-1,-1, 0, 1, 1, 1, 0,-1};

    const int  SCALE = 1024;
    const long bth_s = (long)lroundf(BTH * SCALE);   // ratio≥BTH ⇔ center*SCALE ≥ neighbor*bth_s
    const long dth_s = (long)lroundf(DTH * SCALE);   // ratio≤DTH ⇔ center*SCALE ≤ neighbor*dth_s
    const long lhs   = (long)center * SCALE;

    int countBP = 0, countDP = 0, valid = 0;
    for (int p = 0; p < pitch_times; ++p) {
        const int pX = pitch_x * (p + 1);
        const int pY = pitch_y * (p + 1);
        #pragma unroll
        for (int w = 0; w < 8; ++w) {
            const int tx = x + wx[w] * pX;
            const int ty = y + wy[w] * pY;
            const int ci = subGetCompareIndex(d_input, width, x, y, tx, ty, search_x, search_y);
            const int nb = (int)d_input[ci];
            if (nb < dark_eps) continue;             // ① 暗區/邊界該路棄權（防比值爆衝→邊緣 FP）
            ++valid;
            if (lhs >= (long)nb * bth_s) ++countBP;  // center/nb ≥ BTH → 亮票
            if (lhs <= (long)nb * dth_s) ++countDP;  // center/nb ≤ DTH → 暗票
        }
    }

    const int total_ways = 8 * pitch_times;
    uint8_t result = 0;
    const int min_valid = (total_ways + 1) / 2;      // ③ 需過半路有效才判（角落兜底）
    if (valid >= min_valid) {
        // ② 門檻按有效路比例：邊緣抽走部分路時靈敏度一致
        int need = (int)ceilf((float)choose_amount / (float)total_ways * (float)valid);
        if (need < 1) need = 1;
        if (countBP >= need)      result = 255;
        else if (countDP >= need) result = 128;
    }
    d_binary[idx] = result;
}

void launchDivVotingKernel(
    const uint8_t* d_input,
    uint8_t* d_binary,
    const KernelParams& params,
    dim3 blockDim,
    cudaStream_t stream
) {
    dim3 gridDim(
        (params.width  + blockDim.x - 1) / blockDim.x,
        (params.height + blockDim.y - 1) / blockDim.y
    );
    int pt = params.pitch_times  < 1 ? 1 : params.pitch_times;
    int ca = params.choose_amount < 1 ? 1 : params.choose_amount;
    kernelDivVoting8WayStar<<<gridDim, blockDim, 0, stream>>>(
        d_input, d_binary,
        params.width, params.height,
        params.pitch_x, params.pitch_y,
        pt, ca,
        params.BTH, params.DTH,
        params.search_range_x, params.search_range_y,
        params.dark_eps
    );
    CUDA_CHECK(cudaGetLastError());
}

// ============================================================================
// SUB 前處理 — 忠實移植 legacy 偵測前的 Ip_Remap + 高斯平滑（CamProc.cs:591-641）
//   legacy 順序：Ip_Remap(MimRemap M_FIT_SRC_DATA) → 5×5×SmoothTimes → 3×3×SmoothTimes2 → 偵測。
//   ★ 新增 kernel，不改既有 DIV/AI kernel（§7 不變式 1）。
// ============================================================================

// ---- Ip_Remap：MimRemap(M_FIT_SRC_DATA) = 線性把 [min,max] 拉伸到 [0,255]（對比正規化）----
// 步驟①：256-bin 直方圖（低競爭 atomicAdd）→ host 取 min/max；步驟②：逐像素線性拉伸（in-place）。
__global__ void kernelHistogram256(const uint8_t* __restrict__ d_img, int n, int* __restrict__ d_hist) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    int stride = gridDim.x * blockDim.x;
    for (; i < n; i += stride) atomicAdd(&d_hist[d_img[i]], 1);
}
__global__ void kernelRemapStretch(uint8_t* __restrict__ d_img, int n, int vmin, int vmax) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= n) return;
    int range = vmax - vmin;
    if (range <= 0) return;                      // 平坦影像：MIL 視為無拉伸
    int v = (int)d_img[i];
    int o = (int)((float)(v - vmin) * 255.0f / (float)range + 0.5f);  // 四捨五入
    d_img[i] = (uint8_t)(o < 0 ? 0 : (o > 255 ? 255 : o));
}

void launchRemapFitSrc(uint8_t* d_img, int width, int height, int* d_hist, int* h_hist,
                       cudaStream_t stream) {
    int n = width * height;
    CUDA_CHECK(cudaMemsetAsync(d_hist, 0, 256 * sizeof(int), stream));
    int threads = 256, blocks = 1024;
    kernelHistogram256<<<blocks, threads, 0, stream>>>(d_img, n, d_hist);
    CUDA_CHECK(cudaMemcpyAsync(h_hist, d_hist, 256 * sizeof(int), cudaMemcpyDeviceToHost, stream));
    CUDA_CHECK(cudaStreamSynchronize(stream));   // 需 host 取 min/max
    int vmin = 0; while (vmin < 256 && h_hist[vmin] == 0) ++vmin;
    int vmax = 255; while (vmax >= 0 && h_hist[vmax] == 0) --vmax;
    if (vmin >= vmax) return;                     // 全黑/平坦：不拉伸
    int sblocks = (n + threads - 1) / threads;
    kernelRemapStretch<<<sblocks, threads, 0, stream>>>(d_img, n, vmin, vmax);
    CUDA_CHECK(cudaGetLastError());
}

// ---- 高斯 3×3 平滑（Gau3x3_8：權重 [0,1,0;1,4,1;0,1,0]/8）----
// ⚠️ 係數為依 KernalValue2=8(除數) 重建之標準 plus-Gaussian；bit-exact vs legacy 需原始 Gau3x3_8.mim。
__global__ void kernelSmooth3x3Gau8(const uint8_t* __restrict__ src, uint8_t* __restrict__ dst,
                                    int width, int height) {
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= width || y >= height) return;
    int idx = y * width + x;
    if (x == 0 || y == 0 || x == width - 1 || y == height - 1) { dst[idx] = src[idx]; return; }
    int sum = 4 * (int)src[idx]
            + (int)src[idx - 1] + (int)src[idx + 1]
            + (int)src[idx - width] + (int)src[idx + width];
    dst[idx] = (uint8_t)((sum + 4) >> 3);        // /8 四捨五入（+4 再右移3）
}

// src/scratch 同尺寸 uint8；平滑 smooth_times2 次（in-place 語意：結果寫回 src）。
void launchSmooth3x3(uint8_t* d_img, uint8_t* d_scratch, int width, int height,
                     int times, dim3 blockDim, cudaStream_t stream) {
    if (times <= 0) return;
    dim3 gridDim((width + blockDim.x - 1) / blockDim.x, (height + blockDim.y - 1) / blockDim.y);
    for (int t = 0; t < times; ++t) {
        kernelSmooth3x3Gau8<<<gridDim, blockDim, 0, stream>>>(d_img, d_scratch, width, height);
        CUDA_CHECK(cudaMemcpyAsync(d_img, d_scratch, (size_t)width * height, cudaMemcpyDeviceToDevice, stream));
    }
    CUDA_CHECK(cudaGetLastError());
}

void launchSubVotingKernel(
    const uint8_t* d_input,
    uint8_t* d_binary,
    const KernelParams& params,
    dim3 blockDim,
    cudaStream_t stream
) {
    dim3 gridDim(
        (params.width  + blockDim.x - 1) / blockDim.x,
        (params.height + blockDim.y - 1) / blockDim.y
    );
    int pt = params.pitch_times  < 1 ? 1 : params.pitch_times;
    int ca = params.choose_amount < 1 ? 1 : params.choose_amount;
    kernelSub8WayStarVoting<<<gridDim, blockDim, 0, stream>>>(
        d_input, d_binary,
        params.width, params.height,
        params.pitch_x, params.pitch_y,
        pt, ca,
        params.BTH, params.DTH,
        params.search_range_x, params.search_range_y
    );
    CUDA_CHECK(cudaGetLastError());
}

void launchFast8WayKernel(
    const uint8_t* d_input,
    uint8_t* d_binary,
    const KernelParams& params,
    dim3 blockDim,
    cudaStream_t stream,
    int search_range  // 0=no search, 1=±1 pixel, 2=±2 pixels
) {
    dim3 gridDim(
        (params.width + blockDim.x - 1) / blockDim.x,
        (params.height + blockDim.y - 1) / blockDim.y
    );
    
    // Calculate shared memory requirements
    int halo_x = params.pitch_x + search_range;
    int halo_y = params.pitch_y + search_range;
    int s_width = blockDim.x + 2 * halo_x;
    int s_height = blockDim.y + 2 * halo_y;
    size_t shared_mem_size = s_width * s_height * sizeof(uint8_t);
    
    // Check if shared memory requirements are within limits
    // Query device for actual shared memory limit instead of hardcoding 48KB
    static size_t max_shared_mem = 0;
    if (max_shared_mem == 0) {
        int device;
        cudaGetDevice(&device);
        int max_smem;
        cudaDeviceGetAttribute(&max_smem, cudaDevAttrMaxSharedMemoryPerBlock, device);
        max_shared_mem = (size_t)max_smem;
    }
    bool use_shared = (shared_mem_size <= max_shared_mem);
    
    // For multi-scale (smaller images), use simpler global memory kernel
    bool is_multiscale = (params.width < 8000);
    
    if (is_multiscale) {
        // Use simple global memory version for multi-scale
        kernelFast8WayComparison<<<gridDim, blockDim, 0, stream>>>(
            d_input, d_binary,
            params.width, params.height,
            params.pitch_x, params.pitch_y,
            params.BTH, params.DTH,
            search_range
        );
    } else {
        // Full-res images: prefer texture memory for 2D cache locality
        // On integrated GPUs (GB10), the large L2 cache (24MB) makes
        // texture still effective despite shared physical memory.
        bindTextureObject(d_input, params.width, params.height);
        
        if (g_tex_input != 0) {
            kernelFast8WayTexture<<<gridDim, blockDim, 0, stream>>>(
                g_tex_input, d_binary,
                params.width, params.height,
                params.pitch_x, params.pitch_y,
                params.BTH, params.DTH,
                search_range
            );
        } else if (use_shared) {
            kernelFast8WayShared<<<gridDim, blockDim, shared_mem_size, stream>>>(
                d_input, d_binary,
                params.width, params.height,
                params.pitch_x, params.pitch_y,
                params.BTH, params.DTH,
                search_range
            );
        } else {
            kernelFast8WayComparison<<<gridDim, blockDim, 0, stream>>>(
                d_input, d_binary,
                params.width, params.height,
                params.pitch_x, params.pitch_y,
                params.BTH, params.DTH,
                search_range
            );
        }
    }
    CUDA_CHECK(cudaGetLastError());
}

void launchFastCCLKernel(
    const uint8_t* d_binary,
    int* d_labels,
    int width,
    int height,
    dim3 blockDim,
    cudaStream_t stream
) {
    dim3 gridDim(
        (width + blockDim.x - 1) / blockDim.x,
        (height + blockDim.y - 1) / blockDim.y
    );
    
    size_t total_pixels = (size_t)width * height;
    initPersistentBuffers(total_pixels);
    
    // Pre-fill labels with -1 using fast GPU memset (DMA engine, ~0.2ms for 160MB)
    // This eliminates the need for InitMerge to write -1 for 99.9% of pixels
    CUDA_CHECK(cudaMemsetAsync(d_labels, 0xFF, (size_t)width * height * sizeof(int), stream));
    
    // Init only defect pixels (binary>0), skips 99.9% of pixels
    kernelFastCCLInitMerge<<<gridDim, blockDim, 0, stream>>>(
        d_binary, d_labels, width, height
    );
    
    // Merge passes — iterate to convergence for deterministic, fully-merged labels.
    // NOTE: host-side launch orchestration ONLY; the __global__ kernel bodies are
    // unchanged. A single pass leaves multi-hop blobs racily under-merged (the labels
    // depend on thread-race order → non-deterministic component count). atomicMin's
    // fixed point is unique and order-independent, so looping until d_changed==0
    // yields bit-exact results (satisfies the "same image twice → bit-exact" invariant)
    // and also fixes large-blob under-merging.
    {
        int h_changed = 1;
        const int MAX_CCL_ITERS = 1000;  // safety cap; typical CF point-defects converge in 2-4
        for (int iter = 0; iter < MAX_CCL_ITERS && h_changed; ++iter) {
            CUDA_CHECK(cudaMemsetAsync(g_sparse.d_changed, 0, sizeof(int), stream));
            kernelFastCCLMerge<<<gridDim, blockDim, 0, stream>>>(
                d_binary, d_labels, width, height, g_sparse.d_changed);
            CUDA_CHECK(cudaMemcpyAsync(&h_changed, g_sparse.d_changed, sizeof(int),
                                       cudaMemcpyDeviceToHost, stream));
            CUDA_CHECK(cudaStreamSynchronize(stream));
        }
    }

    // NOTE: Flatten is now fused into launchFastBlobAnalysis (kernelFusedFlattenBlobStats)
    // This saves one full-image scan (~0.5ms for 40M pixels)
}

void launchFastBlobAnalysis(
    const uint8_t* d_input,
    const uint8_t* d_binary,
    const int* d_labels,
    DefectInfo* d_defects,
    int* d_defect_count,
    int width,
    int height,
    int max_defects,
    cudaStream_t stream
) {
    size_t total_pixels = (size_t)width * height;
    initPersistentBuffers(total_pixels);
    
    dim3 blockDim2D(32, 32);
    dim3 gridDim2D(
        (width + blockDim2D.x - 1) / blockDim2D.x,
        (height + blockDim2D.y - 1) / blockDim2D.y
    );
    
    // OPTIMIZATION: Use cudaMemsetAsync instead of kernel launch for sparse buffer init
    // For int buffers that need 0: memset to 0x00
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_counts, 0, MAX_UNIQUE_LABELS * sizeof(int), stream));
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_is_bright, 0, MAX_UNIQUE_LABELS * sizeof(int), stream));
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_unique_count, 0, sizeof(int), stream));
    // For float buffers that need 0.0: memset to 0x00 (IEEE 754: 0x00000000 = 0.0f)
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_sum_x, 0, MAX_UNIQUE_LABELS * sizeof(float), stream));
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_sum_y, 0, MAX_UNIQUE_LABELS * sizeof(float), stream));
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_sum_brightness, 0, MAX_UNIQUE_LABELS * sizeof(float), stream));
    // For min/max: 0x7F7F7F7F for min_x/min_y (large positive), 0x80808080 for max_x/max_y (large negative)
    // Actually use kernel only for min/max initialization since they need INT_MAX/-1
    // But we can use 0xFF for min (=0xFFFFFFFF = -1 as signed, but we want INT_MAX)
    // Better: just use 0x7F for min (0x7F7F7F7F = 2139062143, close to INT_MAX)
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_min_x, 0x7F, MAX_UNIQUE_LABELS * sizeof(int), stream));
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_min_y, 0x7F, MAX_UNIQUE_LABELS * sizeof(int), stream));
    // For max: 0x80 = 0x80808080 = -2139062144, a large negative
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_max_x, 0x80, MAX_UNIQUE_LABELS * sizeof(int), stream));
    CUDA_CHECK(cudaMemsetAsync(g_sparse.d_max_y, 0x80, MAX_UNIQUE_LABELS * sizeof(int), stream));
    
    // Fused Flatten + BlobStats: combines two full-image scans into one
    kernelFusedFlattenBlobStats<<<gridDim2D, blockDim2D, 0, stream>>>(
        d_input, d_binary, const_cast<int*>(d_labels),
        g_sparse.d_counts, g_sparse.d_sum_x, g_sparse.d_sum_y, g_sparse.d_sum_brightness,
        g_sparse.d_min_x, g_sparse.d_max_x, g_sparse.d_min_y, g_sparse.d_max_y,
        g_sparse.d_is_bright,
        g_sparse.d_unique_labels, g_sparse.d_unique_count,
        width, height
    );
    
    // Get unique label count (need sync here but sparse collection is much faster)
    int unique_count = 0;
    CUDA_CHECK(cudaMemcpyAsync(&unique_count, g_sparse.d_unique_count, sizeof(int), cudaMemcpyDeviceToHost, stream));
    CUDA_CHECK(cudaStreamSynchronize(stream));
    
    CUDA_CHECK(cudaMemsetAsync(d_defect_count, 0, sizeof(int), stream));
    
    if (unique_count > 0) {
        unique_count = min(unique_count, MAX_UNIQUE_LABELS);
        
        // Collect defects only from unique labels (hundreds of threads vs 65K)
        int collectBlockDim = 256;
        int collectGridDim = (unique_count + collectBlockDim - 1) / collectBlockDim;
        
        kernelSparseCollectDefects<<<collectGridDim, collectBlockDim, 0, stream>>>(
            g_sparse.d_unique_labels,
            unique_count,
            g_sparse.d_counts, g_sparse.d_sum_x, g_sparse.d_sum_y, g_sparse.d_sum_brightness,
            g_sparse.d_min_x, g_sparse.d_max_x, g_sparse.d_min_y, g_sparse.d_max_y,
            g_sparse.d_is_bright,
            d_defects, d_defect_count,
            max_defects, 1, 300, 5.0f
        );
    }
}

// ============================================================================
// Multi-Scale Detection Launch Functions
// ============================================================================

void launchDownsample2x(
    const uint8_t* d_input,
    uint8_t* d_output,
    int src_width, int src_height,
    int dst_width, int dst_height,
    cudaStream_t stream
) {
    dim3 blockDim(32, 32);
    dim3 gridDim(
        (dst_width + blockDim.x - 1) / blockDim.x,
        (dst_height + blockDim.y - 1) / blockDim.y
    );
    
    kernelDownsample2x<<<gridDim, blockDim, 0, stream>>>(
        d_input, d_output,
        src_width, src_height,
        dst_width, dst_height
    );
}

void launchDownsample4x(
    const uint8_t* d_input,
    uint8_t* d_output,
    int src_width, int src_height,
    int dst_width, int dst_height,
    cudaStream_t stream
) {
    dim3 blockDim(32, 32);
    dim3 gridDim(
        (dst_width + blockDim.x - 1) / blockDim.x,
        (dst_height + blockDim.y - 1) / blockDim.y
    );
    
    kernelDownsample4x<<<gridDim, blockDim, 0, stream>>>(
        d_input, d_output,
        src_width, src_height,
        dst_width, dst_height
    );
}

void launchUpscaleBinaryMask2x(
    const uint8_t* d_small_binary,
    uint8_t* d_full_binary,
    int small_width, int small_height,
    int full_width, int full_height,
    cudaStream_t stream
) {
    dim3 blockDim(32, 32);
    dim3 gridDim(
        (small_width + blockDim.x - 1) / blockDim.x,
        (small_height + blockDim.y - 1) / blockDim.y
    );
    
    kernelUpscaleBinaryMask2x<<<gridDim, blockDim, 0, stream>>>(
        d_small_binary, d_full_binary,
        small_width, small_height,
        full_width, full_height
    );
}

void launchUpscaleBinaryMask4x(
    const uint8_t* d_small_binary,
    uint8_t* d_full_binary,
    int small_width, int small_height,
    int full_width, int full_height,
    cudaStream_t stream
) {
    dim3 blockDim(32, 32);
    dim3 gridDim(
        (small_width + blockDim.x - 1) / blockDim.x,
        (small_height + blockDim.y - 1) / blockDim.y
    );
    
    kernelUpscaleBinaryMask4x<<<gridDim, blockDim, 0, stream>>>(
        d_small_binary, d_full_binary,
        small_width, small_height,
        full_width, full_height
    );
}

void allocateMultiScaleBuffers(MultiScaleBuffers& buffers, int full_width, int full_height) {
    buffers.width_2x = full_width / 2;
    buffers.height_2x = full_height / 2;
    buffers.width_4x = full_width / 4;
    buffers.height_4x = full_height / 4;
    
    size_t size_2x = (size_t)buffers.width_2x * buffers.height_2x;
    size_t size_4x = (size_t)buffers.width_4x * buffers.height_4x;
    
    CUDA_CHECK(cudaMalloc(&buffers.d_image_2x, size_2x));
    CUDA_CHECK(cudaMalloc(&buffers.d_image_4x, size_4x));
    CUDA_CHECK(cudaMalloc(&buffers.d_binary_2x, size_2x));
    CUDA_CHECK(cudaMalloc(&buffers.d_binary_4x, size_4x));
    
    buffers.allocated = true;
}

void freeMultiScaleBuffers(MultiScaleBuffers& buffers) {
    if (!buffers.allocated) return;
    
    if (buffers.d_image_2x) cudaFree(buffers.d_image_2x);
    if (buffers.d_image_4x) cudaFree(buffers.d_image_4x);
    if (buffers.d_binary_2x) cudaFree(buffers.d_binary_2x);
    if (buffers.d_binary_4x) cudaFree(buffers.d_binary_4x);
    
    buffers.d_image_2x = nullptr;
    buffers.d_image_4x = nullptr;
    buffers.d_binary_2x = nullptr;
    buffers.d_binary_4x = nullptr;
    buffers.allocated = false;
}
