# IP 程式 — CLAUDE.md

> 先讀 `../CLAUDE.md` 了解遷移策略，再讀本文件。
> **核心原則：GPU kernel 直接複製 `Reference/Demo/`，只換 I/O 外殼。**

---

## 1. 從 Reference 遷移的檔案對照表

| Reference 來源 | ip/src/ 目標 | 處理方式 |
|---------------|-------------|---------|
| `Demo/src/cuda_kernels_fast.cu` | `gpu/cuda_kernels.cu` | ✅ Demo kernel 區**直接複製不改**（⚠️ SUB 移植後檔內**另新增 5 個非 Demo kernel**，見 §2 註）|
| `Demo/src/tensor_core_classifier.cu` | `ai/ai_kernels.cu` | ✅ **直接複製，不改** |
| `Demo/include/cuda_kernels.h` | `gpu/cuda_kernels.h` | ✅ 複製＋🆕 新增 SUB/voting/前處理 wrapper 宣告 |
| `Demo/include/tensor_core_classifier.h` | `ai/tensor_core_classifier.h` | ✅ 直接複製（**非** `ai_classifier.h`）|
| `Demo/include/config_parser.h` | `config/config_parser.h` | ✅ 直接複製 |
| `Demo/include/inline_types.h` | `config/inline_types.h` | ✅ 直接複製 |
| `Demo/config/config.ini` | `config/default_zone.ini` | ✅ 直接複製（參數名稱保留）|
| `Demo/src/batch_detector.cpp` | `gpu/gpu_pipeline.cpp` | 🔧 改外殼（GPUMemoryManager 內嵌本檔；移除 FileReceiver，加純函式入口）|
| `cfaoi_phase1/src/t40_e2e_server/` | `image_source/rdma_source.cpp` | 🔧 升級 N-slot **SEND/RECV** + credit 背壓（rdma-validate / rdma-process 共用）|
| — | `image_source/tcp_source.cpp` | 🆕 全新（offline-tcp，Step 1）|
| — | `image_source/file_source.cpp` | 🆕 全新（offline-file）|
| — | `image_source/source_image_writer.h` | 🆕 SaveSourceImage 非同步 ring writer（不變式 19）|
| — | `image_source/rdma_common.h` | 🆕 RC 連線樣板（與 `grab/src/rdma_common.h` **同源雙副本，兩份須同步改**）|
| — | `control_server.cpp` | 🆕 全新（TCP JSON server，8200，15 命令）|
| — | `align_engine.cpp` / `edge_check.cpp` / `defect_rules.h` / `diag/flight_recorder.cpp` | 🆕 對位 / 玻璃邊健檢 / CPU 後處理 / 行車紀錄 |

> ⚠️ 舊版本文件曾列 `modes/rdma_validator.cpp`、`modes/image_capturer.cpp`、`tests/tdd_runner.cpp`
> ——**皆未落地**。RDMA 模式分派全在 `main.cpp`（見 §4/§6/§8），無 `modes/`、`tests/` 目錄。

---

## 2. 第一步：複製 CUDA Kernels

> ⚠️ **歷史 bootstrap 紀錄**（首次遷移的指令，保留供考古）：實際落地後 `tests/`、`src/tests/tdd/`
> 未建立、`tensor_core_classifier.h` 落在 `src/ai/`（非 `ai_classifier.h`）。現行實際檔案樹以 §6 為準。
>
> ⚠️ **cuda_kernels.cu 已非純複製**：SUB 管線移植（2026-06-23）後檔內**另新增 5 個非 Demo kernel**
> （`kernelSub8WayStarVoting` / `kernelDivVoting8WayStar` / `kernelHistogram256` / `kernelRemapStretch` /
> `kernelSmooth3x3Gau8`，`cuda_kernels.cu:1125-1361`）＋對應 host wrapper。
> **既有 Demo kernel 區仍禁改**（不變式 1 不變）。

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

> ⚠️ 下方為**遷移時的設計示意碼**（含 `ai_classifier.h` 舊名），非現行原始碼；現行 `run()` 另含
> 三域 dispatch（DIV/SUB/DIV-voting）、SUB 前處理（remap/smooth）、mode2 多尺度、canonical 排序，
> 以 `ip/src/gpu/gpu_pipeline.cpp` 為準。

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

### rdma-validate（Step 2-3，main.cpp 模式分支）

無獨立 `modes/` 檔：`main.cpp`（`mode=="rdma-validate"` 分支）＋ `image_source/rdma_source.cpp`。
RDMA N-slot 接收 → seq 連續性檢查 + 二次 CRC 驗證統計（`CFAOI_RDMA_NOCRC=1` 兩端同步跳過）。
8200 亦開（心跳/GET_STATUS 要通）；LOAD_RECIPE 誠實 ERR（本模式無檢測管線）。

### rdma-process（Step 4/5 現行承接；**無獨立 image-capture / online 模式**）

`main.cpp`（`mode=="rdma-process"` 分支）：RDMA 收圖 → FrameQueue → 與 offline 相同的
`process_image`（GPU 檢測）→ `ResultSaver`。Step 4 存圖與 Step 5 生產由**同一模式＋存圖控制**承接：
- `share_flags.save_source_image` → 原始 payload 非同步落盤（`source_image_writer.h`，不變式 19）
- `overlay_on_defect_only`（生產預設）→ 0 缺陷不寫 overlay（8160×5000 PNG 實測 ~410ms/幀）
- 串流中 8200 可用：LOAD_RECIPE（`zones_mtx` 換配方）/ SET_ALIGN / GET_STATUS / DefectSort 查詢；
  影像注入命令（SEND_IMAGE_* / REVIEW_LOCAL_IMAGE）誠實 ERR 擋掉（來源是 RDMA）
- 含收圖遺失對帳（`frame_loss` → ResultInfo.json `panel_incomplete`）與逐 slice edge_check

---

## 5. RecipeInfo.xml 參數對應（ZoneConfig 橋接）— 三域守門（2026-06-23 SUB 管線移植後，取代舊「DIV-only」）

> ⚠️ **考古修正**：舊版本文件寫的 `ThB`/`ThD`/`ZoneSetting` 對應**是錯的**。
> 實際 legacy 配方是序列化的 **`Recipe`**（`ClibCf/Recipe.cs`），每台 IP 一份，結構：
> **`Recipe → M_AlignRoi + DetectRoiList(List<DetectRoi>，每個 32 欄) + DetectIoiList`**。
> `DetectRoi` 的閾值欄位是 **`BrightThreshold`/`DarkThreshold`**，**沒有 `ThB`/`ThD`**
> （`ThB/ThD` 只是裝置端 `CUDAZone` 內部欄位名，由 CPU 端 `ThB=(float)BrightThreshold` 直接賦值）。

### 演算法域守門（`zone_config_adapter.cpp:120-216`）

權威欄位是 **`<M_AlgorithmWayCompare>`**（legacy `CamProc.cs:501-543` 以 enum 為準覆蓋字串）；
`<AlgorithmCompare>` 是 **stale 欄位**——舊守門只比 `AlgorithmCompare="DIV"`，曾被
「掛 DIV 字串、實為 SUB」的 recipe 騙過 → **靜默假 PASS（血淚教訓，守門存在的理由，不可回退）**。判定：

- awc 含 `Sub` → **`algo_mode=1` SUB**（灰階差 8-Way-Star 投票；BTH/DTH 為灰階差如 +17/−16；
  另讀 `PitchTime`/`ChooseAmount`；前處理鏈 `M_ImagePreproc(Ip_Remap)` → 3×3×`SmoothTimes2`
  ——5×5×`SmoothTimes` 尚未支援，僅解析存欄）
- awc 含 `div` 且帶投票結構標記（`star`/`way`，如 `Awc_8_Way_Star_Div`）→ **`algo_mode=2` DIV-voting 融合**
  （比值域 BTH>1>DTH>0；`MeanLowThreshold`=暗區棄權 dark_eps；`EnableMultiscale` 多尺度、`Lsc*` 校正；
  多尺度僅 mode2 生效、remap 僅 mode1——remap 減 min 會破壞 DIV 比值照度不變性）
  ⚠️ **路由語意待裁示（2026-08-02 複查）**：legacy enum 值全為 `Awc_*_Way_*_Div`（**皆含 "Way"、
  legacy 並無 `Awc_8_Way_Star_Div`**）→ 任何帶 awc 的 legacy DIV recipe 都命中此分支進 mode2
  （非已驗 mode0），且 mode2 連帶吃 `EnableMultiscale` ini 預設 1。mode0 幾乎只剩
  「無 awc + `AlgorithmCompare="DIV"`」可達。與 docs/CLAUDE.md §5 同步。
- awc 含 `div`（無投票標記）或 `AlgorithmCompare="DIV"` → **`algo_mode=0` DIV**（Demo 比例式 kernel；
  **防呆：標 DIV 但 `DarkThreshold<0` = SUB 灰階差域值誤標 → 拒載**）
- 皆判不出 → **拒載報錯（不靜默預設）**。CLI `--algo-mode 0/1/2`/`--multiscale`/`--dark-eps` 可覆寫（驗證用）。

### `DetectRoi`（legacy）→ `ZoneConfig` 對應

| ZoneConfig / KernelParams | legacy DetectRoi 欄位 | 說明 |
|----------------|-------------------------------|------|
| `BTH` / `DTH` | `BrightThreshold` / `DarkThreshold` | 同名欄位**依域解讀**（DIV/mode2=比值、SUB=灰階差；守門判定，見上）；**不做跨域近似轉換** |
| `pitch_x` / `pitch_y` | `PitchX` / `PitchY` | 水平 / 垂直週期 |
| `search_range_x/y` | `SearchX` / `SearchY` | 搜尋範圍 |
| `fast_search_range` | `clamp(SearchY, 0, 2)` | mode0 DIV kernel 實吃的局部搜尋（垂直向）|
| `pitch_times` / `choose_amount` | `PitchTime` / `ChooseAmount` | SUB/mode2 投票路數與門檻（mode0 忽略）|
| `mean_low_threshold` | `MeanLowThreshold` | mode2 暗區棄權門檻（dark_eps）|
| `preproc_remap` / `smooth_times2` | `M_ImagePreproc` / `SmoothTimes2` | SUB/mode2 前處理（remap 僅 mode1 執行）|
| `blob_min/max_size`、`blob_merge_distance` | `BlobMinSize`/`BlobMaxSize`/`BlobAllMergeDistance` | Step E CPU 後處理過濾/合併（所有模式，缺省 0=關）|
| `enable_multiscale`、`enable_lsc`+`lsc_*` | `EnableMultiscale`、`LscEnable`+`Lsc*` | 缺省沿用 `default_zone.ini`；多尺度僅 mode2 執行 |
| ROI 範圍 | `StartX/StartY/EndX/EndY` | -1 = 全幅；每個 DetectRoi 一個 zone |
| `block_dim` | （recipe 無）| 固定 16×16（不變式 15）|

實作見 `ip/src/config/zone_config_adapter.cpp::from_recipe_xml(_content)`：解析每個 `<DetectRoi>`，
依上述守門判定 `algo_mode`（判不出丟 `RecipeError`），回傳 `std::vector<ZoneConfig>`（多 zone）；
`<DetectIoiList>` 另由 `parse_ioi_list()` 解析（#23 興趣區，存圖/監看用）。

---

## 6. 程式碼結構

```
ip/
├── CLAUDE.md
├── CMakeLists.txt
├── config/
│   └── default_zone.ini                 ← ✅ 從 config.ini 直接複製（另有 config_optimized/config_real.ini）
└── src/
    ├── main.cpp                         ← 🆕 進入點＋模式分派（offline-file[--stitch] / offline-tcp /
    │                                        bench / rdma-validate / rdma-process 全在本檔；含
    │                                        edge_check 呼叫、frame_loss 對帳、defect_flood 訊號）
    ├── control_server.h/.cpp            ← 🆕 TCP JSON server（8200；LOAD_RECIPE / SET·CHECK_ALIGN /
    │                                        DefectSort 五命令 / LIST_DIR / GET_IMAGE_PREVIEW /
    │                                        REVIEW_LOCAL_IMAGE 等 15 命令）
    ├── result_saver.h/.cpp              ← 🔧 改自 batch_detector ResultWriter（legacy JudgeResult
    │                                        XML+JSON 雙寫、清舊檔、overlay_on_defect_only、frame_loss）
    ├── align_engine.h/.cpp              ← 🆕 OpenCV Pattern Match（取代 MIL；多角度＋sub-pixel）
    ├── edge_check.h/.cpp                ← 🆕 玻璃前緣/尾緣健檢（INI [EdgeCheck]；含逐 slice 模式）
    ├── defect_rules.h                   ← 🆕 CPU 後處理（Step E Blob 過濾/合併＋#32 邊界略過＋#16 Rule 改判）
    ├── config/
    │   ├── config_parser.h              ← ✅ 直接複製（header-only INI 解析）
    │   ├── inline_types.h               ← ✅ 直接複製
    │   ├── zone_config_adapter.h/.cpp   ← 🆕 XML→ZoneConfig（三域守門，見 §5）＋IOI 解析
    │   └── recipe_saving_config.h / share_flags.h / align_roi_config.h ← 🆕 LOAD_RECIPE 附帶設定
    ├── gpu/
    │   ├── cuda_kernels.h/.cu           ← ✅ Demo kernel 區不改＋🆕 5 個 SUB/融合 kernel（見 §2 註）
    │   ├── defect_info.h                ← 🆕 DefectInfo 抽出（CUDA-free，供 CPU 後處理/單元測）
    │   └── gpu_pipeline.h/.cpp          ← 🔧 改自 batch_detector.cpp（**GPUMemoryManager 內嵌本檔**，
    │                                        無獨立 gpu_memory_manager.*；三域 dispatch＋前處理＋
    │                                        多尺度＋CCL＋blob＋canonical 排序）
    ├── ai/
    │   ├── ai_kernels.cu                ← ✅ 直接複製 tensor_core_classifier.cu
    │   ├── tensor_core_classifier.h     ← ✅ 直接複製
    │   └── rf_model_config.h            ← ✅ 直接複製（無 ai_inference.cpp / ONNX 外殼）
    ├── image_source/
    │   ├── image_source.h               ← 🆕 IImageSource 介面＋FrameQueue（背壓＋緩衝回收池）
    │   ├── file_source.h/.cpp           ← 🆕 offline-file（Step 1 批次）
    │   ├── tcp_source.h/.cpp            ← 🆕 offline-tcp（FrameQueue 消費轉接）
    │   ├── rdma_source.h/.cpp           ← 🔧 改自 t40_e2e_server（N-slot SEND/RECV＋credit＋lost 追蹤）
    │   ├── rdma_common.h                ← 🆕 RC 樣板（與 grab/src/rdma_common.h 同源雙副本）
    │   └── source_image_writer.h        ← 🆕 SaveSourceImage 非同步 ring writer（不變式 19）
    ├── diag/
    │   └── flight_recorder.h/.cpp       ← 🆕 行車紀錄（結構化診斷 JSONL/incident，見不變式 16）
    └── align_verify.cpp / edge_verify.cpp / coord_verify.cpp / crc_verify.cpp / rules_verify.cpp
                                         ← 🆕 獨立驗證器（各自編為執行檔，不進 cfaoi_ip）

（無 modes/、無 tests/ 目錄；舊版本文件所列 offline_processor / rdma_validator / image_capturer /
 gpu_memory_manager / ai_inference / tdd_runner 皆不存在。）
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

# ONNX Runtime（offline AI，x86）— 可選（CFAOI_HAS_ORT）
find_path(ORT_INCLUDE onnxruntime_cxx_api.h PATHS /opt/onnxruntime/include QUIET)
find_library(ORT_LIB onnxruntime PATHS /opt/onnxruntime/lib QUIET)

# libibverbs + librdmacm（rdma-validate / rdma-process）— 可選（CFAOI_HAS_RDMA）
find_library(IBVERBS_LIB ibverbs QUIET)
find_library(RDMACM_LIB  rdmacm  QUIET)

# 核心可執行檔（= 現行 ip/CMakeLists.txt 實際清單）
add_executable(cfaoi_ip
    src/main.cpp
    src/gpu/gpu_pipeline.cpp
    src/gpu/cuda_kernels.cu          # ← Demo kernel 區不改；含 5 個新增 SUB/融合 kernel（§2 註）
    src/ai/ai_kernels.cu             # ← 直接複製來的，不改
    src/config/zone_config_adapter.cpp
    src/diag/flight_recorder.cpp
    src/image_source/file_source.cpp
    src/image_source/tcp_source.cpp
    src/result_saver.cpp
    src/control_server.cpp
    src/align_engine.cpp
    src/edge_check.cpp
    $<$<BOOL:${IBVERBS_LIB}>:src/image_source/rdma_source.cpp>
)
target_link_libraries(cfaoi_ip PRIVATE
    CUDA::cudart CUDA::cublas ${OpenCV_LIBS} nlohmann_json::nlohmann_json fmt::fmt Threads::Threads
    $<$<BOOL:${ORT_LIB}>:${ORT_LIB}>
    $<$<BOOL:${IBVERBS_LIB}>:${IBVERBS_LIB}>
    $<$<BOOL:${RDMACM_LIB}>:${RDMACM_LIB}>
)
set_property(TARGET cfaoi_ip PROPERTY CUDA_SEPARABLE_COMPILATION ON)

# 另有 5 個獨立驗證器（各自 add_executable，不進 cfaoi_ip）：
#   crc_verify（ARM64 硬體 CRC ↔ 表格版 wire 相容守門）/ align_verify（Gap #1）/
#   coord_verify（Gap #5 pixel→μm）/ edge_verify / rules_verify（#16/#32）
```

> 以 `ip/CMakeLists.txt` 為準；舊版本文件曾列 `src/ai/ai_inference.cpp`、`src/modes/*.cpp`——皆不存在，勿照抄。

---

## 8. 各平台支援模式

實際模式（`main.cpp` 分派）：`offline-file`（含 `--stitch` 全 panel 拼接）/ `offline-tcp` / `bench` /
`rdma-validate`\* / `rdma-process`\*。**無獨立 `image-capture` / `online` 模式**——Step 4/5 由
`rdma-process` ＋ 存圖控制（SaveSourceImage / overlay_on_defect_only / TuningRecipe）承接（見 §4）。

| 平台 | GPU | sm | CUDA | 支援模式 | 記憶體策略 |
|------|-----|----|----|---------|----------|
| Linux RTX 2080 Super | RTX 2080S | 75 | 12.x | offline-file/tcp, bench, rdma-validate*, rdma-process* | discrete_async |
| DGX Spark (ARM) | GB10 | 121 | 13.0 | 所有模式 | zero_copy_mapped |
| Windows RTX | 任何 | native | 12.x | offline-file/tcp, bench | discrete_async |

*需要 libibverbs（CFAOI_HAS_RDMA）

---

## 9. 不變式

1. `cuda_kernels.cu` 和 `ai_kernels.cu` **禁止修改任何 `__global__` kernel 邏輯**
   （host 端 launch wrapper 的編排可改 → 見不變式 7）
2. `config.ini` 的參數名稱在 `ZoneConfigAdapter` 中必須有完整對應
3. 影像載入：`cv::IMREAD_UNCHANGED`，禁止任何後處理
4. 原始圖存檔：現行走 rdma-process `SaveSourceImage`（raw `.bin` Mono8，不變式 19）；
   （獨立 `image-capture` 模式未實作——若日後補 TIFF 存圖，沿用「8-bit 無壓縮 TIFF
   `IMWRITE_TIFF_COMPRESSION=1`」規則）
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
9. **三域守門（algo-domain-gate，2026-06-23 取代舊「DIV-only」）**：`from_recipe_xml(_content)` 以
   **`M_AlgorithmWayCompare`（enum）為權威**判定 `algo_mode`（0=DIV / 1=SUB / 2=DIV-voting，細節見 §5），
   `AlgorithmCompare` 字串僅 fallback——舊守門只比字串，曾被「掛 DIV 字串、實為 SUB」的 recipe 騙過 →
   **靜默假 PASS（血淚教訓，此守門不可回退）**。**判不出域、或標 DIV 但 `DarkThreshold<0`
   （SUB 灰階差域值誤標）→ 一律拒載報錯，不靜默預設**。`BTH/DTH` 同名欄位依域解讀
   （DIV/mode2=比值、SUB=灰階差），**不做任何跨域近似轉換**（灰階差轉比例需依賴背景灰階，無固定公式）。
   ⚠️ 已知限制：legacy `Awc_*_Way_*_Div` 全含 "Way" → 現行一律路由 mode2、mode0 幾乎不可達，
   路由語意待裁示（見 §5）。
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
    `bad_json`/`uncaught_exception`/`queue_overflow`/`queue_high_watermark`/`source_ring_full`/`rdma_validate`/
    `defect_flood`/`align_fail`/`transport_anomaly`/`frame_loss`（後三種 = edge_check 玻璃邊健檢與
    收圖遺失標記，2026-07 新增，見 main.cpp）。（2026-06-15 RTX 2080 五種 kind + 決定性 + bench-noop 全驗證。）
    **2026-07-12 修訂（審計 Q2 盲區收口）**——「出事才落地」放寬為「出事＋低頻結構化留痕」，仍不擾動運算：
    - **ZoneSnap 擴欄**：現場快照補 SUB/融合欄位（algo_mode/multiscale/pitch_times/choose_amount/blob_*/lsc/
      remap/smooth），出事時可直接回答「這張用哪個演算法跑的」。
    - **`record_recipe`**：LOAD_RECIPE **成功**也寫一行 `type="recipe"` jsonl（守門判定後每 zone 關鍵參數）——
      封殺「載錯但合法無痕跡」整族（stale 守門/靜默預設事故的事後還原）。
    - **`defect_flood` 觸發器**：任一 zone 觸頂 cap=10000 或總數 ≥ cap → 自動 incident（含最近 64 張基線）；
      第一懷疑 Pitch/演算法域錯配。
    - **`tick_stats`**：每 200 張寫一行 `type="stats"`（fps/gpu_ms p50/p95/max/queue_peak/defects）——效能退化
      有基線、hang 有「最後活著時間」上界。與 record_frame 同執行緒、計時區外，bench 仍全 no-op。
    - **incident 節流**：同 kind 30s 窗內只寫一次完整 incident 檔，期間每 100 筆補一行
      `incident_suppressed` 摘要＋下次完整寫入帶 `suppressed_since_last`——防持續性錯誤（如 NOCRC 單邊）
      把 _diag 灌爆 inode。
    - **race 修復**：record_incident 深拷現場一律移入 ring_mtx_（try_lock＋yield 重試）；搶不到鎖時僅 dump
      POD 摘要（不碰 string/vector，消除 UB）。
    - rdma_source 收圖補「payload/尺寸一致性」驗證（w/h∈[1,16384] 且 payload==w×h）→ 失敗記
      `frame_validation`（P1-6 收口）；rdma-process 的 scene 補填 queue_depth。
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
    **[2026-06-17 damac↔Spark 實機驗通；⚠️ 資料路徑 2026-07-30 已改 `SEND/RECV`（★2 根治，見 STATUS）]**：
    **現行資料路徑 = SEND/RECV**：slot 落點由**收端 recv WQE** 決定（送端不再指定遠端位址）、
    `slot_id` 取自 `wc.wr_id`（不再 `seq % n_slots` 推算）、`seq` 取自 payload 內 `FrameHeader.frameSeq`
    （uint64，不受 imm 32-bit 截斷）、另以 `wc.byte_len` 與 header 宣告長度對帳；**處理完該 slot 才重掛
    其 WQE** → 無 WQE 時 RNR 發生在「放置資料之前」→ slot 不可能在讀取期間被覆寫（by construction）。
    credit 背壓語意與順序不變：
    IP 預掛 N 個 `post_recv`（= N 個初始 credit）；`recv_thread` 正確順序為
    `[1] memcpy slot → payload → [2] push_blocking（等 FrameQueue 有位置）→ [3] post_recv（補 credit）`。
    此順序不可調換：post_recv 在 push_blocking 之後，保證 Grab 在 IP 讀完 slot 前不能重用該 slot。
    credit 耗盡 → Grab `SEND` 觸發 RNR（`rnr_retry_count=7=∞`）→ Grab `poll_one()` 阻塞
    → 自然背壓，無需額外控制通道。C++17 happens-before 語意保證順序，不需額外 `std::atomic_thread_fence`。
    `--rdma-slots` 預設 4（4×40MB=160MB host pinned memory）。
    （**歷史脈絡**：舊資料路徑為 `WRITE_WITH_IMM` + `slot_id = frame_seq % n_slots`——RNR 只擋 imm、
    payload 照樣落地且重試時反覆重寫 slot → 多相機+背壓實測 CRC 損毀（slots=2/4/16 → err=11/10/0），
    故改 SEND/RECV；**勿再回退**。下方 min_rnr_timer 陷阱與量測敘述於 SEND 路徑仍然成立。）
    **實測數據（2026-06-17）**：Phase 1 連續 120 幀 CRC=OK、1375fps/86MB/s、slot 繞回正確；
    Phase 2 背壓（--test-consumer-delay-ms 200）→ Grab 降至 9.6fps（非斷線）、QP 未進 error state。
    **⚠️ min_rnr_timer 陷阱（2026-06-23 全幅 8160×5000 實機，commit `dee1a15`）**：`rdma_cm` 預設
    `min_rnr_timer = index 0 = 655.36ms`。若送端速率 > 收端、credit 短暫耗盡 → RNR NAK → 送端等 **655ms** →
    吞吐崩潰（實測 2.6fps）。**這就是「33fps 牆」的隱形成因**：原 app-CRC 的 16ms/幀意外把送端降速對齊收端、
    剛好避開 RNR；CRC 一關送端變快即 RNR 崩潰。**修法**：收端 `accept_conn()` 後
    `ibv_modify_qp(qp, {min_rnr_timer=12}, IBV_QP_MIN_RNR_TIMER)`（~0.64ms，RTS 狀態可改、安全永遠開）→
    RNR 快速重試、吞吐回到消費端速率。配合送端非同步多緩衝（`grab/src/rdma_sender.cpp` N-buffer
    lazy FIFO poll，commit `66bc8ba`）+ app-CRC 改 env 可選（`CFAOI_RDMA_NOCRC=1`，RC 已保證有序無損；
    關 CRC 仍 bit-exact）→ **33→73.8fps（2.2×）**。73.8fps → 37CCD/1110 張 ~15s，遠進 20–30s 節拍。
    **收端 slot→host memcpy（11.7ms/幀）是生產必要上限**（缺陷小圖從 host 影像裁切，`io/result_saver.cpp`），
    故 device-direct（slot→device 跳 host）只利 `--no-save` 合成壓測、不利生產 → 不做。詳見
    `docs/rdma_replay_驗證報告_20260623.html`。
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
