# CF-AOI 分散式架構 — 全域 CLAUDE.md

> Claude Code 根目錄 context。各程式子目錄（ip/grab/control）有各自的 CLAUDE.md。
> **核心策略：從 Reference/ 遷移現有程式碼，不從空白重寫。**
> **新 session 工作流程：先讀本文件（總綱）→ 再讀目標模組 CLAUDE.md（見 §9 索引）。**

---

## 1. 目錄結構

```
~/cf-aoi/
├── docs/CLAUDE.md          ← 本文件（總綱；§9 有各模組 CLAUDE.md 索引）
├── ip/                     ← IP 程式（C++ CUDA）
├── grab/                   ← Grab 程式（C++ pylon/eBUS）
├── control/                ← Control 程式（C# Avalonia）
├── tools/                  ← 小工具（golden_maker 等；見 tools/README.md）
├── shared/FrameHeader.h    ← RDMA wire format（兩端共用）
├── scripts/                ← 驗證腳本（verify_alignment.py、estimate_pitch.py 等）
├── test_images/            ← MIL 測試影像
└── Reference/              ← 唯讀，舊版程式碼遷移來源
    ├── gpu_algo/           ← 全 GPU 演算法
    ├── legacy_win/         ← 舊版 Windows（PrjCfAoi）
    └── phase1_tests/       ← Phase-1 測試套件
```

---

## 2. 系統架構

```
上位機（Master Controller PC，另一台 PC，不是 PLC）
    ↕ TCP port 8787（CF_ 前綴文字命令，| 分隔，\r\n，9 參數回應；見 §5）
CONTROL（C# Avalonia，Linux/Windows/macOS）
    ↓ TCP JSON 8100        ↓ TCP JSON 8200
GRAB（Linux x86）         IP（Linux RTX2080 開發 / DGX Spark 生產）
    → RDMA libibverbs →
```

---

## 3. 5 步驟驗證流程

| 步驟 | Control | Grab | IP 模式 | 目的 |
|------|---------|------|--------|------|
| 1 | offline 連線 | ❌ | `offline-tcp` | 演算法驗證（MIL 影像）|
| 2 | validate | `--cam-count 1` | `rdma-validate` | 單 CCD 傳輸 |
| 3 | validate | `--cam-count ALL` | `rdma-validate` | 全陣列 + Switch |
| 4 | full + 上位機 | `--cam-count ALL` | `image-capture` | 存圖調 Recipe |
| 5 | full + 上位機 | `--cam-count ALL` | `online` | 完整生產 |

**Step 4 → Step 1 閉環**：image-capture 存的 TIFF 用於 Step 1 調 Recipe，保證 offline = inline。

---

## 4. 遷移決策

| Reference 來源 | 目標 | 方式 |
|---------------|------|------|
| `gpu_algo/src/cuda_kernels_fast.cu` | `ip/src/gpu/cuda_kernels.cu` | ✅ 直接複製不改 |
| `gpu_algo/src/tensor_core_classifier.cu` | `ip/src/ai/ai_kernels.cu` | ✅ 直接複製不改 |
| `gpu_algo/src/batch_detector.cpp` | `ip/src/gpu/gpu_pipeline.cpp` | 🔧 換 I/O 外殼 |
| `gpu_algo/config.ini` | `ip/config/default_zone.ini` | ✅ 參數對應 ZoneConfig（非「ZoneSetting」）|
| `phase1_tests/shared/FrameHeader.h` | `shared/FrameHeader.h` | ✅ 直接複製 |
| `phase1_tests/src/t31_*` | `grab/src/cam_*.cpp` | 🔧 升級多相機 |
| `legacy_win/PrjCfAoi/MainProc.cs` | `control/src/Controllers/UpstreamServer.cs` | 🔧 移除 MIL |
| MIL / FrameGrabber / CamProc | — | ❌ 完全移除 |

---

## 5. 通訊協議

### 上位機 → Control（文字命令）
> ⚠️ **考古確認（取代舊敘述）**：舊版 `Reference/legacy_win/PrjCfAoi`（`Common.cs` / `MainProc.cs` /
> `Configuration.cs`）實際使用 **port 8787**、命令前綴 **`CF_`**、`|` 分隔、`\r\n` 結尾，回應走
> 9 參數 `ReturnResponse`（`OK|p1|…|p9` / `ERR`）。命令常數：
> `CF_LOAD_RECIPE` / `CF_GRAB_START` / `CF_CHECK_ALIGN` / `CF_SET_ALIGN` / `CF_GET_RESULT`。
> `CF_GET_RESULT` 回傳的是各 IP 的 **ResultInfo.xml 路徑 + 缺陷數**（逗號分隔），**不是** JSON。
> 具體格式（已實作於 `control/src/Controllers/UpstreamServer.cs`）：
> `CF_LOAD_RECIPE|{recipe}|{panelId}|{yyyy-MM-dd-HH-mm-ss}|||||||{detectMode 0/1}`（panel 範例 "gg4mida"）、
> `CF_GRAB_START|{timeoutMs}`（範例 40000）、`CF_SET_ALIGN|{result}|{shiftX}|{shiftY}`；
> 回應一律 9 參數 `OK|p1|…|p8|{p9=errMsg}` 或 `ERR|…`。Control 監聽 port 由 appsettings `UpstreamServer.ListenPort`（**= 8787**）。
>
> ⚠️ **已寫程式碼、但尚未接線啟動、未與真實上位機驗證**：`control/src/Controllers/UpstreamServer.cs`
> 已照考古以 **CF_ 前綴 / `|` 分隔 / 9 參數** 寫好解析與回應邏輯（TcpListener@8787、`Split('|')`、CF_ switch），
> 舊的「port 8000 簡化介面 / `LoadRecipe|RECIPE|PANEL`」假設作廢。
> **但目前**：① 程式中**沒有任何地方 `.Start()` 啟動它**，`On*` 回呼也尚未接到 IpClient 流程；
> ② Step 1 是 offline，**從未接過真實上位機**。→ 屬「實作完成、待驗證」。接真實上位機時必須：
> 接線啟動 + 綁定 On* 回呼 + 用實機/模擬器驗證 9 參數格式與 `CF_GET_RESULT` 回傳內容。
> **IP 程式不直接實作 8787**，只對 Control 走 8200 JSON。

### Control ↔ Grab/IP（JSON，8100/8200）
```json
{"cmd":"LOAD_RECIPE","seq":1,"params":{"recipe":"DEFAULT","panel_id":"T001"}}
{"cmd":"SET_MODE","seq":2,"params":{"mode":"rdma-validate"}}
{"cmd":"CHECK_HEALTH","seq":3,"params":{}}
```

#### IP output 資料夾結構（考古對齊 legacy，result_saver 產生）
> 來源：`Reference/legacy_win/PrjCfAoi/Class/MainProc.cs`(L412) + `CamProc.cs`(L915) + `PrjAoiSettingEditor/frmSortDefect.cs`。
```
<--output>/<yyyyMMdd>/<panelId>_<recipeName>/      ← ① 日期夾（無分隔線）② 一塊 panel/批一夾
   Defect_<IpName>_Slice<ff>_Roi<rr>_Run<nn>_X<xxxx>_Y<yyyyyy>_Dr<Bright|Dark>.png   缺陷小圖（全域座標）
   <panelId>_<recipeName>_ResultInfo.json / .xml   結果（DefectCnt 來源）
   <panelId>_<recipeName>_result.png               overlay（PNG 低壓縮）
```
- 簡化為 **3 層**（省略 legacy 第 4 層 `<IpName>/`，新架構每台 IP 自有 output；IpName 進檔名）。
- `--ip-name`（預設 IP01）決定缺陷檔名；`panelId`=影像/panel 名、`recipeName`=zone 的 recipe 名。
- 日期格式 **`yyyyMMdd`**（與 legacy 一致；遠端命令的 `date` 參數同此格式，**非** `yyyy-MM-dd`）。
- **AI 分類預設停用**（訓練資料不足）：模型仍載入（保留架構），但不推論、不過濾，缺陷 `AiType="待人工複核"`；
  log 顯示「N 缺陷(待人工複核)」。`--use-ai` 重新啟用。
- **存圖效能**：overlay 用 PNG（非 BMP）、缺陷小圖多緒平行寫；調參可用 `--no-save-images`/`--no-overlay`/
  `--max-patches N` 加速。log 印 `存圖耗時: crop/patches/overlay ms`。

#### 缺陷遠端歸檔（DefectSort）— 缺陷影像存在運算端 IP/Linux/Spark，不在 Control 本地
Control 下命令、IP 就地處理、結果回傳（跨機免共用檔案系統；Control 端不假設能看到 IP 的硬碟）：
```json
// 列出 <output>/<yyyyMMdd>/ 下的 panel 夾。date="" = 掃所有日期夾彙整。每筆對應一塊 panel。
{"cmd":"LIST_DEFECT_FOLDERS","seq":4,"params":{"date":"20260614"}}
  → {"status":"OK","folders":[{"folder_name":"IP01_panelA_DEFAULT","panel_id":"IP01_panelA_DEFAULT",
                               "date":"20260614","defect_count":3}, ...]}
// IP 就地把選中 panel 夾的 Defect* 檔複製到 output/{output_subdir}，檔名加前綴 {folder}_。
//（by_id_folder=true → 依資料夾名前兩段 '_' token 建子夾，對應 legacy "By ID Folder"）。
{"cmd":"SORT_DEFECTS","seq":5,"params":{"date":"20260614","output_subdir":"sorted","by_id_folder":true,
                                        "selected_folders":["IP01_panelA_DEFAULT","IP01_panelB_DEFAULT"]}}
  → {"status":"OK","total":8,"output_dir":"/.../output/sorted",
     "results":[{"folder":"IP01_panelA_DEFAULT","copied":3},{"folder":"IP01_panelB_DEFAULT","copied":5}]}
```
> date="" 時 SORT 跨日期夾搜尋第一個同名 panel 夾。Step1View 的 OK/NG 即時手動分（少量即時缺陷在 Control 端操作）是另一用途，不走此遠端命令。

##### 小圖人工分類（DefectSort 第二層：看小圖標 TrueDefect/Particle，未來 AI 訓練標註）
雙擊資料夾進入；缺陷小圖在運算端，透過 network-clean 傳 PNG bytes 給 Control 顯示：
```json
// 1) 列出一塊 panel 夾所有缺陷小圖 metadata（patch_id=檔名；座標/型別解析自檔名、Size 讀 ResultInfo.json、
//    current_class 讀 classification.json）。
{"cmd":"LIST_DEFECT_PATCHES","seq":6,"params":{"date":"20260614","folder_name":"IP02_panelA_DEFAULT"}}
  → {"status":"OK","patches":[{"patch_id":"Defect_IP02_..._.png","run_index":0,"roi_index":0,
       "GC_X":10,"GC_Y":20,"Size":5,"Type":"Bright","current_class":"未分類"}, ...]}
// 2) 批次取小圖 PNG bytes（base64），一次 ~50 張避免逐張往返。
{"cmd":"GET_DEFECT_PATCHES_BATCH","seq":7,"params":{"date":"20260614","folder_name":"...","patch_ids":["...","..."]}}
  → {"status":"OK","patches":[{"patch_id":"...","png_base64":"iVBORw0K..."}, ...]}
// 3) 人工分類存回：IP 依分類複製到 {folder}/TrueDefect|Particle/ + 寫 classification.json（供 re-training 重用）。
{"cmd":"SAVE_DEFECT_CLASSIFICATION","seq":8,"params":{"date":"20260614","folder_name":"...",
       "classifications":[{"patch_id":"...","class":"TrueDefect"},{"patch_id":"...","class":"Particle"}]}}
  → {"status":"OK","TrueDefect":2,"Particle":1,"total":3,"output_dir":"/.../IP02_panelA_DEFAULT"}
```
> AI 暫停用 → 純人工分類；分類結果（classification.json + 子夾）即未來 AI 重訓的標註資料。
> Control 端 UI：縮圖牆 + 鍵盤 T/P 快速標 + ←→ 切換 + 點圖放大 + 頂部統計，小圖用 Avalonia Bitmap(PNG) 顯示。
> **分類即時持久化**：每標一張即送 SAVE（不必按 Sort）→ 中途離開回來、預設 filter「只顯示未分類」續標未標的；
> filter 另可切「顯示全部 / 只 TrueDefect / 只 Particle」複查。**存圖避免累積**：result_saver 每次存 patch 前
> 先清本層舊 `Defect_*`（換 IpName/換參數的舊檔），確保 DefectSort 張數 = 當次缺陷數（非倍數）。
> **調參加速**：SEND_IMAGE_FOR_REVIEW `debug=true` 才存全部 patch；預設只存結果+overlay。
> IP log `[T.T]`：GPU運算 / crop / patch存圖 / overlay存圖 / 收圖傳輸 各階段 ms。

### 配方（RecipeInfo.xml）與結果（ResultInfo.xml）格式 — 考古確認（取代舊「ZoneSetting/ThB/ThD」敘述）
> 來源已逐檔驗證：`Reference/legacy_win/ClibCf/Recipe.cs`、`JudgeResult.cs`、`CudaCore/CUDA_Func.h`。

- **配方 `RecipeInfo.xml` = 序列化 `Recipe`，每台 IP 一份**：
  `Recipe → M_AlignRoi + DetectRoiList(List<DetectRoi>) + DetectIoiList`。
  `DetectRoi`（~32 欄位）關鍵：ROI `StartX/StartY/EndX/EndY`；閾值 **`BrightThreshold`/`DarkThreshold`**
  （**不是** `ThB`/`ThD`）；幾何 `PitchX/PitchY/SearchX/SearchY`；模式 `AlgorithmWay` + **`AlgorithmCompare`("SUB"|"DIV")**；
  Blob 過濾 `BlobMaxSize/BlobMinSize/...`。
  - 裝置端 `CUDAZone` 結構內部欄位才叫 `float ThB/ThD`，由 `ThB=(float)BrightThreshold` 直接賦值。
- **只支援 `AlgorithmCompare="DIV"`**：gpu_algo kernel 是比例式（`center/mean₈(neighbors) vs BTH/DTH`），
  legacy DIV 同定義域 → `BTH=BrightThreshold`、`DTH=DarkThreshold` 嚴格相等對應。
  **SUB（灰階差）無法不依賴背景灰階精確轉成比例 → IP 直接拒絕載入並報錯。**
- **幾何欄位對應**（`DetectRoi`→ZoneConfig，見 ip/CLAUDE.md §5）：`PitchX→pitch_x`、`PitchY→pitch_y`、
  `SearchX→search_range_x`、`SearchY→search_range_y`，**`fast_search_range = clamp(SearchY,0,2)`**（kernel 實吃的垂直局部搜尋）；
  ROI `StartX/StartY/EndX/EndY`（-1=全幅，每個 DetectRoi 一個 zone）。
- **結果雙寫**（IP `result_saver.cpp`，兩者欄位一致）：
  `{panelId}_{recipeName}_ResultInfo.json`（Control 反序列化用）+ `{panelId}_{recipeName}_ResultInfo.xml`
  （= 序列化 `JudgeResult`，給上位機 `CF_GET_RESULT` 鏈相容）。
  `JudgeResult → RoiInfoList → RoiInfo → DefectInfoList → DefectInfo`（OK/NG 判定：`DefectCnt==0` 即 PASS）。
- **缺陷欄位一律用 legacy 名稱**（IP 輸出 JSON 與 XML 皆然）：
  `GC_X/GC_Y`、`Size`、`Width/Height`、`X_Min/X_Max/Y_Min/Y_Max`、`Type`(PointBright/PointDark)、
  `GL_Mean`、`GlobalPosX/GlobalPosY`、`Filter`(GPU 端已過濾 → `NoFilter`)。GPU 無的欄位（`CV_*`、`*_Sigma`）填 0。
- **多 ROI**：gpu_algo kernel 一次只吃單一參數組（無 zone 邊界），故 IP 對 `DetectRoiList` 每個
  `DetectRoi` 各裁切子影像跑一次，再合併（一個 zone = 一個 `RoiInfo`）。

### shared/FrameHeader.h（256 bytes）— = Phase-1 實機驗證版（magic 0xA01CF00D）
> 2026-06-11 於 damac↔spark-c16f 用 t21/t40 RDMA→GPU 全幀 CRC 驗證通過；`shared/FrameHeader.h` 已對齊此版。
> ⚠️ 舊 repo 版（magic 0xCFA0A001 + panel_id_hash/system_id/flags）**作廢**（從未經 RDMA）。
```cpp
#pragma pack(push,1)
struct FrameHeader {        // 固定欄位 64 + reserved 192 = 256
    uint32_t magic;         // FRAME_MAGIC = 0xA01CF00D
    uint16_t version;       // = 2
    uint16_t headerBytes;   // = 256
    uint64_t frameSeq;      // 也塞進 RDMA imm
    uint32_t panelId;       // 面板編號（IP make_frame_header 暫填 panel 字串 FNV hash）
    uint16_t camId; uint16_t sliceIndex; uint16_t totalSlice; uint16_t scanStep;
    uint32_t width;         // 8192
    uint32_t height;        // 5000
    uint16_t bitDepth;      // 8
    uint16_t pixelFormat;   // 0=Mono8
    uint64_t ptpTimestampNs;
    int32_t  machineCoordX; int32_t machineCoordY;   // 缺陷全域座標用
    uint32_t payloadBytes;
    uint32_t crc32;         // payload CRC32（IEEE 0xEDB88320）
    uint8_t  reserved[256 - 64];
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader)==256,"");
// 相容層（附加，非 wire）：make_frame_header() / frame_panel_hash()；phase1 無 system_id/flags，日後用 reserved 擴充
```

---

## 6. 平台

| 平台 | GPU | sm | CUDA | 記憶體策略 |
|------|-----|----|----|----------|
| Linux RTX2080 Super（開發）| RTX 2080S | 75 | 12.x | cudaMalloc + async |
| DGX Spark（生產，NVLink-C2C SoC）| GB10 | 121 | 13.0 | **`cudaHostAlloc(Portable\|Mapped)` + `cudaHostGetDevicePointer`**（⚠️ 不可用 nvidia_peermem，見不變式 11）|

---

## 7. 不變式

1. `cuda_kernels.cu` / `ai_kernels.cu` 禁止修改任何 `__global__` kernel 邏輯（host wrapper 編排可改，見 ip/CLAUDE.md 不變式 7）
2. `shared/FrameHeader.h` 兩端版本一致，`sizeof==256`
3. FrameHeader = Phase-1 實機驗證版，magic `0xA01CF00D`（舊 `0xCFA0A001` 版作廢，從未經 RDMA）；`shared/FrameHeader.h` 已對齊
4. 上位機命令名稱固定為 **`CF_` 前綴**（`CF_LOAD_RECIPE`/`CF_GRAB_START`/`CF_GET_RESULT` 等，port 8787，9 參數）不可改；
   舊文件的 `LoadRecipe/GrabStart/GetResult|RECIPE|PANEL` 假設已作廢
5. RecipeInfo.xml 格式與舊系統相容（= legacy `Recipe`；閾值 `BrightThreshold`/`DarkThreshold`，非 ThB/ThD；只收 DIV）
6. pylon/eBUS 路徑嚴格分離
7. 影像載入用 cv::IMREAD_UNCHANGED，禁止任何後處理
8. **network-clean**：Control↔IP 跨機（Mac↔Linux）不共用檔案系統 → 配方傳 **XML 內容**（非路徑）、
   結果 JSON 與缺陷小圖 PNG bytes 皆 **over TCP** 回傳；IP 不依賴對方硬碟，反之亦然
9. **output 同 panel 重測前先清空該 panel 夾的舊 `Defect_*`**（IP `result_saver` 已無條件清），
   避免 DefectSort 讀到換 IpName/換參數的歷史殘留疊加成倍數
10. 同一影像跑兩次結果 bit-exact（見 ip/CLAUDE.md 不變式 7/8）
11. **GB10（DGX Spark）RDMA 收圖不可用 `nvidia_peermem`，改 `cudaHostAlloc(Portable|Mapped)`**
    （2026-06-11 實機驗證，見 `docs/verification/verification_report_20260611.md`）：GB10 NVLink-C2C SoC
    的 GPU Bus ID 非標準 PCIe 空間 → `nvidia_peermem` 載入 EINVAL。正式 IP RDMA 接收用 pinned host memory
    （`cudaHostAlloc` Portable|Mapped）註冊 MR，GPU 經 `cudaHostGetDevicePointer` 透過 NVLink-C2C(~900GB/s)
    讀寫；`t40_e2e_server` 已採此法。詳見 grab/CLAUDE.md 不變式 6。
12. **跨架構一致性（x86 sm_75 ↔ ARM GB10 sm_121）**（2026-06-15 GB10 實機驗證，見
    `docs/verification/verification_report_arm_20260615.md`）：x86(off-line 調參)與 ARM(on-line 生產)跑**同一份 `ip/`**，
    **整數/幾何欄位完全一致**；唯**浮點閾值邊界像素可能 ULP 翻面**（單像素級、方向偏保守＝寧抓勿漏），
    缺陷預設 `待人工複核` 進 DefectSort 人工確認 → 可接受。**結論：off-line(x86)調的參數對 on-line(ARM)有效。**
    一致性/決定性只用未觸頂負載比對（觸頂 cap=10000 截斷為 race-dependent，非 bit-exact，僅供 perf）。

---

## 8. 工作流程紀律（完成的定義）

### 完成 = 驗證過（L1 → L3）

| 狀態 | 定義 |
|------|------|
| **L1** | 程式碼實作完成，尚未驗證。功能接線可能正確，但未在實機或合成測試中確認。STATUS.md 標 L1 的項目**不得合入 production 路徑**。|
| **L3** | 在目標平台（RTX 2080S 或 DGX Spark GB10）上跑過針對性驗證，貼出數據，結果符合規格。|

**規則：沒有驗證就不算完成。** 每次說「做完了」之前必須貼數據。

### 驗證最低要求

1. **Stage 1（合成影像）**：合成數據驗算法正確性，不依賴相機或真實面板。
2. **Stage 2（端對端）**：offline-tcp 或 rdma-validate 模式下跑 TCP 測試，貼數據（缺陷數、幀數、CRC、ShiftX/Y 誤差等）。
3. **Stage 3（失敗路徑）**：確認錯誤輸入 → 正確的 ERR 回傳，不 crash、不 silent fail。

每階段貼數據再繼續下一階段。「缺陷數 n0 vs n_aligned」、「幀數/CRC」、「ShiftX/Y 誤差」等關鍵數據必須顯示在 STATUS.md 或 commit message 裡。

---

## 9. 各模組 CLAUDE.md 索引

**新 session 工作流程：先讀本文件（總綱）→ 再讀目標模組 CLAUDE.md。**

| 模組 | 路徑 | 各自涵蓋（不重複總綱）|
|------|------|----------------------|
| IP（CUDA/GPU）| `ip/CLAUDE.md` | Reference 遷移表、CUDA kernel 不變式（禁改 kernel 本體）、ZoneConfig 參數對應、各平台支援模式、24 條 IP 不變式（含決定性/爆量陷阱/對位 pipeline/RDMA 背壓）|
| Grab（相機）| `grab/CLAUDE.md` | Reference 遷移表、pylon/eBUS SDK 嚴格分離、cam_manager、rdma_sender N-slot ring、8 條 Grab 不變式（含 GB10 cudaHostAlloc / RoCE MTU / 背壓握手）|
| Control（UI）| `control/CLAUDE.md` | C# Avalonia 架構、MVVM 模式、Zone 參數編輯表單（手寫 32 欄資料驅動）、連線設定、13 條 Control 不變式（含 CF_ 協議實作狀態 / AI 停用 / DefectSort）|
| tools（小工具）| `tools/README.md` | golden_maker 用途/用法（GUI 框選 Mark + CLI headless `--mark-rect`，輸出 AlignRoi XML）；未來其他小工具也記在這裡 |

**本文件（docs/CLAUDE.md）涵蓋（跨模組共識）：** 系統架構全貌、5 步驟驗證流程、跨模組通訊協議（CF_/JSON/RDMA）、RecipeInfo.xml/ResultInfo.xml 格式契約、FrameHeader wire format（§5）、平台說明（§6）、12 條跨模組不變式（§7）、完成定義（§8）。
