# IP 程式 — CLAUDE.md

> 先讀 `../CLAUDE.md` 了解遷移策略，再讀本文件。
> **核心原則：GPU kernel 直接複製 `Reference/gpu_algo/`，只換 I/O 外殼。**

---

## 1. 從 Reference 遷移的檔案對照表

| Reference 來源 | ip/src/ 目標 | 處理方式 |
|---------------|-------------|---------|
| `gpu_algo/src/cuda_kernels_fast.cu` | `gpu/cuda_kernels.cu` | ✅ **直接複製，不改** |
| `gpu_algo/src/tensor_core_classifier.cu` | `ai/ai_kernels.cu` | ✅ **直接複製，不改** |
| `gpu_algo/include/cuda_kernels.h` | `gpu/cuda_kernels.h` | ✅ 直接複製 |
| `gpu_algo/include/tensor_core_classifier.h` | `ai/ai_classifier.h` | ✅ 直接複製 |
| `gpu_algo/include/config_parser.h` | `config/config_parser.h` | ✅ 直接複製 |
| `gpu_algo/include/inline_types.h` | `config/inline_types.h` | ✅ 直接複製 |
| `gpu_algo/config/config.ini` | `config/default_zone.ini` | ✅ 直接複製（參數名稱保留）|
| `gpu_algo/src/batch_detector.cpp` | `gpu/gpu_pipeline.cpp` | 🔧 改外殼（移除 FileReceiver，加純函式入口）|
| `gpu_algo/src/inline_controller.cpp` | `modes/rdma_validator.cpp` | 🔧 抽出 RDMA 接收邏輯 |
| `gpu_algo/src/tdd_runner.cpp` | `tests/tdd_runner.cpp` | 🔧 保留診斷邏輯 |
| `phase1_tests/src/t40_e2e_server/` | `image_source/rdma_source.cpp` | 🔧 升級為生產等級 |
| — | `image_source/tcp_source.cpp` | 🆕 全新（offline-tcp，Step 1）|
| — | `image_source/file_source.cpp` | 🆕 全新（offline-file）|
| — | `modes/image_capturer.cpp` | 🆕 全新（Step 4 存圖）|
| — | `control_server.cpp` | 🆕 全新（TCP JSON server）|

---

## 2. 第一步：複製 CUDA Kernels

```bash
# 在 ip/ 目錄下執行
mkdir -p src/gpu src/ai src/config src/image_source src/modes src/tests

# ★ 最重要的步驟：直接複製，不修改
cp ../Reference/gpu_algo/src/cuda_kernels_fast.cu    src/gpu/cuda_kernels.cu
cp ../Reference/gpu_algo/src/tensor_core_classifier.cu src/ai/ai_kernels.cu
cp ../Reference/gpu_algo/include/cuda_kernels.h      src/gpu/cuda_kernels.h
cp ../Reference/gpu_algo/include/tensor_core_classifier.h src/ai/ai_classifier.h
cp ../Reference/gpu_algo/include/config_parser.h     src/config/config_parser.h
cp ../Reference/gpu_algo/include/inline_types.h      src/config/inline_types.h
cp ../Reference/gpu_algo/include/rf_model_config.h   src/ai/rf_model_config.h
cp ../Reference/gpu_algo/config/config.ini            config/default_zone.ini

# 複製 TDD 測試基礎設施
cp -r ../Reference/gpu_algo/src/tests/              src/tests/
cp -r ../Reference/gpu_algo/include/tdd/            src/tests/tdd/
```

---

## 3. 第二步：改寫 gpu_pipeline.cpp（外殼替換）

`Reference/gpu_algo/src/batch_detector.cpp` 的 `GPUDetectionEngine` 是核心。
改寫目標：**保留所有演算法邏輯，只換掉輸入介面**。

```cpp
// ip/src/gpu/gpu_pipeline.cpp
// 遷移自 Reference/gpu_algo/src/batch_detector.cpp (GPUDetectionEngine class)
// 變更：移除 FileReceiver/SocketReceiver/RivermaxReceiver，改為純函式呼叫介面
// 保留：GPU 記憶體管理、8-Way kernel 呼叫、CCL、BlobStats、AI、ResultWriter 等全部不變

#include "gpu_pipeline.h"
#include "cuda_kernels.h"               // ← 直接複製來的，不改
#include "ai_classifier.h"              // ← 直接複製來的，不改
#include "config_parser.h"              // ← 直接複製來的，不改

// 原 GPUDetectionEngine 的私有成員完全保留
class GpuPipeline {
public:
    // 新介面：接受已在 CPU 記憶體的 uint8 影像 + ZoneConfig
    // 原本的 detect_from_file() / detect_from_socket() 全部移除
    // 改用此函式被 offline_processor.cpp / rdma_source.cpp 呼叫
    DetectionResult process_frame(
        const uint8_t* image_data,   // pinned memory 或 mapped memory
        int width, int height,
        const ZoneConfig& zone_cfg)  // 來自 RecipeInfo.xml 解析的 ZoneConfig
    {
        // ★ 以下邏輯直接從 batch_detector.cpp::detect() 搬過來
        // 只改輸入來源，GPU 計算部分一字不改
        upload_to_gpu(image_data, width, height);
        run_8way_kernel(zone_cfg);
        run_ccl();
        run_blob_stats();
        run_ai_filter();
        return build_result();
    }

private:
    // ★ 從 batch_detector.cpp 完整搬過來的私有成員和方法
    GPUMemoryManager mem_mgr_;
    // ... （見 Reference/gpu_algo/src/batch_detector.cpp）
};
```

---

## 4. 第三步：各模式的 image_source（新外殼）

### offline-tcp（Step 1，全新）

```cpp
// ip/src/image_source/tcp_source.cpp
// 無前身，全新撰寫
// Control 透過 TCP 傳來影像 → 解碼 → 呼叫 GpuPipeline::process_frame()
class TcpImageSource : public IImageSource {
    bool next_frame(FrameHeader& hdr, uint8_t* out_buf) override {
        // 接收 JSON header 命令 + binary 影像資料
        // 詳見 control/CLAUDE.md § 10 的 IpClient 傳輸方式
    }
};
```

### rdma-validate（Step 2-3，改寫自 inline_controller.cpp）

```cpp
// ip/src/modes/rdma_validator.cpp
// 遷移自 Reference/gpu_algo/src/inline_controller.cpp 的 RDMA 接收部分
// 移除：GPU 處理、結果寫入
// 保留：RDMA 接收邏輯、FrameHeader 解析
// 新增：CRC 驗證統計、per-camera FPS 計算、回報給 Control
```

### image-capture（Step 4，全新）

```cpp
// ip/src/modes/image_capturer.cpp
// 無前身，全新撰寫
// RDMA 接收 → 驗證 CRC → 存 8-bit 無壓縮 TIFF → 不跑 GPU
```

---

## 5. RecipeInfo.xml 參數對應（ZoneConfig 橋接）

> ⚠️ **考古修正**：舊版本文件寫的 `ThB`/`ThD`/`ZoneSetting` 對應**是錯的**。
> 實際 legacy 配方是序列化的 **`Recipe`**（`ClibCf/Recipe.cs`），每台 IP 一份，內含
> `DetectRoiList`（`List<DetectRoi>`）。`DetectRoi` 的閾值欄位是 **`BrightThreshold`/`DarkThreshold`**，
> **沒有 `ThB`/`ThD`**（`ThB/ThD` 只是裝置端 `CUDAZone` 內部欄位名）。

`DetectRoi`（legacy）→ `ZoneConfig`（gpu_algo KernelParams）對應：

| ZoneConfig / KernelParams | legacy DetectRoi 欄位 | 說明 |
|----------------|-------------------------------|------|
| `BTH` | `BrightThreshold` | 亮缺陷閾值（**僅 `AlgorithmCompare=="DIV"`，嚴格相等，無近似轉換**）|
| `DTH` | `DarkThreshold` | 暗缺陷閾值（同上）|
| `pitch_x` | `PitchX` | 水平週期 |
| `pitch_y` | `PitchY` | 垂直週期 |
| `search_range_x/y` | `SearchX` / `SearchY` | 搜尋範圍 |
| `fast_search_range` | `clamp(SearchY, 0, 2)` | kernel 實際吃的局部搜尋（垂直向）|
| ROI 範圍 | `StartX/StartY/EndX/EndY` | -1 = 全幅；每個 DetectRoi 一個 zone |
| `enable_multiscale`/`enable_lsc`/`block_dim` | （recipe 無）| 取 `default_zone.ini` 預設；block_dim 固定 16×16 |

> `AlgorithmWay`/`PitchTime`/`ChooseAmount`/`Blob*` 在 gpu_algo kernel **無對應 → 忽略並 log**。
> **SUB 模式直接拒絕**（見不變式 9）。

實作見 `ip/src/config/zone_config_adapter.cpp::from_recipe_xml()`：解析每個 `<DetectRoi>`，
驗證 `AlgorithmCompare=="DIV"`（否則丟 `RecipeError`），回傳 `std::vector<ZoneConfig>`（多 zone）。

---

## 6. 程式碼結構

```
ip/
├── CLAUDE.md
├── CMakeLists.txt
└── src/
    ├── main.cpp                         ← 🆕 進入點（模式分派）
    ├── config/
    │   ├── config_parser.h/.cpp         ← ✅ 從 gpu_algo/include/ 直接複製
    │   ├── inline_types.h               ← ✅ 直接複製
    │   ├── zone_config_adapter.cpp      ← 🆕 XML→ZoneConfig 轉換
    │   └── default_zone.ini             ← ✅ 從 config.ini 直接複製
    ├── gpu/
    │   ├── cuda_kernels.h/.cu           ← ✅ 直接複製（不改任何 kernel）
    │   ├── gpu_pipeline.h/.cpp          ← 🔧 改自 batch_detector.cpp
    │   └── gpu_memory_manager.h/.cpp    ← 🔧 改自 batch_detector.cpp 內的記憶體管理
    ├── ai/
    │   ├── ai_kernels.cu                ← ✅ 直接複製 tensor_core_classifier.cu
    │   ├── ai_classifier.h              ← ✅ 直接複製
    │   ├── rf_model_config.h            ← ✅ 直接複製
    │   └── ai_inference.cpp             ← 🔧 改外殼：加 ONNX Runtime 後端
    ├── image_source/
    │   ├── image_source.h               ← 🆕 IImageSource 介面
    │   ├── tcp_source.cpp               ← 🆕 offline-tcp（Step 1）
    │   ├── file_source.cpp              ← 🆕 offline-file（Step 1 批次）
    │   └── rdma_source.cpp              ← 🔧 改自 t40_e2e_server + inline_controller
    ├── modes/
    │   ├── offline_processor.cpp        ← 🔧 改自 batch_detector 的 offline 流程
    │   ├── rdma_validator.cpp           ← 🔧 改自 inline_controller RDMA 接收
    │   └── image_capturer.cpp           ← 🆕 Step 4 存圖
    ├── align_engine.h/.cpp              ← 🆕 OpenCV Pattern Match（取代 MIL）
    ├── control_server.h/.cpp            ← 🆕 TCP JSON server
    ├── result_saver.h/.cpp              ← 🔧 改自 batch_detector ResultWriter
    └── tests/
        ├── tdd_runner.cpp               ← 🔧 改自 gpu_algo/src/tdd_runner.cpp
        └── tdd/                         ← ✅ 直接複製 gpu_algo/include/tdd/
```

---

## 7. CMakeLists.txt

```cmake
cmake_minimum_required(VERSION 3.24)
project(cfaoi_ip LANGUAGES CXX CUDA)
set(CMAKE_CXX_STANDARD 17)

if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES)
    set(CMAKE_CUDA_ARCHITECTURES "native")
endif()
# RTX 2080 Super: -DCMAKE_CUDA_ARCHITECTURES=75
# DGX Spark:      -DCMAKE_CUDA_ARCHITECTURES=121

find_package(CUDAToolkit REQUIRED)
find_package(OpenCV REQUIRED COMPONENTS core imgproc imgcodecs)
find_package(nlohmann_json REQUIRED)
find_package(fmt REQUIRED)

# ONNX Runtime（offline AI，x86）
find_path(ORT_INCLUDE onnxruntime_cxx_api.h PATHS /opt/onnxruntime/include QUIET)
find_library(ORT_LIB onnxruntime PATHS /opt/onnxruntime/lib QUIET)
if(ORT_LIB) add_compile_definitions(CFAOI_HAS_ORT) endif()

# libibverbs（rdma-validate/image-capture/online）
find_library(IBVERBS_LIB ibverbs QUIET)
if(IBVERBS_LIB)
    add_compile_definitions(CFAOI_HAS_RDMA)
    message(STATUS "RDMA enabled → Step 2-5 modes available")
else()
    message(STATUS "No RDMA → Step 1 (offline) only")
endif()

# 核心可執行檔
add_executable(cfaoi_ip
    src/main.cpp
    src/gpu/gpu_pipeline.cpp
    src/gpu/cuda_kernels.cu        # ← 直接複製來的，不改
    src/ai/ai_kernels.cu           # ← 直接複製來的，不改
    src/ai/ai_inference.cpp
    src/config/zone_config_adapter.cpp
    src/image_source/tcp_source.cpp
    src/image_source/file_source.cpp
    $<$<BOOL:${IBVERBS_LIB}>:src/image_source/rdma_source.cpp>
    $<$<BOOL:${IBVERBS_LIB}>:src/modes/rdma_validator.cpp>
    $<$<BOOL:${IBVERBS_LIB}>:src/modes/image_capturer.cpp>
    src/modes/offline_processor.cpp
    src/control_server.cpp
    src/result_saver.cpp
    src/align_engine.cpp
)
target_include_directories(cfaoi_ip PRIVATE src/ ${ORT_INCLUDE} ${CUDA_INCLUDE_DIRS})
target_link_libraries(cfaoi_ip PRIVATE
    ${CUDA_LIBRARIES} ${OpenCV_LIBS} nlohmann_json::nlohmann_json fmt::fmt
    $<$<BOOL:${ORT_LIB}>:${ORT_LIB}>
    $<$<BOOL:${IBVERBS_LIB}>:${IBVERBS_LIB}>
    $<$<BOOL:${IBVERBS_LIB}>:rdmacm>
)
set_property(TARGET cfaoi_ip PROPERTY CUDA_SEPARABLE_COMPILATION ON)
```

---

## 8. 各平台支援模式

| 平台 | GPU | sm | CUDA | 支援模式 | 記憶體策略 |
|------|-----|----|----|---------|----------|
| Linux RTX 2080 Super | RTX 2080S | 75 | 12.x | offline-tcp/file, rdma-validate*, image-capture* | discrete_async |
| DGX Spark (ARM) | GB10 | 121 | 13.0 | 所有模式 | zero_copy_mapped |
| Windows RTX | 任何 | native | 12.x | offline-tcp/file | discrete_async |

*需要 libibverbs

---

## 9. 不變式

1. `cuda_kernels.cu` 和 `ai_kernels.cu` **禁止修改任何 `__global__` kernel 邏輯**
   （host 端 launch wrapper 的編排可改 → 見不變式 7）
2. `config.ini` 的參數名稱在 `ZoneConfigAdapter` 中必須有完整對應
3. 影像載入：`cv::IMREAD_UNCHANGED`，禁止任何後處理
4. `image-capture` 存圖：8-bit 無壓縮 TIFF（`IMWRITE_TIFF_COMPRESSION=1`）
5. 同一影像跑兩次結果 bit-exact，否則是 bug
6. `block_dim=16×16`，`MAX_UNIQUE_LABELS=65536`，`MAX_DEFECTS=10000` 不可改
7. **CCL 收斂迴圈（gpu-ccl-nondeterminism）**：`cuda_kernels.cu` 的 host wrapper
   `launchFastCCLKernel` 內含「迴圈呼叫 `kernelFastCCLMerge` 直到 `d_changed==0`」的收斂迴圈。
   reference 原版**只跑一次未收斂**的 lock-free union-find → 缺陷數會隨 thread-race 飄動
   （曾觀察 2761/2762/2763）且大 blob 漏合併。收斂後 atomicMin 的不動點唯一 → bit-exact + 正確合併
   （正確數為 2606）。
   ⚠️ **若從 `Reference/gpu_algo/` 重新複製 kernel，此迴圈會被覆蓋，決定性會壞 → 必須重新加回。**
   （只改 host wrapper 的編排；`__global__` kernel 本體一字不改，符合不變式 1。）
8. **缺陷排序（bit-exact 前置條件）**：blob analysis 用 `atomicAdd` append，陣列順序隨 race 變動
   （集合相同、順序不定）。`GpuPipeline::process_frame` 下載後**必須依 canonical key
   （`label` → `center_y` → `center_x` → `size`）排序**，否則輸出順序非決定 → 破壞不變式 5。
9. **DIV-only 閾值（ip-div-only-threshold）**：`from_recipe_xml` **只接受 `AlgorithmCompare="DIV"`**，
   其餘（SUB／缺 tag）一律報錯拒絕。`BTH = BrightThreshold`、`DTH = DarkThreshold` **嚴格相等對應，
   不做任何近似轉換**。（SUB 灰階差轉比例需依賴背景灰階，無固定公式。）
