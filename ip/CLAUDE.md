# IP 程式 — CLAUDE.md

> 先讀 `../CLAUDE.md` 了解遷移策略，再讀本文件。
> **核心原則：GPU kernel 直接複製 `Reference/Demo/`，只換 I/O 外殼。**

---

## 1. 從 Reference 遷移的檔案對照表

| Reference 來源 | ip/src/ 目標 | 處理方式 |
|---------------|-------------|---------|
| `Demo/src/cuda_kernels_fast.cu` | `gpu/cuda_kernels.cu` | ✅ **直接複製，不改** |
| `Demo/src/tensor_core_classifier.cu` | `ai/ai_kernels.cu` | ✅ **直接複製，不改** |
| `Demo/include/cuda_kernels.h` | `gpu/cuda_kernels.h` | ✅ 直接複製 |
| `Demo/include/tensor_core_classifier.h` | `ai/ai_classifier.h` | ✅ 直接複製 |
| `Demo/include/config_parser.h` | `config/config_parser.h` | ✅ 直接複製 |
| `Demo/include/inline_types.h` | `config/inline_types.h` | ✅ 直接複製 |
| `Demo/config/config.ini` | `config/default_zone.ini` | ✅ 直接複製（參數名稱保留）|
| `Demo/src/batch_detector.cpp` | `gpu/gpu_pipeline.cpp` | 🔧 改外殼（移除 FileReceiver，加純函式入口）|
| `Demo/src/inline_controller.cpp` | `modes/rdma_validator.cpp` | 🔧 抽出 RDMA 接收邏輯 |
| `Demo/src/tdd_runner.cpp` | `tests/tdd_runner.cpp` | 🔧 保留診斷邏輯 |
| `cfaoi_phase1/src/t40_e2e_server/` | `image_source/rdma_source.cpp` | 🔧 升級為生產等級 |
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
cp ../Reference/Demo/src/cuda_kernels_fast.cu    src/gpu/cuda_kernels.cu
cp ../Reference/Demo/src/tensor_core_classifier.cu src/ai/ai_kernels.cu
cp ../Reference/Demo/include/cuda_kernels.h      src/gpu/cuda_kernels.h
cp ../Reference/Demo/include/tensor_core_classifier.h src/ai/ai_classifier.h
cp ../Reference/Demo/include/config_parser.h     src/config/config_parser.h
cp ../Reference/Demo/include/inline_types.h      src/config/inline_types.h
cp ../Reference/Demo/include/rf_model_config.h   src/ai/rf_model_config.h
cp ../Reference/Demo/config/config.ini            config/default_zone.ini

# 複製 TDD 測試基礎設施
cp -r ../Reference/Demo/src/tests/              src/tests/
cp -r ../Reference/Demo/include/tdd/            src/tests/tdd/
```

---

## 3. 第二步：改寫 gpu_pipeline.cpp（外殼替換）

`Reference/Demo/src/batch_detector.cpp` 的 `GPUDetectionEngine` 是核心。
改寫目標：**保留所有演算法邏輯，只換掉輸入介面**。

```cpp
// ip/src/gpu/gpu_pipeline.cpp
// 遷移自 Reference/Demo/src/batch_detector.cpp (GPUDetectionEngine class)
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
    // ... （見 Reference/Demo/src/batch_detector.cpp）
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
// 遷移自 Reference/Demo/src/inline_controller.cpp 的 RDMA 接收部分
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
> 實際 legacy 配方是序列化的 **`Recipe`**（`ClibCf/Recipe.cs`），每台 IP 一份，結構：
> **`Recipe → M_AlignRoi + DetectRoiList(List<DetectRoi>，每個 32 欄) + DetectIoiList`**。
> `DetectRoi` 的閾值欄位是 **`BrightThreshold`/`DarkThreshold`**，**沒有 `ThB`/`ThD`**
> （`ThB/ThD` 只是裝置端 `CUDAZone` 內部欄位名，由 CPU 端 `ThB=(float)BrightThreshold` 直接賦值）。

`DetectRoi`（legacy）→ `ZoneConfig`（Demo KernelParams）對應：

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

> `AlgorithmWay`/`PitchTime`/`ChooseAmount`/`Blob*` 在 Demo kernel **無對應 → 忽略並 log**。
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
    │   ├── config_parser.h/.cpp         ← ✅ 從 Demo/include/ 直接複製
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
    ├── diag/
    │   └── flight_recorder.h/.cpp       ← 🆕 行車紀錄（結構化診斷 JSONL/incident，見不變式 16）
    ├── align_engine.h/.cpp              ← 🆕 OpenCV Pattern Match（取代 MIL）
    ├── control_server.h/.cpp            ← 🆕 TCP JSON server
    ├── result_saver.h/.cpp              ← 🔧 改自 batch_detector ResultWriter
    └── tests/
        ├── tdd_runner.cpp               ← 🔧 改自 Demo/src/tdd_runner.cpp
        └── tdd/                         ← ✅ 直接複製 Demo/include/tdd/
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
   ⚠️ **若從 `Reference/Demo/` 重新複製 kernel，此迴圈會被覆蓋，決定性會壞 → 必須重新加回。**
   （只改 host wrapper 的編排；`__global__` kernel 本體一字不改，符合不變式 1。）
8. **缺陷排序（bit-exact 前置條件）**：blob analysis 用 `atomicAdd` append，陣列順序隨 race 變動
   （集合相同、順序不定）。`GpuPipeline::process_frame` 下載後**必須依 canonical key
   （`label` → `center_y` → `center_x` → `size`）排序**，否則輸出順序非決定 → 破壞不變式 5。
9. **DIV-only 閾值（ip-div-only-threshold）**：`from_recipe_xml` **只接受 `AlgorithmCompare="DIV"`**，
   其餘（SUB／缺 tag）一律報錯拒絕。`BTH = BrightThreshold`、`DTH = DarkThreshold` **嚴格相等對應，
   不做任何近似轉換**。（SUB 灰階差轉比例需依賴背景灰階，無固定公式。）
10. **output 同 panel 重測前清空（避免 DefectSort 殘留疊加）**：`result_saver::save` 每次存圖**無條件**
    先清掉該 panel 夾本層舊 `Defect_*` 與舊 `.bmp`（不動 `TrueDefect/`、`Particle/` 子夾與 `classification.json`）。
    否則跨次 Test 換 `--ip-name`（IP01→IP02）或換參數會產生不同檔名 → 堆疊成 N 倍（曾見 1122=561×2，
    座標完全相同重複兩次）。單次乾淨偵測恆 1:1：`DetectionResult 缺陷數 == JSON DefectInfo 筆數 == 寫出 patch 數`
    （`[Diag]` log 印此三數）。
11. **network-clean（跨機免共用檔案系統）**：Control↔IP 為 Mac↔Linux 不同機器、無共用磁碟。
    `LOAD_RECIPE` 傳**配方 XML 內容**（`recipe_xml`，非路徑）；`SEND_IMAGE_FOR_REVIEW` 結果 JSON
    經 TCP 回傳（`deliver_result` rendezvous）；缺陷小圖以 PNG bytes（base64）經
    `GET_DEFECT_PATCHES_BATCH` 回傳。IP 不讀寫對方硬碟，反之亦然。
12. **缺陷檔名 IpName 段取自 panel 名前綴**（`result_saver`：panel 第一個 '_' 前 token，例
    `IP02_Origin000001` → `Defect_IP02_...`），與資料夾一致；不要用固定 `--ip-name` 硬寫死（那是 fallback）。
13. **AI 預設停用**（訓練資料不足）：模型仍載入（保留架構）但 `set_ai_active(false)`，不推論、不過濾，
    缺陷 `AiType="待人工複核"`；`--use-ai` 重啟。缺陷分類靠 DefectSort 人工標 TrueDefect/Particle。
14. **Pitch 正確性至關重要（爆量陷阱）**：`PitchX`/`PitchY` 必須精確匹配面板實際網格週期，
    **偏差 1~4 px 就會讓比例式演算法把正常網格當缺陷而爆量**（實測 `PitchX 26→30` → 缺陷 `561→10000`(觸頂 MAX_DEFECTS)）。
    新面板務必先用 **FFT 估算**（`scripts/estimate_pitch.py` 或 Control Step1 的「FFT 估算」鈕）確認 Pitch，
    **不可沿用舊面板值或用猜的**。缺陷數異常暴增（接近 10000 觸頂）時，第一個要懷疑的就是 Pitch 設錯。
15. **GB10 效能基準與 block_dim（gb10-perf-baseline）**（2026-06-15 實機 bench，見
    `docs/verification/verification_report_arm_20260615.md`）：**`block_dim` 固定 16×16** —— `zone_config_adapter.cpp`
    `from_ini`（L69-70）硬寫死 `block_dim_x=block_dim_y=16`，**完全忽略 INI 的 `[GPU] block_dim`（32×32 為死設定）**；
    RAG_TRAINING.md §5.2 建議的 16×16 已是現狀、32×32 從未被執行，改 INI 不影響 GPU block 維度。
    GB10 正常面板（個位數缺陷）**~7.4ms/張**（cudaEvent median；乾淨 0 缺陷 6.95ms，皆於 16×16），
    `1110 張 × 7.4ms = 8.2s/面板 < 30s 節拍` → **1 台 Spark 足夠**（G8.5 37 相機陣列，餘裕 ~73%）。
    vs reference Demo 同影像 4.9ms 慢 **1.5×**，是 **CCL 收斂迴圈（不變式 7）+ zero-copy mapped 讀 + canonical
    排序（不變式 8）的決定性代價**，非效能 bug；gpu_ms 隨缺陷量 scaling（爆量觸頂 ~14ms）證實此 kernel 記憶體頻寬綁定。
16. **行車紀錄純觀測，不得擾動運算（flight-recorder-observe-only）**：`diag/flight_recorder.cpp` 平時零磁碟
    I/O（最近 64 張現場進記憶體環形緩衝），出事才落地 `<output>/_diag/<yyyyMMdd>.jsonl`（每事件一行 compact 索引）
    + `incident_<ts>.json`（完整現場 pretty-print）。**`record_frame` 在 `cudaEvent` 計時區外呼叫**、
    `set_scene` 寫 ring 只鎖極短（小 struct 複製）→ 不影響 `gpu_ms`/bit-exact 決定性（不變式 5）。
    **bench 模式不呼叫 `begin_session` → `enabled_=false` → 所有方法 no-op**（bench `process_image` 路徑無任何
    scene hook，gpu_ms 零擾動）。跨執行緒抓現場用全域 `std::atomic<const FrameScene*> latest_`（非 thread_local）：
    `CUDA_CHECK` 的 `exit()` 觸發 `std::atexit` handler，讀 `latest_` + `cudaPeekAtLastError()` dump `cuda_fatal`；
    `std::set_terminate` dump `uncaught_exception`。incident kind：`cuda_fatal`/`frame_validation`/`recipe_load`/
    `bad_json`/`uncaught_exception`。（2026-06-15 RTX 2080 五種 kind + 決定性 + bench-noop 全驗證。）
17. **收圖入口驗證（frame-ingress-validate）**：`control_server.cpp` 收圖入口驗證
    **magic/version/headerBytes/payloadBytes + payload CRC32**（用 `shared/FrameHeader.h::crc32_ieee`）+ **尺寸防呆**
    （width/height ∈ [1, 16384]，擋 bogus 尺寸→巨量配置→OOM）；offline-tcp 另支援 client 在 `SEND_IMAGE_FOR_REVIEW`
    可選帶 `crc32` 宣告值比對偵測傳輸損壞。**任一失敗 → 記 `frame_validation` incident + 回 ERR 拒收（不 enqueue）**。
    （offline-tcp header 為本地建構故 magic 等恆對，wire 驗證主擋未來 RDMA 收圖路徑；該 RDMA 分支待 `rdma_source` 實作後才實際生效。）
18. **FrameQueue / SourceRing 固定上限，啟動後不可動態增大**：`max_size_` 由 buffer 計算器（`sysinfo().freeram` 在 GPU 持久
    buffer 配完後才查）設置一次；push() 返回 bool，滿則背壓/拒收/drop + `queue_overflow` incident，
    **絕不動態配更多記憶體**（根治舊版 OOM 炸彈：legacy CamProc per-frame 配 List → 累積 → OOM）。
    FrameQueue 上限 = 50% 可用 host RAM / 幀大小（最多 8 幀）；SourceRing 上限 = 30% / 幀大小（最多 4 幀）。
    `cudaMemGetInfo` = device RAM；`sysinfo().freeram` = host RAM；**兩者絕不混用**。
19. **SaveSourceImage async writer，絕不同步阻塞主路徑、絕不囤 List**：原始 payload 非同步寫磁碟用
    `SourceImageWriter`（`image_source/source_image_writer.h`）：固定 N_src ring slots（啟動一次配置）+ 獨立 writer thread；
    ring 滿 → drop + `source_ring_full` incident，主路徑繼續不阻塞。格式：raw `.bin`（Mono8，比 PNG 快 5-10×）。
    由 `LOAD_RECIPE share_flags.save_source_image=true` 啟用。
20. **TuningRecipe 不寫磁碟但 deliver_result 不變**：`LOAD_RECIPE share_flags.tuning_recipe=true` →
    GPU 正常跑、`deliver_result()` 結果仍經 TCP 回傳 Control，但 `ResultSaver::save()` 完全跳過（零磁碟寫入）。
    log 印 `[TuningRecipe] 跳過存圖（結果仍回傳）`。**不可把 TuningRecipe 當 bench**（bench 無 TCP server，是另一模式）。
21. **MaxDefectCountPass 截斷不破壞決定性（不變式 5）**：`LOAD_RECIPE recipe_saving.max_defect_count_pass` 設上限。
    截斷只在整個 zone 的 `process_frame()` 完成後（GPU CCL 已收斂 + canonical 排序完）用整數比較；
    `break` 在 zone 迴圈層（host 端）。兩跑累積缺陷數相同 → break 在同一 zone → 輸出 bit-exact。
    `--verify-deterministic` 須涵蓋「缺陷數剛好等於上限」與「剛好超過」兩種邊界 case。
22. **RecipeSaving 欄位 -1 = 向下相容**：`max_save_defect_count=-1` 等同現行 `max_patches=-1`（無上限）；
    `save_defect_width/height` 預設 100px；`max_defect_count_pass=-1` = 不截斷。
    LOAD_RECIPE 若無 `recipe_saving` 欄位，IP 保留前次設定（session 初始值為全 -1 預設）。
23. **RDMA credit 背壓（rdma-credit-backpressure）**（Step 3，`image_source/rdma_source.cpp`）
    **[2026-06-17 damac↔Spark 實機驗通]**：
    IP 預掛 N 個 `post_recv`（= N 個初始 credit）；`recv_thread` 正確順序為
    `[1] memcpy slot → payload → [2] push_blocking（等 FrameQueue 有位置）→ [3] post_recv（補 credit）`。
    此順序不可調換：post_recv 在 push_blocking 之後，保證 Grab 在 IP 讀完 slot 前不能重用該 slot。
    credit 耗盡 → Grab `WRITE_WITH_IMM` 觸發 RNR（`rnr_retry_count=7=∞`）→ Grab `poll_one()` 阻塞
    → 自然背壓，無需額外控制通道。C++17 happens-before 語意保證順序，不需額外 `std::atomic_thread_fence`。
    N-slot ring：`slot_id = frame_seq % n_slots`，N 個連續幀佔 N 個不同 slot，credit 確保 in-flight ≤ N，
    不可能出現 slot 覆蓋。`--rdma-slots` 預設 4（4×40MB=160MB host pinned memory）。
    **實測數據（2026-06-17）**：Phase 1 連續 120 幀 CRC=OK、1375fps/86MB/s、slot 繞回正確；
    Phase 2 背壓（--test-consumer-delay-ms 200）→ Grab 降至 9.6fps（非斷線）、QP 未進 error state。
    **注意**：RoCE v2（非 IB）Grab 斷線後 `IBV_WC_WR_FLUSH_ERR` **不保證立即出現**；
    需在 recv_thread no-event 分支輪詢 CM event channel（`check_cm_disconnect()`），
    否則 recv_thread 永不退出，`queue_->close()` 永不呼叫，主迴圈 pop 永久阻塞（commit `de047a3` 修正）。
24. **對位 pipeline（Gap #1）— 實作完成、待實機驗證（L1→目標 L3）**（2026-06-17）：
    - **流程**：每片一次（CF_GRAB_START 觸發），`CHECK_ALIGN` 收搜尋 ROI（Control 端裁 500×500≈250KB）→
      `run_align()`（13 角 × TM_CCOEFF_NORMED + 拋物線 sub-pixel）→ 回 ShiftX/Y；
      `SET_ALIGN` 套回所有 zones（`aligned_* = roi_* + round(shift_*)`）；
      偵測路徑統一用 `eff_*()`（aligned_* ≥ 0 則用對位值，否則 fallback roi_*）。
    - **失敗策略**：score < threshold → `CHECK_ALIGN` 回 ERR，Control 回 CF_CHECK_ALIGN ERR 給上位機，
      由上位機決策（停線/放行/重試），IP/Control 不自行 fallback 繼續（釘點 3）。
    - **Golden**：Control 端讀 `PatternPath`（相對 recipe 目錄），base64 嵌入 LOAD_RECIPE `golden_png_base64`，
      IP 在記憶體 decode（不寫磁碟，network-clean 不變式 8）；LOAD_RECIPE 覆蓋舊 golden + 重設 `aligned_* = -1`。
    - **旋轉**：可配置（`AlignRoiConfig.angle_range_deg` 預設 ±3°, step 0.5°）。
    - **AlignEnable=false 行為不變**：`eff_*` fallback roi_*，偵測路徑無感知。
    - **2026-06-17 DGX Spark GB10 實機驗通（L3）**：
      - Stage 1 `align_verify` 14/14 PASS：sub-pixel 誤差全 <0.1px（最差 L2=0.087px）；旋轉角度誤差 0.000°。
      - Stage 2 `verify_alignment.py` 8/8 PASS：n0=7 缺陷基準；面板偏移 7px→CHECK_ALIGN ShiftX=7.001 誤差<0.001px→SET_ALIGN→偵測 n_aligned=7＝n0（對位後缺陷數不變，不爆量）。
      - Stage 3A 空白 ROI → ok=false ERR + score=0.000；eff_* fallback/套回邏輯全 PASS。
