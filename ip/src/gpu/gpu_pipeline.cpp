// ═══ 📖 手冊對照（docs/html/cf-aoi-training.html，開啟後 ⌘K 搜章節）═══
// [手冊 r1] R1.4 GPU 管線八步流程圖＋逐站計時動畫（mode0 加總=黃金數字 7.4ms）
// [手冊 ch3] GPU 工廠比喻 / 決定性兩根柱子（CCL 收斂＋canonical 排序）
// [手冊 p4] GpuPipelineImpl::run 導師卡（gpu_ms 含 H2D；換尺寸=全套重配）
// ═══════════════════════════════════════════════════════════════
/**
 * ============================================================================
 * gpu_pipeline.cpp
 * 遷移自 Reference/gpu_algo/src/batch_detector.cpp
 *   - GPUMemoryManager（記憶體管理 / zero-copy 偵測）
 *   - BatchDefectDetector::processImage（8-Way → CCL → Blob → AI）
 * 變更：移除 FileReceiver/SocketReceiver/Rivermax，改純函式入口 process_frame。
 * 保留：所有 GPU 計算邏輯不變（kernel 一字不改）。
 * ============================================================================
 */

#include "gpu_pipeline.h"

#include <algorithm>
#include <cstring>
#include <iostream>

#include <cuda_runtime.h>

#include "cuda_kernels.h"               // ✅ 直接複製來的，不改
#include "ai/tensor_core_classifier.h"  // ✅ 直接複製來的，不改

namespace {
constexpr int MAX_DEFECTS = 10000;  // = MAX_DEFECTS_PER_IMAGE
}

// ============================================================================
// GPUMemoryManager — 自 batch_detector.cpp 完整搬過來（input 改吃 raw 指標）
// ============================================================================
class GPUMemoryManager {
private:
    uint8_t* d_input = nullptr;
    uint8_t* d_binary = nullptr;
    int* d_labels = nullptr;
    DefectInfo* d_defects = nullptr;
    int* d_defect_count = nullptr;
    DefectInfo* d_filtered_defects = nullptr;
    int* d_filtered_count = nullptr;
    int* d_hist = nullptr;              // SUB 前處理 Ip_Remap：256-bin 直方圖（取 min/max）
    int* h_hist = nullptr;             // 對應 host 端（pinned）

    uint8_t* h_pinned = nullptr;        // discrete: pinned staging
    uint8_t* h_mapped_input = nullptr;  // integrated: zero-copy host
    uint8_t* d_mapped_input = nullptr;

    bool is_integrated_gpu = false;
    bool use_zero_copy = false;

    size_t width = 0, height = 0, total_pixels = 0;
    int max_defects_stored = 0;
    bool allocated = false;

    void detectIntegratedGPU() {
        int device;
        cudaGetDevice(&device);
        cudaDeviceProp prop;
        cudaGetDeviceProperties(&prop, device);
        is_integrated_gpu = (prop.integrated != 0);
        use_zero_copy = is_integrated_gpu && (prop.canMapHostMemory != 0);
        if (use_zero_copy) {
            cudaSetDeviceFlags(cudaDeviceMapHost);
        }
    }

public:
    GPUMemoryManager() { detectIntegratedGPU(); }
    ~GPUMemoryManager() { deallocate(); }

    bool isZeroCopy() const { return use_zero_copy; }

    void allocate(int w, int h, int max_defects) {
        if (allocated && width == (size_t)w && height == (size_t)h) return;
        if (allocated) deallocate();

        width = w;
        height = h;
        total_pixels = (size_t)w * h;
        max_defects_stored = max_defects;

        if (use_zero_copy) {
            CUDA_CHECK(cudaMalloc(&d_input, total_pixels * sizeof(uint8_t)));
            CUDA_CHECK(cudaHostAlloc(&h_mapped_input, total_pixels * sizeof(uint8_t),
                                     cudaHostAllocMapped));
            CUDA_CHECK(cudaHostGetDevicePointer(&d_mapped_input, h_mapped_input, 0));
        } else {
            CUDA_CHECK(cudaMalloc(&d_input, total_pixels * sizeof(uint8_t)));
            CUDA_CHECK(cudaMallocHost(&h_pinned, total_pixels * sizeof(uint8_t)));
        }

        CUDA_CHECK(cudaMalloc(&d_binary, total_pixels * sizeof(uint8_t)));
        CUDA_CHECK(cudaMalloc(&d_labels, total_pixels * sizeof(int)));
        CUDA_CHECK(cudaMalloc(&d_defects, max_defects * sizeof(DefectInfo)));
        CUDA_CHECK(cudaMalloc(&d_defect_count, sizeof(int)));
        CUDA_CHECK(cudaMalloc(&d_filtered_defects, max_defects * sizeof(DefectInfo)));
        CUDA_CHECK(cudaMalloc(&d_filtered_count, sizeof(int)));
        CUDA_CHECK(cudaMalloc(&d_hist, 256 * sizeof(int)));
        CUDA_CHECK(cudaMallocHost(&h_hist, 256 * sizeof(int)));

        CUDA_CHECK(cudaMemset(d_binary, 0, total_pixels * sizeof(uint8_t)));
        CUDA_CHECK(cudaMemset(d_defect_count, 0, sizeof(int)));
        CUDA_CHECK(cudaMemset(d_filtered_count, 0, sizeof(int)));

        initPersistentBuffers(total_pixels);
        allocated = true;
    }

    void deallocate() {
        if (!allocated) return;
        if (use_zero_copy) {
            if (h_mapped_input) cudaFreeHost(h_mapped_input);
            if (d_input) cudaFree(d_input);
            h_mapped_input = nullptr;
            d_mapped_input = nullptr;
            d_input = nullptr;
        } else {
            if (d_input) cudaFree(d_input);
            if (h_pinned) cudaFreeHost(h_pinned);
            d_input = nullptr;
            h_pinned = nullptr;
        }
        if (d_binary) cudaFree(d_binary);
        if (d_labels) cudaFree(d_labels);
        if (d_defects) cudaFree(d_defects);
        if (d_defect_count) cudaFree(d_defect_count);
        if (d_filtered_defects) cudaFree(d_filtered_defects);
        if (d_filtered_count) cudaFree(d_filtered_count);
        if (d_hist) cudaFree(d_hist);
        if (h_hist) cudaFreeHost(h_hist);
        d_hist = nullptr;
        h_hist = nullptr;
        freePersistentBuffers();
        d_binary = nullptr;
        d_labels = nullptr;
        d_defects = nullptr;
        d_defect_count = nullptr;
        d_filtered_defects = nullptr;
        d_filtered_count = nullptr;
        allocated = false;
    }

    // 自 cv::Mat 版本改寫：直接吃 host raw 指標（w*h bytes, Mono8）。
    // 先 memcpy 進 pinned / mapped staging 取得穩定的 async 傳輸，
    // 再搬到 d_input（kernel cache-friendly access）。
    void uploadImage(const uint8_t* host_data, cudaStream_t stream = 0) {
        if (use_zero_copy) {
            std::memcpy(h_mapped_input, host_data, total_pixels);
            CUDA_CHECK(cudaMemcpyAsync(d_input, d_mapped_input, total_pixels,
                                       cudaMemcpyDeviceToDevice, stream));
        } else {
            std::memcpy(h_pinned, host_data, total_pixels);
            CUDA_CHECK(cudaMemcpyAsync(d_input, h_pinned, total_pixels,
                                       cudaMemcpyHostToDevice, stream));
        }
    }

    int downloadFilteredDefects(std::vector<DefectInfo>& defects) {
        int n;
        CUDA_CHECK(cudaMemcpy(&n, d_filtered_count, sizeof(int), cudaMemcpyDeviceToHost));
        n = std::min(n, max_defects_stored);
        defects.resize(n);
        if (n > 0)
            CUDA_CHECK(cudaMemcpy(defects.data(), d_filtered_defects,
                                  n * sizeof(DefectInfo), cudaMemcpyDeviceToHost));
        return n;
    }

    int downloadDefects(std::vector<DefectInfo>& defects, int max_defects) {
        int n;
        CUDA_CHECK(cudaMemcpy(&n, d_defect_count, sizeof(int), cudaMemcpyDeviceToHost));
        n = std::min(n, max_defects);
        defects.resize(n);
        if (n > 0)
            CUDA_CHECK(cudaMemcpy(defects.data(), d_defects,
                                  n * sizeof(DefectInfo), cudaMemcpyDeviceToHost));
        return n;
    }

    uint8_t* getInputPtr() { return d_input; }
    uint8_t* getBinaryPtr() { return d_binary; }
    int* getHistDevice() { return d_hist; }
    int* getHistHost() { return h_hist; }
    int* getLabelsPtr() { return d_labels; }
    DefectInfo* getDefectsPtr() { return d_defects; }
    int* getDefectCountPtr() { return d_defect_count; }
    DefectInfo* getFilteredDefectsPtr() { return d_filtered_defects; }
    int* getFilteredCountPtr() { return d_filtered_count; }
};

// ============================================================================
// GpuPipelineImpl — 自 BatchDefectDetector 改寫（移除 batch/CSV/檔案外殼）
// ============================================================================
class GpuPipelineImpl {
public:
    GPUMemoryManager gpu_mem;
    cudaStream_t stream = 0;
    cudaStream_t ai_stream = 0;
    TensorCoreClassifier* ai_classifier = nullptr;
    bool ai_on = false;        // 模型是否載入成功
    bool ai_active = false;    // 執行期開關（預設停用：訓練資料不足，暫不推論）
    cudaEvent_t ev_start = nullptr, ev_stop = nullptr;
    MultiScaleBuffers ms_{};   // 多尺度 resize-redetect（大顆 Defect 漏檢補強）緩衝

    explicit GpuPipelineImpl(const std::string& ai_model_dir) {
        CUDA_CHECK(cudaStreamCreate(&stream));
        CUDA_CHECK(cudaStreamCreate(&ai_stream));
        CUDA_CHECK(cudaEventCreate(&ev_start));
        CUDA_CHECK(cudaEventCreate(&ev_stop));

        ai_classifier = new TensorCoreClassifier(MAX_DEFECTS);
        if (ai_classifier->initializeFromSklearn(ai_model_dir)) {
            ai_on = true;
            ai_classifier->setStream(ai_stream);
            std::cout << "[AI] Tensor Core classifier 已載入: " << ai_model_dir << "\n";
        } else {
            std::cout << "[AI] 模型未找到 (" << ai_model_dir << ")，AI 過濾已停用\n";
            delete ai_classifier;
            ai_classifier = nullptr;
        }
    }

    ~GpuPipelineImpl() {
        if (ev_stop) cudaEventDestroy(ev_stop);
        if (ev_start) cudaEventDestroy(ev_start);
        if (stream) cudaStreamDestroy(stream);
        if (ai_stream) cudaStreamDestroy(ai_stream);
        if (ai_classifier) delete ai_classifier;
        freeMultiScaleBuffers(ms_);
    }

    DetectionResult run(const uint8_t* img, int w, int h, const ZoneConfig& cfg) {
        DetectionResult result;
        result.image_width = w;
        result.image_height = h;
        result.panel_id = cfg.panel_id;

        // Step 1: 配置記憶體（同尺寸則重用）
        gpu_mem.allocate(w, h, MAX_DEFECTS);

        CUDA_CHECK(cudaEventRecord(ev_start, stream));

        // Step 2: 上傳影像
        gpu_mem.uploadImage(img, stream);

        // Step 2.5: Lens Shading Correction（可選，預設關閉以保 bit-exact）
        dim3 blockDim(cfg.block_dim_x, cfg.block_dim_y);
        if (cfg.enable_lsc) {
            LensShadingParams lsc;
            lsc.center_x = w / 2.0f;
            lsc.center_y = h / 2.0f;
            lsc.k1 = cfg.lsc_k1;
            lsc.k2 = cfg.lsc_k2;
            lsc.k3 = cfg.lsc_k3;
            lsc.max_gain = cfg.lsc_max_gain;
            launchLensShadingCorrection(gpu_mem.getInputPtr(), w, h, lsc, blockDim, stream);
        }

        // Step 3: 8-Way 比對參數
        KernelParams params;
        params.width = w;
        params.height = h;
        params.pitch_x = cfg.pitch_x;
        params.pitch_y = cfg.pitch_y;
        params.search_range_x = cfg.search_range_x;
        params.search_range_y = cfg.search_range_y;
        params.BTH = cfg.BTH;
        params.DTH = cfg.DTH;
        params.pitch_times   = cfg.pitch_times;
        params.choose_amount = cfg.choose_amount;
        params.dark_eps      = cfg.mean_low_threshold;

        // [手冊 ch3] 三演算法白話對照表＋沙盒（親手拉爆 pitch）；[手冊 ch4] 守門怎麼選到這裡
        // Step 4: 偵測 —— DIV(比例單次) / SUB(灰階差投票,legacy) / DIV-voting(融合:比值逐路投票)
        if (cfg.algo_mode == 1 || cfg.algo_mode == 2) {
            // 前處理（legacy 偵測前順序：Ip_Remap → 3×3 高斯平滑×SmoothTimes2；d_binary 暫作平滑 scratch）
            // ★ remap 只用於 SUB(mode1)：它減 min 會破壞 DIV 比值 center/neighbor 的照度不變性，
            //   故 DIV-voting(mode2) 不做 remap（比值本身抵消照度）；兩者皆做高斯平滑降噪。
            if (cfg.preproc_remap && cfg.algo_mode == 1) {
                launchRemapFitSrc(gpu_mem.getInputPtr(), w, h,
                                  gpu_mem.getHistDevice(), gpu_mem.getHistHost(), stream);
            }
            if (cfg.smooth_times2 > 0) {
                launchSmooth3x3(gpu_mem.getInputPtr(), gpu_mem.getBinaryPtr(), w, h,
                                cfg.smooth_times2, blockDim, stream);
                CUDA_CHECK(cudaMemsetAsync(gpu_mem.getBinaryPtr(), 0, (size_t)w * h, stream));
            }
            if (cfg.algo_mode == 1)
                launchSubVotingKernel(gpu_mem.getInputPtr(), gpu_mem.getBinaryPtr(), params,
                                      blockDim, stream);
            else
                launchDivVotingKernel(gpu_mem.getInputPtr(), gpu_mem.getBinaryPtr(), params,
                                      blockDim, stream);
        } else {
            launchFast8WayKernel(gpu_mem.getInputPtr(), gpu_mem.getBinaryPtr(), params,
                                 blockDim, stream, cfg.fast_search_range);
        }

        // Step 4.5: 多尺度 resize-redetect（大顆 Defect 漏檢補強）——
        //   大於 pitch reach(~pitch×pitch_times) 的缺陷,全解析度下 8 鄰皆落在缺陷內→無局部對比→漏檢;
        //   downsample 2×/4× 後縮回 pitch 範圍 → 同 kernel 同參數偵測 → upscale OR-merge 進 d_binary。
        //   ★ 重用既有 Demo 多尺度 downsample/upscale kernel(先前未接線),此處接入偵測主路(投票模式 1/2)。
        // 僅 mode2(融合)啟用多尺度：legacy SUB(mode1) 維持忠實基準(無多尺度)，新 DIV-voting 才補大顆 Defect。
        if (cfg.enable_multiscale >= 1 && cfg.algo_mode == 2) {
            if (!ms_.allocated || ms_.width_2x != w / 2 || ms_.height_2x != h / 2) {
                freeMultiScaleBuffers(ms_);
                allocateMultiScaleBuffers(ms_, w, h);
            }
            auto detect_scale = [&](const uint8_t* src, uint8_t* bin, int sw, int sh) {
                KernelParams ps = params; ps.width = sw; ps.height = sh;
                launchDivVotingKernel(src, bin, ps, blockDim, stream);
            };
            launchDownsample2x(gpu_mem.getInputPtr(), ms_.d_image_2x, w, h, ms_.width_2x, ms_.height_2x, stream);
            detect_scale(ms_.d_image_2x, ms_.d_binary_2x, ms_.width_2x, ms_.height_2x);
            launchUpscaleBinaryMask2x(ms_.d_binary_2x, gpu_mem.getBinaryPtr(),
                                      ms_.width_2x, ms_.height_2x, w, h, stream);
            if (cfg.enable_multiscale >= 2) {
                launchDownsample4x(gpu_mem.getInputPtr(), ms_.d_image_4x, w, h, ms_.width_4x, ms_.height_4x, stream);
                detect_scale(ms_.d_image_4x, ms_.d_binary_4x, ms_.width_4x, ms_.height_4x);
                launchUpscaleBinaryMask4x(ms_.d_binary_4x, gpu_mem.getBinaryPtr(),
                                          ms_.width_4x, ms_.height_4x, w, h, stream);
            }
        }

        // Step 5: CCL
        launchFastCCLKernel(gpu_mem.getBinaryPtr(), gpu_mem.getLabelsPtr(),
                           w, h, blockDim, stream);

        // Step 6: Blob analysis
        CUDA_CHECK(cudaMemsetAsync(gpu_mem.getDefectCountPtr(), 0, sizeof(int), stream));
        launchFastBlobAnalysis(gpu_mem.getInputPtr(), gpu_mem.getBinaryPtr(),
                              gpu_mem.getLabelsPtr(), gpu_mem.getDefectsPtr(),
                              gpu_mem.getDefectCountPtr(), w, h, MAX_DEFECTS, stream);

        CUDA_CHECK(cudaEventRecord(ev_stop, stream));
        CUDA_CHECK(cudaStreamSynchronize(stream));

        // Step 7: AI 過濾（可選；ai_active=false 時跳過，缺陷全數輸出待人工複核）
        if (ai_on && ai_active && ai_classifier) {
            ai_classifier->classifyAndFilterGPU(
                gpu_mem.getInputPtr(), gpu_mem.getDefectsPtr(),
                gpu_mem.getDefectCountPtr(), gpu_mem.getFilteredDefectsPtr(),
                gpu_mem.getFilteredCountPtr(), MAX_DEFECTS, w, h, ai_stream);
            CUDA_CHECK(cudaStreamSynchronize(ai_stream));
            result.num_defects = gpu_mem.downloadFilteredDefects(result.defects);
        } else {
            result.num_defects = gpu_mem.downloadDefects(result.defects, MAX_DEFECTS);
        }

        float gpu_ms = 0;
        CUDA_CHECK(cudaEventElapsedTime(&gpu_ms, ev_start, ev_stop));
        result.process_time_ms = (double)gpu_ms;

        // Blob analysis 用 atomicAdd append 缺陷，陣列順序隨 thread-race 而異（集合相同、
        // 順序不定）。以 canonical key 排序使輸出 bit-exact 可重現：CCL 收斂後每個連通元件的
        // root `label`（最小像素索引）唯一，作為主鍵即為全序；座標為次鍵以防萬一。
        std::sort(result.defects.begin(), result.defects.end(),
                  [](const DefectInfo& a, const DefectInfo& b) {
                      if (a.label != b.label) return a.label < b.label;
                      if (a.center_y != b.center_y) return a.center_y < b.center_y;
                      if (a.center_x != b.center_x) return a.center_x < b.center_x;
                      return a.size < b.size;
                  });

        for (const auto& d : result.defects) {
            if (d.is_bright) result.num_bright++;
            else result.num_dark++;
        }
        result.pass = (result.num_defects == 0);
        return result;
    }
};

// ============================================================================
// GpuPipeline — public facade
// ============================================================================
GpuPipeline::GpuPipeline(const std::string& ai_model_dir)
    : impl_(new GpuPipelineImpl(ai_model_dir)) {}

GpuPipeline::~GpuPipeline() { delete impl_; }

DetectionResult GpuPipeline::process_frame(const uint8_t* img, int w, int h,
                                           const ZoneConfig& cfg) {
    return impl_->run(img, w, h, cfg);
}

void GpuPipeline::set_ai_active(bool active) { impl_->ai_active = active; }
bool GpuPipeline::ai_enabled() const { return impl_->ai_on && impl_->ai_active; }
bool GpuPipeline::ai_model_loaded() const { return impl_->ai_on; }
bool GpuPipeline::is_zero_copy() const { return impl_->gpu_mem.isZeroCopy(); }
