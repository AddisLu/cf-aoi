# CF-AOI 分散式架構 — 全域 CLAUDE.md

> Claude Code 根目錄 context。各程式子目錄（ip/grab/control）有各自的 CLAUDE.md。
> **核心策略：從 Reference/ 遷移現有程式碼，不從空白重寫。**

---

## 1. 目錄結構

```
~/cf-aoi/
├── docs/CLAUDE.md          ← 本文件
├── ip/                     ← IP 程式（C++ CUDA）
├── grab/                   ← Grab 程式（C++ pylon/eBUS）
├── control/                ← Control 程式（C# Avalonia）
├── shared/FrameHeader.h    ← RDMA wire format（兩端共用）
├── scripts/                ← bootstrap / 測試腳本
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
    ↕ TCP port 8000（文字命令 LoadRecipe|GrabStart|GetResult）
CONTROL（C# Avalonia，Linux/Windows）
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
| `gpu_algo/config.ini` | `ip/config/default_zone.ini` | ✅ 參數對應 ZoneSetting |
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
> 回應一律 9 參數 `OK|p1|…|p8|{p9=errMsg}` 或 `ERR|…`。Control 監聽 port 由 appsettings `UpstreamServer.ListenPort`（預設 **8787**）。
>
> 下面的 `LoadRecipe/GrabStart/GetResult`（port 8000）是**新版 Control 對外**要呈現的簡化介面；
> Control 的 `UpstreamServer` 負責把它對映回上位機所需格式。**IP 程式不直接實作 8787**，
> 只對 Control 走 8200 JSON。
```
LoadRecipe|RECIPE|PANEL\r\n   →  OK\r\n        （Control 對外簡化介面）
GrabStart|PANEL\r\n
GetResult\r\n                  →  OK|{json}\r\n
```

### Control ↔ Grab/IP（JSON，8100/8200）
```json
{"cmd":"LOAD_RECIPE","seq":1,"params":{"recipe":"DEFAULT","panel_id":"T001"}}
{"cmd":"SET_MODE","seq":2,"params":{"mode":"rdma-validate"}}
{"cmd":"CHECK_HEALTH","seq":3,"params":{}}
```

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
- **結果 `{IpName}_ResultInfo.xml` = 序列化 `JudgeResult`**：
  `JudgeResult → RoiInfoList → RoiInfo → DefectInfoList → DefectInfo`（OK/NG 判定：`DefectCnt==0` 即 PASS）。
- **缺陷欄位一律用 legacy 名稱**（IP 輸出 JSON 與 XML 皆然）：
  `GC_X/GC_Y`、`Size`、`Width/Height`、`X_Min/X_Max/Y_Min/Y_Max`、`Type`(PointBright/PointDark)、
  `GL_Mean`、`GlobalPosX/GlobalPosY`、`Filter`(GPU 端已過濾 → `NoFilter`)。GPU 無的欄位（`CV_*`、`*_Sigma`）填 0。
- **多 ROI**：gpu_algo kernel 一次只吃單一參數組（無 zone 邊界），故 IP 對 `DetectRoiList` 每個
  `DetectRoi` 各裁切子影像跑一次，再合併（一個 zone = 一個 `RoiInfo`）。

### shared/FrameHeader.h（256 bytes）
```cpp
#pragma pack(push,1)
struct FrameHeader {
    uint32_t magic;        // FRAME_MAGIC = 0xCFA0A001（合法 hex）
    uint32_t version;
    uint64_t timestamp_ns;
    uint32_t panel_id_hash;
    uint16_t cam_id;
    uint16_t frame_seq;
    uint32_t width;        // 8192
    uint32_t height;       // 5000
    uint8_t  pixel_format; // 0=Mono8
    uint8_t  system_id;    // 0=Reflection 1=Transmission
    uint16_t flags;        // bit0=last_frame
    uint32_t payload_bytes;
    uint32_t crc32;
    uint8_t  padding[/*補齊到256*/];
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader)==256,"");
```

---

## 6. 平台

| 平台 | GPU | sm | CUDA | 記憶體策略 |
|------|-----|----|----|----------|
| Linux RTX2080 Super（開發）| RTX 2080S | 75 | 12.x | cudaMalloc + async |
| DGX Spark ARM（生產）| GB10 | 121 | 13.0 | zero-copy mapped |

---

## 7. 不變式

1. `cuda_kernels.cu` / `ai_kernels.cu` 禁止修改任何 kernel 邏輯
2. `shared/FrameHeader.h` 兩端版本一致，`sizeof==256`
3. magic 用合法 hex `0xCFA0A001`（不是 0xCFAOI001）
4. 上位機命令名稱（LoadRecipe/GrabStart/GetResult）不可改
5. RecipeInfo.xml 格式與舊系統相容
6. pylon/eBUS 路徑嚴格分離
7. 影像載入用 cv::IMREAD_UNCHANGED，禁止任何後處理
8. 同一影像跑兩次結果 bit-exact
