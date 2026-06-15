# IP 程式完整說明

> 版本：2026-06-15 整理（逐檔靜態分析 + GB10 實機驗證對照）+ flight_recorder（行車紀錄）
> 專案：`ip/CMakeLists.txt`（C++17 / CUDA）→ 可執行檔 `cfaoi_ip`
> 技術棧：C++ + CUDA（cuda_kernels.cu / ai_kernels.cu）+ OpenCV + nlohmann_json + fmt + cuBLAS
> 平台：Linux RTX 2080 Super（sm_75，開發）/ DGX Spark GB10（sm_121，生產，ARM aarch64）

---

## 目錄

1. [系統概述](#1-系統概述)
2. [架構對比：Reference gpu_algo → IP](#2-架構對比reference-gpu_algo--ip)
3. [整體分層架構](#3-整體分層架構)
4. [軟體流程圖](#4-軟體流程圖)
5. [主要模組職責](#5-主要模組職責)
6. [執行模式與命令列參數](#6-執行模式與命令列參數)
7. [GPU 檢測管線](#7-gpu-檢測管線)
8. [RecipeInfo.xml → ZoneConfig 對應](#8-recipeinfoxml--zoneconfig-對應)
9. [TCP 協議（Control ↔ IP，port 8200）](#9-tcp-協議control--ip-port-8200)
10. [結果輸出（result_saver）](#10-結果輸出result_saver)
11. [AI 分類器](#11-ai-分類器)
12. [記憶體策略（zero-copy vs discrete）](#12-記憶體策略zero-copy-vs-discrete)
13. [關鍵不變式](#13-關鍵不變式)
14. [建置與平台支援](#14-建置與平台支援)
15. [設定檔說明](#15-設定檔說明)
16. [驗證狀態](#16-驗證狀態)
17. [關鍵檔案索引](#17-關鍵檔案索引)

---

## 1. 系統概述

IP 是 CF-AOI 分散式架構的**運算節點**，負責：

- 接收**影像**（offline-file 讀檔、offline-tcp 由 Control 送圖、未來 RDMA 由 Grab 直送），在 GPU 上跑 **8 方向鄰域比較**的 LCD CF 缺陷檢測。
- 把缺陷結果寫成 **legacy 相容**的 `ResultInfo.json` / `ResultInfo.xml` + 缺陷小圖 + overlay。
- 提供 **TCP JSON server（port 8200）** 給 Control：載入配方、送圖檢測、缺陷遠端歸檔 / 人工分類。

核心策略：**GPU kernel 直接複製 `Reference/gpu_algo/`，只換 I/O 外殼**。
`cuda_kernels.cu` / `ai_kernels.cu` 的 `__global__` kernel 本體一字不改（host 端編排可改，見 §13）。

與 Reference `gpu_algo` 最大不同：
- 加上 **CCL 收斂迴圈 + canonical 排序** → 同影像兩跑 **bit-exact**（reference 單趟、非決定性）。
- I/O 外殼換成 **XML 配方 / JSON 結果 / TCP 送圖 / network-clean**（reference 是 CSV/bmp + 檔案）。
- 多 ROI（每個 `DetectRoi` 裁子影像各跑一次再合併）。

---

## 2. 架構對比：Reference gpu_algo → IP

```
Reference gpu_algo（單機批次工具，RAG_TRAINING.md 描述）
┌──────────────────────────────────────────────┐
│ batch_detector（GPUDetectionEngine）          │
│   FileReceiver / SocketReceiver / Rivermax    │
│   cuda_kernels_fast.cu（8-way/CCL/Blob）       │  ← 演算法真相
│   tensor_core_classifier.cu（MLP + RF）        │
│   config.ini（含合成缺陷注入）                  │
│   輸出 Result.csv + result.bmp + patches       │
│   CCL 單趟（非決定性）                          │
└──────────────────────────────────────────────┘
                      ↕ 遷移（換 I/O 外殼，kernel 不改）
CF-AOI 分散式架構 IP 程式（本文件）
┌──────────────────────────────────────────────┐
│ main.cpp（模式分派 offline-file/tcp/bench）     │
│   IImageSource（File / Tcp / [Rdma 待做]）     │
│   GpuPipeline.process_frame()                  │
│     ├ cuda_kernels.cu（複製，不改 kernel）      │
│     │   + CCL 收斂迴圈（host 編排，新增）        │
│     │   + canonical 排序（host，新增）          │
│     └ ai_kernels.cu（複製，不改 kernel）        │
│   ZoneConfigAdapter（RecipeInfo.xml→ZoneConfig）│
│   ControlServer（TCP JSON @8200）              │
│   ResultSaver（ResultInfo.json/.xml + 小圖）    │
└──────────────────────────────────────────────┘
            ↑ TCP JSON 8200            ↑ (未來) RDMA
        ┌───┴────┐                 ┌───┴────┐
        │ CONTROL │                 │  GRAB   │
        └─────────┘                 └─────────┘
```

---

## 3. 整體分層架構

```
╔═══════════════════════════════════════════════════════╗
║ 進入點 / 模式分派 (main.cpp)                          ║
║   parse_args → from_ini/from_recipe_xml → GpuPipeline ║
║   分派：offline-file / offline-tcp / bench            ║
║   process_image()：多 zone 裁切 → process_frame 合併   ║
╚═══════════════════════════════════════════════════════╝
        ↑ 影像來源                    ↑ TCP 命令
╔══════════════════════════╗  ╔══════════════════════════╗
║ image_source/            ║  ║ control_server.{h,cpp}   ║
║  IImageSource（介面）     ║  ║  TCP JSON server @8200   ║
║  FileImageSource         ║  ║  newline-JSON + raw bytes║
║  TcpImageSource          ║  ║  FrameQueue（送圖佇列）   ║
║  FrameQueue（MPSC 佇列）  ║  ║  deliver_result（會合）   ║
╚══════════════════════════╝  ╚══════════════════════════╝
        ↓ uint8 Mono8 + ZoneConfig
╔═══════════════════════════════════════════════════════╗
║ GPU 運算層 (gpu/)                                     ║
║  GpuPipeline（PIMPL）process_frame()                  ║
║   GPUMemoryManager  zero-copy(GB10) / pinned(x86)     ║
║   cuda_kernels.cu  8-way / CCL(收斂) / Blob / LSC      ║
║  DetectionResult（缺陷集 + 計時）                      ║
╚═══════════════════════════════════════════════════════╝
        ↑ ZoneConfig                  ↓ DetectionResult
╔══════════════════════════╗  ╔══════════════════════════╗
║ config/                  ║  ║ ai/（預設停用）           ║
║  ZoneConfigAdapter       ║  ║  TensorCoreClassifier    ║
║  ConfigParser（INI）      ║  ║  ai_kernels.cu RF+MLP    ║
║  ZoneConfig（CPU 參數）   ║  ║  RF 100 樹（TF32/FP16）   ║
╚══════════════════════════╝  ╚══════════════════════════╝
        ↓ InspectionResult（多 zone 彙整）
╔═══════════════════════════════════════════════════════╗
║ 輸出層 (result_saver.{h,cpp})                        ║
║  <out>/<yyyyMMdd>/<panel>_<recipe>/                   ║
║   ResultInfo.json/.xml（legacy JudgeResult 相容）      ║
║   Defect_*.png 小圖（多緒）+ _result.png overlay       ║
╚═══════════════════════════════════════════════════════╝

  process_image ──set_scene/record_frame（純觀測，不影響運算）──┐
                                                              ↓
╔═══════════════════════════════════════════════════════╗
║ 診斷層 (diag/flight_recorder.{h,cpp})  ※行車紀錄      ║
║  環形緩衝 64 張當下現場（平時零磁碟 I/O）             ║
║  出事才落地 <out>/_diag/<yyyyMMdd>.jsonl（事件索引）   ║
║            + incident_<ts>.json（完整現場 pretty）     ║
║  atomic latest_ 跨執行緒；atexit/set_terminate 抓現場 ║
║  收圖入口 magic/version/CRC32 驗證（§4.6/§9/§13#17）   ║
╚═══════════════════════════════════════════════════════╝
```

---

## 4. 軟體流程圖

### 4.1 啟動 / 模式分派（main.cpp:208-407）

```
main()
  ├─ parse_args()                                      失敗 → 1
  ├─ base = ZoneConfigAdapter::from_ini(--ini)         （單一全幅 zone 預設）
  ├─ 套用 bench 覆寫（--bth/--dth/--pitch-x/-y，若給）
  ├─ GpuPipeline pipe(--ai-model-dir)                  偵測 integrated→zero-copy；set_ai_active(--use-ai)=false
  ├─ FlightRecorder::begin_session(...)                pipe 後（拿 zero_copy/ai）；bench 不啟用→全 no-op（§13#16）
  ├─ zones = --recipe ? from_recipe_xml(...) : { base }  RecipeError → record_incident("recipe_load") + 2
  └─ switch(--mode)
       ├─ offline-file → §4.2
       ├─ offline-tcp  → §4.3
       └─ bench        → §4.5
```

### 4.2 offline-file（讀檔批次，main.cpp:250-271）

```
FileImageSource src(--input)            檔案或目錄（.tif/.png/.bmp...，排序）
  loop src.next_frame(hdr, payload)
    ├─ cv::imread(IMREAD_UNCHANGED)     不變式：禁後處理；多通道取 ch[0]；非 8-bit 跳過
    ├─ gray = Mat(h, w, CV_8UC1, payload)
    ├─ process_image(pipe, zones, gray, name, verify)   §4.4
    └─ ResultSaver::save(res, payload, w, h, --output, --ip-name, save_opt)
  （--verify-deterministic：每 zone 跑兩次比對 bit-exact，不一致 → return 4）
```

### 4.3 offline-tcp（Control 送圖，main.cpp:273-329）

```
FrameQueue queue;  ControlServer server(--control-port=8200, queue)
  server.set_load_recipe_handler(...)   收 LOAD_RECIPE → from_recipe_xml_content（XML 內容，network-clean）→ 更新 zones
  server.set_status_provider(...)        GET_STATUS → {processed, ai, zones}
  server.start()                         監聽 0.0.0.0:8200（失敗 → 3）
TcpImageSource src(queue)
  loop src.next_frame(hdr, payload)      ← 由 SEND_IMAGE_FOR_REVIEW push 進 queue
    ├─ name = panel_id（或 cam{n}_seq{n}）
    ├─ zones 快照（mutex）
    ├─ process_image(... verify=false)
    ├─ ResultSaver::save(...)            review_opt.save_patches = server.review_save_patches()（僅 debug=true 存全部小圖）
    └─ server.deliver_result(name, ResultSaver::to_json(res))   結果經 TCP 會合回傳
```

### 4.4 process_image：多 zone 裁切 → 合併（main.cpp:159-204）

```
（offline-file/tcp 迴圈：process_image 前後夾行車紀錄，純觀測，bench 無此 hook）
  FlightRecorder::set_scene(scene_params)   ← process 前：抓當下參數現場（panel/zone/BTH/DTH/pitch）
InspectionResult agg{ panel_id, w, h, ai_used=pipe.ai_enabled(), recipe_name }
for zone in zones:
   r = zone_rect(zone, w, h)              全幅→整張；否則夾 ROI 邊界
   sub = gray(r)                          裁切子影像（不連續則 clone）
   zc = zone; zc.width=sub.cols; zc.height=sub.rows; zc.panel_id=...
   res = pipe.process_frame(sub.data, sub.cols, sub.rows, zc)   §7
   （verify：跑第二次 first_determinism_diff() 比對 → verify_failed）
   agg.zones += ZoneResult{ zone_index, roi_offset=(r.x,r.y), zone, res }
→ 一個 DetectRoi = 一個 ZoneResult = 一個 RoiInfo
  FlightRecorder::record_frame(scene+結果)  ← process 後（cudaEvent 計時區外）：補缺陷數/gpu_ms/pass
```

### 4.5 bench（純 GPU 量速，main.cpp:331-398）

```
載入單張影像
warmup（--bench-warmup，丟棄，吸收 CUDA init/JIT）
量測（--bench-iters）：每次累加各 zone result.process_time_ms（cudaEvent）+ wall
另量 5 次「含存圖」wall（存 /tmp/bench_out）
報告 mean/median/P99/min/max（gpu_ms / wall_no_save / wall_incl_save）
容量換算：N_spark = ceil(1110 × gpu_median / 30000)    （1110 張/面板，30s 節拍）
```

### 4.6 SEND_IMAGE_FOR_REVIEW 會合機制（control_server.cpp:323-380）

```
Control 送：{"cmd":"SEND_IMAGE_FOR_REVIEW","params":{width,height,payload_bytes,panel_id,cam_id,debug,...}}\n
            + 緊接 payload_bytes 個 raw bytes（Mono8 = width*height）
IP：
  ├─ 尺寸守衛：width/height ∈ [1,16384] 且 payload_bytes==width*height（否則 record_incident("frame_validation")+ERR，擋 bogus→OOM）
  ├─ read_exact(payload_bytes)            讀完整張影像
  ├─ make_frame_header → 驗 magic/version/headerBytes/payloadBytes + CRC32（§13#17）
  │    + 可選 client 宣告 crc32 比對（偵測傳輸損壞）；失敗 → record_incident("frame_validation")+ERR 拒收（不 enqueue）
  ├─ results_.erase(panel)                清舊結果
  ├─ queue_.push(hdr, panel, payload)     交給 TcpImageSource → GpuPipeline
  └─ result_cv_.wait_for(60s, results_.count(panel)>0)
        ← 主迴圈 deliver_result(panel, json) 喚醒
        回 {"status":"OK","result":{...}}（或 TIMEOUT）
```

---

## 5. 主要模組職責

### 進入點 / 影像來源

| 模組 | 檔案 | 職責 |
|------|------|------|
| `main` | `src/main.cpp` | 解析參數、建 ZoneConfig、模式分派、`process_image()` 多 zone 裁切合併、verify/bench 邏輯 |
| `IImageSource` | `src/image_source/image_source.h` | 影像來源介面 `next_frame(hdr, payload)`；註解預留 RdmaImageSource（需 libibverbs，尚未實作）|
| `FileImageSource` | `image_source/file_source.cpp` | offline-file：`cv::imread(IMREAD_UNCHANGED)`，目錄排序、取 ch[0]、只收 8-bit |
| `TcpImageSource` | `image_source/tcp_source.cpp` | offline-tcp：從 `FrameQueue` 取出 ControlServer push 的影像 |
| `FrameQueue` | `image_source/image_source.h` | MPSC 阻塞佇列（ControlServer 生產 / TcpImageSource 消費）|

### GPU 運算

| 模組 | 檔案 | 職責 |
|------|------|------|
| `GpuPipeline` | `gpu/gpu_pipeline.{h,cpp}` | PIMPL；`process_frame()` 單 zone 全流程；CCL 收斂迴圈、canonical 排序、AI gate |
| `GPUMemoryManager` | `gpu/gpu_pipeline.cpp` | 偵測 integrated GPU → zero-copy(mapped) / discrete(pinned+async)；持久 buffer 重用 |
| CUDA kernels | `gpu/cuda_kernels.cu/.h` | 8-way 比較 / CCL / fused blob stats / 多尺度 / LSC（**複製不改**）|

### 設定 / 配方

| 模組 | 檔案 | 職責 |
|------|------|------|
| `ZoneConfigAdapter` | `config/zone_config_adapter.{h,cpp}` | `from_ini` / `from_recipe_xml(_content)`：DetectRoi→ZoneConfig；**DIV-only 強制**；多 zone |
| `ConfigParser` | `config/config_parser.h` | INI 解析（[Image]/[Pattern]/[LensShading]/[Threshold]/[Debug]/[GPU]）|
| `ZoneConfig` | `config/zone_config_adapter.h` | 純 CPU 參數（ROI / pitch / 閾值 / LSC / block_dim）|

### 服務 / 輸出 / AI

| 模組 | 檔案 | 職責 |
|------|------|------|
| `ControlServer` | `control_server.{h,cpp}` | TCP JSON server @8200；命令分派；送圖會合 `deliver_result`；DefectSort 遠端命令 |
| `ResultSaver` | `result_saver.{h,cpp}` | 輸出夾結構、ResultInfo.json/.xml（legacy 相容）、小圖多緒寫、overlay、清舊檔、`[Diag]` 一致性 |
| `TensorCoreClassifier` | `ai/tensor_core_classifier.h`, `ai/ai_kernels.cu` | RF（100 樹）+ MLP 備援；TF32/FP16 Tensor Core；**預設停用** |
| `FlightRecorder` | `diag/flight_recorder.{h,cpp}` | 行車紀錄：ring buffer 64 張當下現場 + 出事 dump incident JSON；收圖入口 CRC/magic 驗證；atomic `latest_` 跨執行緒抓現場（atexit/set_terminate）；純觀測、bench no-op |

---

## 6. 執行模式與命令列參數

### 6.1 三種模式（`main.cpp`）

| `--mode` | 用途 | 影像來源 | 起 TCP server |
|----------|------|---------|--------------|
| `offline-file`（預設） | 讀檔批次驗證（MIL/真實影像）| FileImageSource | ❌ |
| `offline-tcp` | Control 送圖（Step 1）| TcpImageSource | ✅ @8200 |
| `bench` | 純 GPU 量速（容量規劃）| 單張檔 | ❌ |

> `rdma-validate` / `image-capture` / `online` 模式**尚未實作**（`modes/` 空、無 RdmaImageSource）。CMake 偵測到 ibverbs 會印「RDMA enabled」但無對應原始碼（define-only gate）。

### 6.2 命令列參數（全部）

| 旗標 | 預設 | 效果 |
|------|------|------|
| `--mode <m>` | offline-file | 模式分派 |
| `--input <path>` | "" | offline-file/bench：影像檔或目錄 |
| `--output <dir>` | output | 結果輸出根目錄 |
| `--recipe <xml>` | "" | legacy RecipeInfo.xml 路徑（多 ROI，DIV-only）|
| `--ini <path>` | config/default_zone.ini | 預設參數 INI |
| `--control-port <n>` | 8200 | offline-tcp 監聽 port |
| `--ai-model-dir <dir>` | models/gpu_model | AI 模型目錄（找不到→停用 AI）|
| `--ip-name <name>` | IP01 | 缺陷檔名 `Defect_{IpName}_...` 的 fallback（優先取 panel 前綴）|
| `--use-ai` | （off） | 啟用 AI 分類過濾 |
| `--no-save-images` | — | 只存 ResultInfo（patches + overlay 都關）|
| `--no-overlay` | （overlay on） | 不存 overlay 全圖（仍存小圖）|
| `--max-patches <n>` | -1 | 只存前 N 張缺陷小圖 |
| `--save-threads <n>` | 0（auto） | 小圖平行寫入緒數 |
| `--verify-deterministic` | （off） | offline-file：每 zone 跑兩次比對 bit-exact（不一致 return 4）|
| `--bench-iters <n>` | 100 | bench 量測張數 |
| `--bench-warmup <n>` | 10 | bench 暖機張數（丟棄）|
| `--bth <f>` / `--dth <f>` | -1 | 覆寫單一全幅 zone 的 BTH/DTH（bench 掃負載；≥0 才生效）|
| `--pitch-x <n>` / `--pitch-y <n>` | -1 | 覆寫 pitch（bench；>0 才生效）|
| `-h` / `--help` | — | 印用法 |

**回傳碼**：0 OK｜1 參數/缺輸入｜2 配方載入錯（RecipeError）｜3 server.start 失敗｜4 非決定性。

---

## 7. GPU 檢測管線

### 7.1 process_frame 單 zone 全流程（gpu_pipeline.cpp:218-306）

```
run(img, w, h, cfg):
  1. gpu_mem.allocate(w,h,MAX_DEFECTS)          同尺寸則重用（不重配）
  2. cudaEventRecord(ev_start)
  3. uploadImage                                zero-copy:memcpy→mapped→D2D / discrete:memcpy→pinned→H2D
  4.(可選) LensShadingCorrection                cfg.enable_lsc（預設 off 保 bit-exact）
  5. KernelParams = {w,h,pitch_x,pitch_y,search_range_x,search_range_y,BTH,DTH}
  6. launchFast8WayKernel(... fast_search_range)  8 方向鄰域比較（DIV 比例）
  7. launchFastCCLKernel                         CCL：init → 收斂迴圈（§7.3）
  8. launchFastBlobAnalysis                      fused flatten+blob stats → DefectInfo[]
  9. cudaEventRecord(ev_stop) + sync
 10.(可選) AI：classifyAndFilterGPU              僅 ai_on && ai_active（§11）
 11. process_time_ms = event elapsed
 12. canonical 排序（§7.4）
 13. 統計 bright/dark；pass = (num_defects==0)
```

### 7.2 DIV 比例式閾值（cuda_kernels.cu，8-way kernel）

中心像素 vs 8 鄰域（距離 ±pitch_x / ±pitch_y）之**比值**判定，為避浮點除法用整數縮放：
```
neighbor_sum = Σ 8 鄰域值      （search_range>0 時取垂直 ±1 最接近中心者，容忍線掃歪斜）
center_check = (center << 3) * 1000        // ×8 對應 8 個鄰域 = 比 mean₈
bth_scaled = round(BTH*1000)，dth_scaled = round(DTH*1000)
center_check > neighbor_sum * bth_scaled → binary=255（亮缺陷 PointBright）
center_check < neighbor_sum * dth_scaled → binary=128（暗缺陷 PointDark）
否則 binary=0
```
等價於 `center/mean₈ > BTH`（亮）/ `< DTH`（暗），與 legacy `AlgorithmCompare="DIV"` 嚴格相等。
binary 編碼 255/128 為下游所依賴：CCL 只合併同型（亮暗不互混）。

**未檢測邊界（DeathMargin）**：每個 8-way kernel 內 `margin = pitch*2 + search_range`（x、y 各算），
邊界外像素強制 binary=0 → 有效檢測區 `[margin_x, w-margin_x) × [margin_y, h-margin_y)`。result_saver 會 log 此區。

### 7.3 CCL 收斂迴圈（不變式 #7，cuda_kernels.cu:1156-1203）

```
memset labels = -1（DMA）
kernelFastCCLInitMerge：缺陷像素 label=idx
迴圈（最多 MAX_CCL_ITERS=1000；典型 CF 點缺陷 2-4 趟收斂）：
   memset d_changed=0
   kernelFastCCLMerge   8 鄰域同型 atomicMin union；有變動 → d_changed=1
   讀回 d_changed
直到 d_changed==0
```
reference 原版**只跑單趟**（lock-free union-find 未收斂）→ 缺陷數隨 thread-race 飄動（曾見 2761/2762/2763）且大 blob 漏合併。
收斂後 atomicMin 不動點唯一、order-independent → **bit-exact + 正確合併**（正確數 2606）。
**只改 host 端 launch 編排，`__global__` kernel 本體一字不改**（符合不變式 #1）。

### 7.4 canonical 排序（不變式 #8，gpu_pipeline.cpp:292-298）

blob analysis 用 `atomicAdd` append → 陣列順序隨 race 變動（集合同、順序不定）。
下載後 host 端 `std::sort` 依 **label → center_y → center_x → size**，順序唯一 → bit-exact。

### 7.5 關鍵常數

| 常數 | 值 | 位置 |
|------|----|----|
| `MAX_DEFECTS` | 10000 | gpu_pipeline.cpp:24 |
| `MAX_UNIQUE_LABELS` | 65536 | cuda_kernels.cu:24 |
| `MAX_CCL_ITERS` | 1000（典型 2-4） | cuda_kernels.cu:1190 |
| `block_dim` | **16×16（from_ini 硬寫死，無條件覆寫，忽略 INI 的 `[GPU] block_dim` 不論 16/32）** | zone_config_adapter.cpp:69-70 |
| Blob 分析 block | 32×32 | cuda_kernels.cu:1219 |
| 多尺度 dispatch | `width < 8000`（8160 全幅 → 走 texture 8-way）| cuda_kernels.cu:1110 |
| blob 過濾 | min=1, max=300, aspect>5&density<0.3 剔除 | cuda_kernels.cu:1275/811 |
| DeathMargin | `pitch*2 + search_range`（x,y）| cuda_kernels.cu:135-136 |

### 7.6 __global__ kernels（cuda_kernels.cu）

| Kernel | 功能 |
|--------|------|
| `kernelFast8WayTexture` | 8 方向比較（tex2D，全幅主路徑，`__launch_bounds__(1024)`）|
| `kernelFast8WayComparison` | 8 方向比較（global mem，多尺度 / fallback）|
| `kernelFast8WayShared` | 8 方向比較（shared-mem tile + halo）|
| `kernelLensShadingCorrection` | 徑向暗角校正 `1+k1r²+k2r⁴+k3r⁶` |
| `kernelFastCCLInitMerge` | 缺陷像素 label=idx |
| `kernelFastCCLMerge` | 一趟 union-find（8 鄰同型 atomicMin）|
| `kernelFusedFlattenBlobStats` | 融合：flatten + 稀疏 hash 統計（count/sum_x/sum_y/sum_gl/bbox/is_bright）|
| `kernelSparseCollectDefects` | 每 unique-label → 過濾 → 輸出 DefectInfo（atomicAdd）|
| `kernelDownsample2x/4x` | 2×/4× 面積平均降採樣（多尺度）|
| `kernelUpscaleBinaryMask2x/4x` | 低解析二值 OR 回全幅 |

### 7.7 缺陷欄位（GPU 計算）

`DefectInfo`（cuda_kernels.h:8-16）：`label, center_x, center_y, size, avg_brightness, min_x/max_x/min_y/max_y, is_bright`。
- `size` = 像素數；`center_x/y` = 質心；`avg_brightness` = blob 平均灰階 = **GL_Mean**；`is_bright` → Type PointBright/PointDark。
- GPU 不算的（result_saver 填 0 / 衍生）：`CV_*`、`GL_Sigma/Min/Max`、`Width/Height`（由 bbox 推）、`GlobalPosX/Y`（roi_offset+center）。

---

## 8. RecipeInfo.xml → ZoneConfig 對應

> ⚠️ 配方是序列化的 legacy **`Recipe`**（`Recipe → M_AlignRoi + DetectRoiList + DetectIoiList`），每台 IP 一份。
> 閾值欄位是 **`BrightThreshold`/`DarkThreshold`**（不是 ThB/ThD）。

`ZoneConfigAdapter::from_recipe_xml_content`（zone_config_adapter.cpp:86-163，純字串解析，無 XML lib）逐 `<DetectRoi>` 轉一個 ZoneConfig：

| ZoneConfig | legacy DetectRoi | 說明 |
|------------|-----------------|------|
| `BTH` | `BrightThreshold` | 亮閾值（**僅 DIV，嚴格相等**）|
| `DTH` | `DarkThreshold` | 暗閾值（同上）|
| `pitch_x` / `pitch_y` | `PitchX` / `PitchY` | 網格週期 |
| `search_range_x/y` | `SearchX` / `SearchY` | 搜尋範圍 |
| `fast_search_range` | `clamp(SearchY, 0, 2)` | kernel 實吃的垂直局部搜尋 |
| `roi_start_x/y`,`roi_end_x/y` | `StartX/StartY/EndX/EndY` | -1 = 全幅；每 DetectRoi 一個 zone |
| LSC / multiscale / block_dim | （XML 無）| 取 `from_ini` 預設；block_dim 固定 16×16 |

- **DIV-only 強制**（不變式 #9）：`<AlgorithmCompare>` 非 `"DIV"`（SUB / 缺 tag）→ 拋 `RecipeError` 拒絕載入（含 zone index + 修正建議）。
- **忽略並 log**：`AlgorithmWay` / `PitchTime` / `ChooseAmount` / `Blob*`（gpu_algo kernel 無對應）。
- 無 `<DetectRoi>` → 拋 RecipeError。

---

## 9. TCP 協議（Control ↔ IP，port 8200）

格式：`{"cmd":..,"seq":..,"params":{..}}\n`，回應 `{"seq":..,"status":"OK"|"ERR"|"TIMEOUT",...}\n`（newline-delimited JSON）。
單一 accept thread、一次服一個 client（offline review 為序列單請求）。

| `cmd` | 輸入 params | 回應欄位 |
|-------|------------|---------|
| `CHECK_HEALTH` | 無 | `status`, `ai`(bool) |
| `GET_STATUS` | 無 | `status`, `data`={processed, ai, zones} |
| `LOAD_RECIPE` | `recipe`, **`recipe_xml`(XML 全文)**, `panel_id` | `status`（失敗附 `error`）|
| `SEND_IMAGE_STREAM_BEGIN` | （無；重置 frame_seq=0）| `status` |
| `SEND_IMAGE_FOR_REVIEW` | `width`,`height`,`payload_bytes`,`panel_id`,`cam_id`,`frame_seq`,`last`,`debug` + 緊接 raw Mono8 bytes | `status`, `frame_seq`, `result`{...}（或 TIMEOUT）|
| `LIST_DEFECT_FOLDERS` | `date`(yyyyMMdd，""=全部) | `status`, `folders`[{folder_name, panel_id, date, defect_count}] |
| `SORT_DEFECTS` | `date`,`output_subdir`,`by_id_folder`,`selected_folders`[] | `status`, `results`[{folder, copied}], `total`, `output_dir` |
| `LIST_DEFECT_PATCHES` | `date`,`folder_name` | `status`, `patches`[{patch_id, run_index, roi_index, GC_X, GC_Y, Size, Type, current_class}] |
| `GET_DEFECT_PATCHES_BATCH` | `date`,`folder_name`,`patch_ids`[] | `status`, `patches`[{patch_id, png_base64}] |
| `SAVE_DEFECT_CLASSIFICATION` | `date`,`folder_name`,`classifications`[{patch_id, class}] | `status`, `TrueDefect`, `Particle`, `total`, `output_dir` |

- **收圖驗證**（不變式 #17）：`SEND_IMAGE_FOR_REVIEW` 收圖先過**尺寸守衛**（width/height ∈ [1,16384]）+ **magic/version/headerBytes/payloadBytes + CRC32** 驗證（offline-tcp 另可帶 `crc32` 宣告值比對偵測傳輸損壞）；任一失敗 → 回 `ERR` 並記 `frame_validation` incident、**拒收不 enqueue**。
- **network-clean**（不變式 #11）：`LOAD_RECIPE` 傳 **XML 內容**（非路徑）；缺陷小圖以 **base64 PNG** 經 TCP 回傳；IP 不讀寫對方硬碟。
- **送圖會合**：SEND_IMAGE 推進 FrameQueue → 主迴圈算完 `deliver_result(panel, json)` → `result_cv_` 喚醒回傳（60s timeout）。
- **DefectSort 命令**就地操作 `output_dir`（IP 端硬碟），對應 Control 的缺陷整理 UI 兩層（panel 夾列表 / 小圖人工分類）。

---

## 10. 結果輸出（result_saver）

### 10.1 輸出夾結構

```
<output>/<yyyyMMdd>/<panelId>_<recipeName>/
   Defect_<IpName>_Slice<ff>_Roi<rr>_Run<nn>_X<xxxx>_Y<yyyyyy>_Dr<Bright|Dark>.png   缺陷小圖（全域座標）
   <panelId>_<recipeName>_ResultInfo.json    Control 反序列化用
   <panelId>_<recipeName>_ResultInfo.xml     = 序列化 JudgeResult，給上位機 CF_GET_RESULT 鏈
   <panelId>_<recipeName>_result.png         overlay（PNG 低壓縮）
```
- `IpName` 取 panel 名第一個 `_` 前 token（`IP02_Origin000001`→`IP02`），與資料夾一致；`--ip-name` 為 fallback（不變式 #12）。
- `Slice<ff>` = `cy / frame_height`（線掃 slice）；`Run<nn>` = 缺陷序（max_patches 截斷時仍 ++ 保序）。

### 10.2 ResultInfo 欄位（JSON + XML 一致，legacy JudgeResult）

```
JudgeResult/top: panel_id, recipe_name, DefectCnt, AiOkCnt(0), RuleOkCnt(0), pass,
                 total_time_ms, image_width, image_height, RoiInfoList[]
RoiInfo:  RoiIndex(=zone_index), roi_offset_x/y, num_defects, process_time_ms, DefectInfoList[]
DefectInfo: RunIndex, GlobalPosX/Y, Size, Width, Height, Type, Filter(NoFilter),
            X_Min/X_Max/Y_Min/Y_Max, GC_X/GC_Y, GL_Mean, DetectReason, AiType, ...
```
- 座標：`GC_X/GC_Y` = ROI 局部質心；`GlobalPosX/Y` 與 `X_/Y_Min/Max` = roi_offset + 局部（全域）。
- GPU 無的填 0：`CV_Sigma/Mean/Min/Max`、`GL_Sigma/Min/Max`；`AiIndex=-1, AiScore=-1, MeanValue=-1`。
- `AiType`：AI 啟用→`"NoSet"`；**停用→`"待人工複核"`**（缺陷進 DefectSort 人工標 TrueDefect/Particle）。
- XML 多 `<SaveDefectWidth>/<SaveDefectHeight>` = patch_size(100)，`<IoiInfoList/>` 空。

### 10.3 存圖行為

- **重測先清舊**（不變式 #9）：存圖前**無條件**刪本層舊 `Defect_*` 與 `.bmp`（不動 `TrueDefect/`、`Particle/`、`classification.json`）→ 避免換 IpName/換參數堆疊成 N 倍（曾見 1122=561×2）。
- 小圖：`patch_size=100`（半徑 50），邊緣補零；多緒平行 `cv::imwrite`（PNG 壓縮等級 1）；`--max-patches` 上限；`debug`/`--no-save-images` 控制。
- overlay：灰→BGR，每缺陷畫框（亮=紅、暗=藍），PNG 低壓縮。
- **`[Diag]` 三數一致**：`DetectionResult 缺陷數 == JSON DefectInfo 筆數 == 寫出 patch 數`（單次乾淨偵測恆 1:1）。
- `[T.T]` log：GPU運算 / crop / patch存圖 / overlay存圖 各階段 ms。

---

## 11. AI 分類器

> **預設停用**（訓練資料不足）：模型載入但 `set_ai_active(false)`，不推論、不過濾，缺陷標 `待人工複核`；`--use-ai` 才開。

- `TensorCoreClassifier`（`ai/tensor_core_classifier.h`, `ai_kernels.cu`）：
  - **Random Forest 為主推論路徑**：`RF_NUM_TREES=100`、`RF_MAX_DEPTH=10`（節點數由模型檔載入，實機印 ~11592）；`initializeFromSklearn` 載 6 個 `.bin`（thresholds/features/left/right/values/offsets）。
  - MLP（24→64→32→2）為備援（cuBLAS）。
  - **Tensor Core math**：compute capability ≥80（GB10 sm_121）→ `CUBLAS_TF32_TENSOR_OP_MATH`；70-79（RTX2080 sm_75）→ FP16 `CUBLAS_TENSOR_OP_MATH`。
  - 特徵：每缺陷 24 維（32×32 patch 的 mean/std/min/max/直方圖/Sobel/中心區/分位數…）。
- **Gate**：`ai_on（模型載入）&& ai_active（--use-ai）` 才在 `process_frame` Step 7 跑 `classifyAndFilterGPU`（獨立 `ai_stream`）。
- ai_kernels.cu kernels：`extractFeaturesKernel`、`randomForestInferenceKernel`（主）、`filterDefectsKernel`、`extractCoordsFromDefectsKernel` + MLP 的 relu/addBias/softmax。

---

## 12. 記憶體策略（zero-copy vs discrete）

`GPUMemoryManager::detectIntegratedGPU()`（gpu_pipeline.cpp:51-61）讀 `cudaDeviceProp.integrated`：

| 平台 | integrated | 策略 |
|------|-----------|------|
| **DGX Spark GB10**（NVLink-C2C SoC，ARM）| true | **zero-copy mapped**：`cudaHostAlloc(...Mapped)` + `cudaHostGetDevicePointer`；upload 為 memcpy→mapped→D2D（NVLink-C2C ~900GB/s，免 PCIe）|
| **RTX 2080S**（x86 discrete）| false | pinned 暫存：`cudaMallocHost` + `cudaMemcpyAsync(H2D)` |

- 兩路徑仍各拷到獨立 `d_input`（kernel 存取 cache-friendly）。
- 持久 buffer（`d_input/d_binary/d_labels/d_defects/...` + 稀疏 blob 單例 `g_sparse`）**跨 frame 重用**，同尺寸不重配。
- ⚠️ GB10 RDMA 收圖（未來）不可用 `nvidia_peermem`，改 `cudaHostAlloc(Portable|Mapped)`（不變式 #11 / docs §不變式 11）。

---

## 13. 關鍵不變式

> 完整 17 條見 [ip/CLAUDE.md §9](../ip/CLAUDE.md)。重點：

1. `cuda_kernels.cu` / `ai_kernels.cu` **禁改任何 `__global__` kernel 本體**（host 編排可改）。
5. 同影像兩跑 **bit-exact**（`--verify-deterministic` 驗）。
7. **CCL 收斂迴圈**：host wrapper 迴圈 `kernelFastCCLMerge` 直到 `d_changed==0`（從 Reference 重抄會覆蓋此迴圈 → 決定性壞掉，須加回）。
8. **canonical 排序**：下載後依 label→center_y→center_x→size。
9. **DIV-only**：`from_recipe_xml` 只收 `AlgorithmCompare="DIV"`，SUB/缺 tag 拒絕；BTH/DTH 嚴格相等。
9'. **重測先清 panel 夾舊 `Defect_*`**（避免 DefectSort 殘留疊加）。
12. **缺陷檔名 IpName 取 panel 前綴**。
13. **AI 預設停用**（`待人工複核`）。
14. **Pitch 正確性**：偏差 1~4px 就爆量（26→30 → 561→10000 觸頂）；新面板先 FFT 估算。
15. **GB10 block_dim 固定 16×16、效能基準 ~7.4ms/張、1 台 Spark 足夠**（見 §16 與驗證報告）。
16. **行車紀錄純觀測，不得擾動運算**：`record_frame` 在 cudaEvent 計時區外、`set_scene` 寫 ring 只鎖極短 → 不影響 gpu_ms/bit-exact；bench 不 `begin_session`→全 no-op（process_image 路徑無 scene hook）。跨執行緒抓現場用全域 `atomic<const FrameScene*> latest_`（非 thread_local）：`atexit`+`cudaPeekAtLastError`→`cuda_fatal`、`set_terminate`→`uncaught_exception`。incident kind：cuda_fatal/frame_validation/recipe_load/bad_json/uncaught_exception。
17. **收圖入口驗證**：control_server 收圖驗 magic/version/headerBytes/payloadBytes + CRC32（`shared/FrameHeader.h::crc32_ieee`）+ 尺寸守衛（width/height ∈ [1,16384]，擋 bogus→OOM）；offline-tcp 另支援 client 宣告 `crc32` 比對。失敗 → 記 `frame_validation` + ERR 拒收。（offline-tcp header 本地建構故 magic 恆對，wire 驗證主擋未來 RDMA 收圖；該分支待 `rdma_source` 實作後生效。）

---

## 14. 建置與平台支援

### 14.1 依賴與建置（`CMakeLists.txt`）

- **REQUIRED**：`CUDAToolkit`、`OpenCV`(core/imgproc/imgcodecs)、`nlohmann_json`、`fmt`、`Threads`。
- **可選**：ONNX Runtime（`CFAOI_HAS_ORT`，AI offline 後端；找不到→停用，無影響）、libibverbs（`CFAOI_HAS_RDMA`，僅 define gate，無對應原始碼）。
- 連結：`CUDA::cudart`、`CUDA::cublas`、OpenCV、nlohmann_json、fmt、Threads。
- `CUDA_SEPARABLE_COMPILATION ON`；`CMAKE_CUDA_ARCHITECTURES` 未指定 → `native`。

```bash
export PATH=/usr/local/cuda/bin:$PATH               # nvcc
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=121   # GB10；RTX2080S 用 75
cmake --build build -j$(nproc)                       # 產出 build/cfaoi_ip
```

### 14.2 平台 / 模式對照

| 平台 | GPU | sm | CUDA | 記憶體 | 支援模式 |
|------|-----|----|----|------|--------|
| Linux RTX 2080S（開發）| RTX 2080S | 75 | 12.x | discrete pinned+async | offline-file/tcp, bench |
| DGX Spark GB10（生產，ARM）| GB10 | 121 | 13.0 | zero-copy mapped | offline-file/tcp, bench（RDMA 模式待做）|

---

## 15. 設定檔說明

`from_ini` 取的欄位（[Image]/[Pattern]/[LensShading]/[Threshold]）；**[GPU] block_dim 被忽略**（強制 16×16）；[Debug]/[Output] 不帶入 ZoneConfig。

| 檔案 | width | BTH/DTH | LSC | block_dim(檔內，實際無效) |
|------|-------|---------|-----|------------------------|
| `config/default_zone.ini` | 8160 | 1.40 / 0.60 | off | 32×32（→ 實際 16×16）|
| `config/config_real.ini` | 8160 | 1.60 / 0.50 | **on**(k1 0.80) | 16×16 |
| `config/config_optimized.ini` | 8160 | 1.24 / 0.76 | — | 32×32（→ 實際 16×16）|

> 注意：INI 的 `[GPU] block_dim` 為**死設定**（`from_ini` 一律覆寫成 16×16），改 INI 不影響 GPU block 維度。

---

## 16. 驗證狀態

| 項目 | 狀態 | 證據 |
|------|------|------|
| GPU 演算法引擎（DIV-only）| L4 | RTX2080S + GB10 實機 |
| CCL 決定性（收斂 + 排序）| L4 | `--verify-deterministic` x86 & GB10 全 bit-exact |
| 結構化輸出 / patch / DefectSort 遠端命令 | L3–L4 | 真機 offline + python client |
| **ARM/GB10 運算 + 跨架構一致性** | **L4** | [verification_report_arm_20260615.md](verification/verification_report_arm_20260615.md)：26 張真實面板 25/26 一致；正常面板 ~7.4ms/張 → 1 台 Spark |
| RDMA 收圖主程式（rdma-validate/image-capture/online）| L0 | `modes/` 空、無 RdmaImageSource |
| AI 推論 | L1 | 模型載入但預設停用 |
| **行車紀錄（flight_recorder）** | **L3**（RDMA-wire CRC 分支 L1）| RTX2080 6 項驗證全過（5 種 incident kind + 決定性不破 + bench 無 `_diag`/gpu_ms 不變 + JSON 全可解析）；**GB10 待補驗**（atexit/atomic 在 ARM 記憶體模型）；RDMA-wire magic/CRC 分支待 `rdma_source` |

詳見 [docs/STATUS.md](STATUS.md)。

---

## 17. 關鍵檔案索引

| 主題 | 檔案 |
|------|------|
| 進入點 / 模式分派 | [src/main.cpp](../ip/src/main.cpp) |
| 影像來源介面 | [src/image_source/image_source.h](../ip/src/image_source/image_source.h) |
| 讀檔來源 | [src/image_source/file_source.cpp](../ip/src/image_source/file_source.cpp) |
| TCP 來源 | [src/image_source/tcp_source.cpp](../ip/src/image_source/tcp_source.cpp) |
| GPU 管線 | [src/gpu/gpu_pipeline.cpp](../ip/src/gpu/gpu_pipeline.cpp) / [.h](../ip/src/gpu/gpu_pipeline.h) |
| CUDA kernels | [src/gpu/cuda_kernels.cu](../ip/src/gpu/cuda_kernels.cu) / [.h](../ip/src/gpu/cuda_kernels.h) |
| 配方→ZoneConfig | [src/config/zone_config_adapter.cpp](../ip/src/config/zone_config_adapter.cpp) / [.h](../ip/src/config/zone_config_adapter.h) |
| INI 解析 | [src/config/config_parser.h](../ip/src/config/config_parser.h) |
| TCP server | [src/control_server.cpp](../ip/src/control_server.cpp) / [.h](../ip/src/control_server.h) |
| 結果輸出 | [src/result_saver.cpp](../ip/src/result_saver.cpp) / [.h](../ip/src/result_saver.h) |
| AI 分類器 | [src/ai/tensor_core_classifier.h](../ip/src/ai/tensor_core_classifier.h) / [ai_kernels.cu](../ip/src/ai/ai_kernels.cu) |
| 行車紀錄 | [src/diag/flight_recorder.cpp](../ip/src/diag/flight_recorder.cpp) / [.h](../ip/src/diag/flight_recorder.h) |
| 建置 | [CMakeLists.txt](../ip/CMakeLists.txt) |
| 預設參數 | [config/default_zone.ini](../ip/config/default_zone.ini) |
| 不變式 | [ip/CLAUDE.md](../ip/CLAUDE.md) |

---

*本文件由原始碼逐檔靜態分析整理（3 個並行 reader agent 全讀 ~5600 行），對照 GB10 實機驗證（2026-06-15）。2026-06-15 補 flight_recorder（行車紀錄）章節 + block_dim 三處一致性。格式對齊 [control_程式完整說明.md](control_程式完整說明.md)。*
