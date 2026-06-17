# Control 程式完整說明

> 版本：2026-06-17 整理（對齊 Reference/PrjCfAoi 考古 + 補 §12.1 legacy 細項缺口）
> 專案：`control/src/CfAoiControl.csproj`（.NET 8 / Avalonia UI）
> 技術棧：C# + Avalonia + CommunityToolkit.Mvvm + SixLabors.ImageSharp

---

## 目錄

1. [系統概述](#1-系統概述)
2. [架構對比：Legacy vs. Control](#2-架構對比legacy-vs-control)
3. [整體分層架構](#3-整體分層架構)
4. [軟體流程圖](#4-軟體流程圖)
5. [主要類別職責](#5-主要類別職責)
6. [UI 視窗與功能](#6-ui-視窗與功能)
7. [TCP 協議（上位機 / IP / Grab）](#7-tcp-協議上位機--ip--grab)
8. [配方管理](#8-配方管理)
9. [離線分析流程（Step 1）](#9-離線分析流程step-1)
10. [缺陷整理（DefectSort）](#10-缺陷整理defectsort)
11. [連線管理與心跳](#11-連線管理與心跳)
12. [功能對照表：Legacy → Control](#12-功能對照表legacy--control)
13. [待接線 / 待驗證清單](#13-待接線--待驗證清單)
14. [設定檔說明](#14-設定檔說明)
15. [SelfTest 無頭驗證](#15-selftest-無頭驗證)
16. [關鍵檔案索引](#16-關鍵檔案索引)

---

## 1. 系統概述

Control 是 CF-AOI 分散式架構的**中間協調器**，負責：

- 接收**上位機（Master Controller）** 的 TCP 指令（port 8787，CF_ 前綴），協調 Grab / IP。
- 提供**操作介面**：離線影像分析、配方編輯、缺陷整理、系統設定。
- 橋接 Grab 機（port 8100）與 IP 機（port 8200）的 JSON 命令。
- 配方 XML over TCP 送 IP（network-clean，雙機免共用檔案系統）。

與 Legacy PrjCfAoi 最大不同：**不做任何 GPU 計算、不接相機、不用 MIL**。
所有取像交 Grab 機；所有 GPU 缺陷檢測交 IP 機。Control 只做協調與 UI。

---

## 2. 架構對比：Legacy vs. Control

```
Legacy PrjCfAoi（單機 Windows WinForms）
┌───────────────────────────────────────────────┐
│ PrjCfAoi（主執行檔）                           │
│   MainProc ── CamProc[] ── FrameGrabber       │
│              └─ MIL PatternMatch（對位）       │
│              └─ CudaCore（GPU 缺陷檢測）       │
│              └─ AiProc（ONNX AI 分類）         │
│   TCP 上位機（CF_ 命令）                        │
│   PrjAoiSettingEditor（離線配方編輯器）         │
│   PrjAlgorithmTestTools（離線驗證工具）         │
│   PrjTestBaslerCom（相機通訊測試）              │
└───────────────────────────────────────────────┘

                          ↕ 遷移

CF-AOI 分散式架構（Control + Grab + IP）
┌─────────────────────────┐
│ 上位機（Master PC）      │
│  port 8787  CF_ 命令    │
└──────────┬──────────────┘
           ↕ TCP
┌──────────▼──────────────────────────────────────┐
│ CONTROL（C# Avalonia，本文件）                   │
│  TCP Server @8787（UpstreamServer）             │
│  ┌────────────────────────────────────────────┐ │
│  │ MainWindow            （主視窗）            │ │
│  │   Step1View           （Algorithm Test）   │ │
│  │   ZoneParamEditorView （IP Param Editor）  │ │
│  │   DefectSortView      （Sort Defect）      │ │
│  │   SystemSettingsView  （系統設定）          │ │
│  └────────────────────────────────────────────┘ │
│  IpClient  @8200   GrabClient @8100             │
└──────────┬──────────┬──────────────────────────┘
           ↓ TCP JSON ↓ TCP JSON
    ┌──────▼───┐  ┌───▼──────┐
    │ IP 機     │  │ Grab 機  │
    │ GPU 檢測  │  │ 相機取像  │
    │ port 8200 │  │ port 8100│
    └───────────┘  └──────────┘
```

---

## 3. 整體分層架構

```
╔═══════════════════════════════════════════════════════╗
║ UI 層 (Avalonia MVVM)                                 ║
║   Views/      ← axaml 雙向資料綁定                    ║
║   ViewModels/ ← ObservableProperty + RelayCommand     ║
╚═══════════════════════════════════════════════════════╝
                        ↑ DI / AppServices
╔═══════════════════════════════════════════════════════╗
║ 服務層 (Services/)                                    ║
║  AppServices      手動 DI 容器（組裝全部服務）         ║
║  RecipeStore      配方單一資料來源（RecipeReloaded 事件）║
║  RecipeService    RecipeInfo.xml 讀寫 / 自動生成       ║
║  OfflineReviewService  離線影像→IP→讀回結果            ║
║  PitchEstimator   純 managed FFT 估算 PitchX/Y        ║
║  LogService       多通道日誌（Info/Warn/Error）        ║
║  ConfigLoader     appsettings.json 讀寫               ║
║  SelfTest         --selftest 無頭驗證                 ║
╚═══════════════════════════════════════════════════════╝
                        ↑
╔═══════════════════════════════════════════════════════╗
║ 控制層 (Controllers/)                                 ║
║  UpstreamServer   TCP Server @8787（CF_ 命令）         ║
║  IpClient         TCP Client @8200（IP JSON 命令）     ║
║  GrabClient       TCP Client @8100（Grab JSON 命令）   ║
║  ConnectionManager 心跳/重連（2.5s 週期）              ║
╚═══════════════════════════════════════════════════════╝
                        ↑
╔═══════════════════════════════════════════════════════╗
║ 資料模型層 (Models/)                                  ║
║  SystemConfigModel  appsettings.json 綁定             ║
║  RecipeModel        Recipe XML 對應（= ClibCf/Recipe）║
║  ZoneSettingModel   DetectRoi 32 欄位 + ObservableObject║
║  DefectResultModel  IP 結果 JSON 反序列化（JudgeResult）║
║  ShareSettingModel  全域旗標（appsettings ShareSetting）║
║  RecipeSavingModel  per-recipe RecipeSetting.xml      ║
╚═══════════════════════════════════════════════════════╝
```

---

## 4. 軟體流程圖

### 4.1 啟動流程

```
Program.Main()
  ├─ args 含 --selftest → SelfTest.RunAsync()（無頭，不啟動 GUI）
  └─ BuildAvaloniaApp().StartWithClassicDesktopLifetime()
       ↓
     App.axaml.cs → 建立 MainWindow
       ↓
     MainWindowViewModel 建構子
       ├─ AppServices.Build()
       │    ├─ ConfigLoader.Load()    讀 appsettings.json
       │    ├─ LogService.new()
       │    ├─ ConnectionManager.new()
       │    ├─ RecipeService.new()
       │    ├─ RecipeStore.new()      → RefreshNames() + Select("DEFAULT")
       │    └─ OfflineReviewService.new()
       ├─ Step1ViewModel.new()
       ├─ ZoneParamEditorViewModel.new()
       ├─ DefectSortViewModel.new()
       ├─ LogService.Logged → 路由到 SysLog/ErrorLog/WarningLog
       ├─ ConnectionManager.Start()   → 啟動 IP + Grab 心跳迴圈（背景）
       └─ DispatcherTimer(1s) → 更新 IsIpConnected / CurCamStatus

⚠ UpstreamServer 尚未在任何地方 .Start()（見 §13 待接線清單）
```

### 4.2 Step 1 離線分析流程

```
使用者：點「離線分析工具 (Algorithm Test)」
  ↓  MainWindow.OpenAlgoTest() → 開 Step1View 視窗
  
Step1View
  ├─ [Browse Image] → FilePicker → LoadImageForDisplay()
  │    ├─ ImageSharp.Image.Load<L8>()   讀灰階影像
  │    ├─ BuildDisplayBitmap()          建 WriteableBitmap（全解析度顯示）
  │    └─ PixelData 保存原始 bytes      供像素值讀出
  │
  ├─ [FFT 估算] → PitchEstimator.Estimate()    2D FFT 估算 PitchX/Y
  │    └─ [套用] → Store.PrimaryZone.PitchX/Y = 估算值
  │
  └─ [Test（RunAnalysis）]
       ↓
     OfflineReviewService.AnalyzeAsync(imagePath, recipe)
       ├─ ImageSharp.Load<L8>() → payload = raw Mono8 bytes
       ├─ RecipeService.ToXmlString(recipe) → recipeXml
       ├─ IpClient.LoadRecipeAsync(recipeName, panel, recipeXml)
       │    → {"cmd":"LOAD_RECIPE","params":{"recipe":..,"recipe_xml":..,"panel_id":..}}
       ├─ IpClient.SendImageStreamBeginAsync(panel)
       └─ IpClient.SendImageForReviewAsync(panel, camId, w, h, payload, debug)
            → JSON 命令行 + raw bytes（Mono8）over TCP
            ← {"status":"OK","result":{DefectCnt,RoiInfoList,...}}
            
            ↓ 回傳 DefectResultModel
     BuildVisuals(imagePath, result)
       ├─ BuildDisplayBitmap() 重建顯示圖
       ├─ NavDefects.AddRange(result.AllDefects)   導航清單
       ├─ 縮圖牆（封頂 200 張）裁切 64×64 patch
       ├─ DefectCntAtCap / density 警告
       └─ ResultVersion++ → 通知 code-behind 重綁 overlay（紅圈亮/藍圈暗）

  └─ [OK/NG 歸檔] → FileSelectedDefect("ok"/"ng")
       → 裁切 100×100 patch → 存 {OutputDir}/ok(ng)/{name}.png
```

### 4.3 配方編輯流程（IP Param Editor）

```
使用者：點「配方編輯 (IP Param Editor)」
  ↓  MainWindow.OpenParamEditor() → 開 ZoneParamEditorView 視窗
  
  RecipeStore.RecipeReloaded → ZoneParamEditorViewModel.OnRecipeReloaded()
    ├─ 重建 RoiCheckItem[] = Recipe.DetectRoiList
    └─ SelectedRoi = Rois[0]
    
  [選取 ROI] → SelectedRoi → _target.Zone = roi.Zone（共用實例）
  [編輯參數]  → ParamRow.TextValue/BoolValue/EnumValue setter → ZoneSettingModel 屬性即時更新
               （共用同一 ZoneSettingModel 實例 → Step1/主視窗同步）
  
  [Update（某列）] → 若 UpdateWithAsk → 顯示確認 → ConfirmApply()
                  → row.ApplyTo(checked_rois)  批次套用
  [Update（批次）] → ParamRow.IsChecked 的所有列 × Roi.IsChecked 的所有 ROI
  
  [ROI 位移 x-/x+/y-/y+] → Zone.StartX/EndX/StartY/EndY ± ShiftStep
  [套用 ROI 範圍] → 選取 ROI 的 StartX/Y/EndX/Y 複製到勾選的 ROI 們
  
  [Save] → RecipeStore.SaveAsync() → RecipeService.Save(recipeName, recipe, "IP0")
         → {RecipeDir}/{recipe}/IP0/RecipeInfo.xml
```

### 4.4 缺陷整理流程（DefectSort）

```
使用者：點「缺陷整理 (Sort Defect)」
  ↓  MainWindow.OpenSortDefect() → 開 DefectSortView 視窗

【第一層：Panel 資料夾列表】
  [Parse] → IpClient.ListDefectFoldersAsync(yyyyMMdd)
          ← {"status":"OK","folders":[{folder_name, panel_id, defect_count}, ...]}
          → Folders DataGrid 填入
          
  [Sort（勾選/全選）] → IpClient.SortDefectsAsync(date, outputSubdir, byId, selected)
          → IP 就地複製 Defect_* 到 output/{outputSubdir}（非 Control 本地）
          ← {"status":"OK","total":N,"results":[{folder, copied}, ...]}

【第二層：小圖人工分類（雙擊資料夾）】
  OpenFolderAsync(folder)
    ├─ IpClient.ListDefectPatchesAsync(date, folder)
    │   ← {patches:[{patch_id, GC_X/Y, Size, Type, current_class}, ...]}
    ├─ filter 套用（預設「只顯示未分類」，續標上次未標的）
    └─ LoadThumbnailsAsync() → 批次（50張）GetDefectPatchesBatchAsync()
         ← {patches:[{patch_id, png_base64}]}
         → Bitmap.decode → PatchItem.Thumb
         
  [T 鍵 / MarkTrue] → ClassifySelected("TrueDefect")
                    → PersistOne() → 即時 SAVE_DEFECT_CLASSIFICATION（fire-and-forget）
                    → filter「只顯示未分類」時：標完即消失；自動跳下一張
  [P 鍵 / MarkParticle] → ClassifySelected("Particle")

  [SaveClassification] → 整批重送（保險），IP 歸檔到 {folder}/TrueDefect|Particle/
                       + 寫 classification.json（AI 重訓標註）
```

### 4.5 上位機命令處理流程（UpstreamServer，⚠ 尚未 Start()）

```
上位機 → TCP port 8787 → UpstreamServer.HandleClientAsync()
  ├─ CF_LOAD_RECIPE|{recipe}|{panelId}|{datetime}|||||||{detectMode}
  │    → OnLoadRecipe(recipe, panelId, detectMode)（callback 未接線）
  │    ← OK|||||||||  或  ERR|||||||||{errMsg}
  │
  ├─ CF_GRAB_START|{timeoutMs}
  │    → OnGrabStart(timeoutMs)（callback 未接線）
  │    ← OK|||||||||
  │
  ├─ CF_CHECK_ALIGN         ← OK|Cs_AlignSet|0|0||||||||（固定 0 偏移，新架構無 MIL 對位）
  ├─ CF_SET_ALIGN|result|shiftX|shiftY  ← OK|||||||||（接受但無操作）
  │
  ├─ CF_GET_RESULT
  │    → OnGetResult()（callback 未接線）
  │    ← OK|{filePaths}|{defectCounts}|||||||
  │
  └─ 未知命令  ← ERR|||||||||unknown cmd: {cmd}
```

---

## 5. 主要類別職責

### 服務層

| 類別 | 檔案 | 職責 |
|------|------|------|
| `AppServices` | `Services/AppServices.cs` | **手動 DI 容器**：`Build()`/`DesignTime()` 兩種組裝方式 |
| `RecipeStore` | `Services/RecipeStore.cs` | **配方單一資料來源**：所有 ViewModel 共用同一 Recipe/PrimaryZone 實例；`RecipeReloaded` 事件廣播切換 |
| `RecipeService` | `Services/RecipeService.cs` | RecipeInfo.xml 讀寫；自動生成預設 DIV 配方；跨平台路徑展開（`~`） |
| `OfflineReviewService` | `Services/OfflineReviewService.cs` | 影像→IP 分析：LoadRecipe + SendImage + 讀回結果；network-clean（配方內容 over TCP）|
| `PitchEstimator` | `Services/PitchEstimator.cs` | 純 managed 2D FFT 估算 PitchX/Y（SNR 信心度）|
| `LogService` | `Services/LogService.cs` | 多通道日誌（Info/Warn/Error）；`Logged` 事件；ViewModel 訂閱路由到 SysLog/ErrorLog/WarningLog |
| `ConfigLoader` | `Services/ConfigLoader.cs` | appsettings.json 讀寫（`Microsoft.Extensions.Configuration` 綁定）；`SaveShareSetting()` 只改 ShareSetting 節點 |
| `SelfTest` | `Services/SelfTest.cs` | `--selftest parse/recipe/send/fft/store/heartbeat/sort/patches/settings` 無頭驗證 |

### 控制層

| 類別 | 檔案 | 職責 |
|------|------|------|
| `UpstreamServer` | `Controllers/UpstreamServer.cs` | TCP Server @8787；解析 CF_ 命令（Split('｜')）；9 參數 OK/ERR 回應；callbacks 待接線 |
| `IpClient` | `Controllers/IpClient.cs` | TCP Client @8200；newline-delimited JSON；支援 binary payload（SEND_IMAGE_FOR_REVIEW）；`SemaphoreSlim` 確保序列 |
| `GrabClient` | `Controllers/GrabClient.cs` | TCP Client @8100；newline-delimited JSON；目前只有 CHECK_HEALTH（Step 2+ 擴充）|
| `ConnectionManager` | `Controllers/ConnectionManager.cs` | 心跳迴圈（2.5s）；自動重連；只在狀態變化時記 log；`IsBusy` 跳過心跳 |

### ViewModel 層

| 類別 | 對應 UI | 對應 Legacy |
|------|---------|------------|
| `MainWindowViewModel` | MainWindow.axaml | `frmCfAoi`（主視窗）|
| `Step1ViewModel` | Step1View.axaml | `frmAlgorithmTestTools`（離線驗證）|
| `ZoneParamEditorViewModel` | ZoneParamEditorView.axaml | `frmIpParamEditor`（IP 參數編輯）|
| `DefectSortViewModel` | DefectSortView.axaml | `frmSortDefect` + `frmViewDefect`（缺陷整理/分類）|
| `SystemSettingsViewModel` | SystemSettingsView.axaml | `frmSetting`（設定）|

---

## 6. UI 視窗與功能

### 6.1 主視窗 MainWindow（1424×881）

```
┌────────────────────────────────────────────────────────────────┐
│ 左欄                              │ 右欄（2×2 格）              │
│ ┌──── Status ─────────────────┐   │ ┌ Config ┐ ┌ Recipe ┐      │
│ │ Cur Command / Status        │   │ │(路徑/IP│ │配方下拉│      │
│ │ Cur Recipe / Panel ID       │   │ │/Port顯 │ │PrimaryZ│      │
│ │ Cur Detect Mode             │   │ │示)     │ │one預覽 │      │
│ │ ● IP 連線燈                  │   │ └────────┘ └────────┘      │
│ └─────────────────────────────┘   │ ┌ShareSet┐ ┌RecipeSet┐     │
│ ┌──── Reserve ─────────────────┐  │ │SaveSrc │ │Max Save │     │
│ │ [CF Load Recipe] [Grab]↓     │  │ │DebugAlg│ │Defect   │     │
│ │ [Check Align]↓ [Set Align]↓  │  │ │AiRoot  │ │AiOkCount│     │
│ │ [CF Get Result] [Stop]↓      │  │ │(TuningR│ │...      │     │
│ │ [Save Config] [Save Recipe]  │  │ │SaveFull│ │         │     │
│ │ [Refresh]                    │  │ │Bypass↓)│ │[儲存]   │     │
│ │ [離線分析工具] [配方編輯]     │  │ │[儲存]  │ │         │     │
│ │ [缺陷整理] [顯示/隱藏進階]   │  │ └────────┘ └─────────┘     │
│ └─────────────────────────────┘   │                             │
│ ┌──── 系統 log（SysLog）───────┐   │                             │
│ └─────────────────────────────┘   │                             │
│ ┌ Error │ Warning 分頁 ────────┐  │                             │
│ └─────────────────────────────┘   │                             │
└────────────────────────────────────────────────────────────────┘
StatusStrip: AiModel | OfflineFolder
```

**進階 CF 按鈕**（Ctrl+F 顯示）：
- `CF Load Recipe` → 送 LOAD_RECIPE 給 IP（含最新配方 XML）
- `CF Grab Start` → **停用**（offline 模式不取像）
- `CF Check Align` / `CF Set Align` → **停用**（新流程無 MIL 對位）
- `CF Get Result` → 送 GET_STATUS 給 IP
- `CF Stop` → **停用**（offline 無停止對象）

### 6.2 Algorithm Test（Step1View，1064×681）

| 區域 | 功能 |
|------|------|
| 影像區 | 全解析度灰階顯示（WriteableBitmap，nearest-neighbor）；缺陷 overlay（紅圈亮 / 藍圈暗）|
| 工具列 | [Browse Image]、[Test]、[FFT 估算]、[套用 FFT]、[Save Recipe]、[OK 歸檔]、[NG 歸檔]、[重繪標示] |
| 快速調參 | BrightThreshold / DarkThreshold / PitchX / PitchY（直接綁 Store.PrimaryZone → 共用）|
| Debug 勾選 | DebugSaveDefectPatches：Test 時請 IP 存全部缺陷小圖（供 DefectSort 分類用）|
| 縮圖牆 | 最多 200 張 64×64 縮圖；點選跳轉大圖導航 |
| 導航 | ←/→ 遍歷全部缺陷（無封頂）|
| 8 格狀態列 | ImageSize / Axis / Value / Zoom / Selected / Recipe / Region / DefectCnt |
| 警告 | 缺陷數達 10000 上限（參數過嚴或 Pitch 不符）；密度 > 50/Mpx（整片誤報）|

### 6.3 IP Param Editor（ZoneParamEditorView，1280×797）

| 區域 | 功能 |
|------|------|
| 左：ROI 清單 | Roi_0/1/2…（可勾選批次）；Region 顯示選取 ROI 的邊界 |
| 中：參數列表 | 27 個參數（CheckBox + Label + 值輸入 + 單列 Update）；「IP待接」標籤顯示 IP 未消化的欄位 |
| 右：ROI 位移 | x-/x+/y-/y+ 按 ShiftStep 移動（夾 [0, 8160]）|
| 批次 Update | 勾選多列 + 多 ROI → 批次套用（UpdateWithAsk=true 先顯示確認框）|
| 套用 ROI 範圍 | 把當前選取 ROI 的邊界複製到勾選的所有 ROI |
| 全選/清除 | CheckBox 批次選取/取消 |
| Save | 存到 {RecipeDir}/{recipe}/IP0/RecipeInfo.xml |

**參數分類（27 個）：**

| 分類 | 參數 |
|------|------|
| 前處理 | ImagePreProc / SmoothTimes / SmoothTimes2 |
| 閾值 | DarkThreshold / BrightThreshold / SobelEnable / SobelDark / SobelBright |
| 演算法 | AlgorithmWay / AlgorithmCompare (只允許 DIV) / AlgorithmWayCompare / Adjustment / PitchTime / ChooseAmount |
| 核心（IP 吃的） | PitchX / PitchY / SearchX / SearchY |
| 邊緣 | EdgePassRatio / EdgePassThreshold |
| Blob | BlobMaxSize / BlobMinSize / BlobElongation / BlobFeretElong / BlobDarkMergeDistance / BlobBrightMergeDistance / BlobAllMergeDistance |

### 6.4 Sort Defect（DefectSortView，1103×542）

**第一層（Panel 資料夾列表）：**
- 日期選擇器（DatePicker） → Parse → LIST_DEFECT_FOLDERS → DataGrid（Sort 勾選 / FolderName / DefectCount）
- 輸出子目錄（文字框）、By ID（勾選）、SortAll（勾選）
- Sort → SORT_DEFECTS → log 顯示每個 panel 複製數 + 總計

**第二層（小圖人工分類）：**
- 雙擊資料夾 → LIST_DEFECT_PATCHES → 顯示縮圖牆 + 統計（Total/Classified/TrueDefect/Particle/Unclassified）
- Filter 切換：只顯示未分類 / 顯示全部 / 只 TrueDefect / 只 Particle
- 鍵盤 T（TrueDefect）/ P（Particle）快速分類 → 即時 SAVE_DEFECT_CLASSIFICATION
- ←/→ 切換；Back 回第一層；SaveClassification 整批重送

### 6.5 系統設定（SystemSettingsView）

- IP Host / Port（唯讀顯示 appsettings 值）
- RecipeDir / OutputDir / ImageDir（唯讀顯示）
- 上位機 Port（唯讀顯示，= 8787）
- [Test Connection] → ConnectAsync + CHECK_HEALTH → 顯示結果

---

## 7. TCP 協議（上位機 / IP / Grab）

### 7.1 上位機 → Control（port 8787）

格式：`CF_{CMD}|p1|p2|…\r\n`，回應 9 參數 `OK|p1|…|p8|{p9=errMsg}\r\n`

| 命令 | 格式 | 回應 |
|------|------|------|
| CF_LOAD_RECIPE | `CF_LOAD_RECIPE|{recipe}|{panelId}|{datetime}|||||||{detectMode}` | `OK|||||||||` 或 `ERR|||||||||{msg}` |
| CF_GRAB_START  | `CF_GRAB_START|{timeoutMs}` | `OK|||||||||` |
| CF_CHECK_ALIGN | `CF_CHECK_ALIGN` | `OK|Cs_AlignSet|0|0|||||||`（固定 0 偏移）|
| CF_SET_ALIGN   | `CF_SET_ALIGN|{result}|{shiftX}|{shiftY}` | `OK|||||||||` |
| CF_GET_RESULT  | `CF_GET_RESULT` | `OK|{filePaths}|{defectCounts}|||||||` |
| CF_READY       | `CF_READY` | `OK|||||||||` |

### 7.2 Control → IP（port 8200，JSON）

格式：`{"cmd":..,"seq":..,"params":{..}}\n`，回應：`{"seq":..,"status":"OK"|"ERR",...}\n`

| 命令 | 用途 | 重要 params |
|------|------|------------|
| CHECK_HEALTH | 心跳 | 無 |
| GET_STATUS | 取狀態/結果 | 無 |
| LOAD_RECIPE | 載入配方 | `recipe`, `recipe_xml`（XML 全文），`panel_id` |
| SEND_IMAGE_STREAM_BEGIN | 開始送圖 | `panel_id` |
| SEND_IMAGE_FOR_REVIEW | 送影像（命令行 + raw bytes）| `panel_id`, `cam_id`, `width`, `height`, `payload_bytes`, `last`, `debug` |
| LIST_DEFECT_FOLDERS | 列缺陷資料夾 | `date`（yyyyMMdd）|
| SORT_DEFECTS | 遠端歸類缺陷 | `date`, `output_subdir`, `by_id_folder`, `selected_folders[]` |
| LIST_DEFECT_PATCHES | 列一個 panel 的小圖 metadata | `date`, `folder_name` |
| GET_DEFECT_PATCHES_BATCH | 批次取小圖（base64 PNG，~50 張） | `date`, `folder_name`, `patch_ids[]` |
| SAVE_DEFECT_CLASSIFICATION | 存人工分類 | `date`, `folder_name`, `classifications[{patch_id, class}]` |

### 7.3 Control → Grab（port 8100，JSON）

目前只有 `CHECK_HEALTH`（心跳）；Step 2+ 取像命令待擴充。

---

## 8. 配方管理

### 8.1 目錄結構

```
{RecipeDir}/                           預設 ~/cf-aoi/recipes
└── {RecipeName}/
    ├── RecipeSetting.xml              per-recipe 存圖設定（MaxSaveDefectCount 等）
    └── IP0/
        └── RecipeInfo.xml             AlignRoi + DetectRoiList[] 配方主檔
```

### 8.2 RecipeInfo.xml 格式

對齊 legacy `ClibCf/Recipe.cs`（XML 序列化 `RecipeModel`）：

```xml
<Recipe>
  <M_AlignRoi>
    <AlignEnable>false</AlignEnable>
    <PatternPath></PatternPath>
    ...
  </M_AlignRoi>
  <DetectRoiList>
    <DetectRoi>
      <StartX>-1</StartX><StartY>-1</StartY>
      <EndX>-1</EndX><EndY>-1</EndY>          <!-- -1 = 全幅 -->
      <BrightThreshold>1.4</BrightThreshold>   <!-- DIV 比例域 BTH -->
      <DarkThreshold>0.6</DarkThreshold>        <!-- DIV 比例域 DTH -->
      <AlgorithmCompare>DIV</AlgorithmCompare>  <!-- 只支援 DIV -->
      <PitchX>26</PitchX><PitchY>19</PitchY>
      <SearchX>1</SearchX><SearchY>1</SearchY>
      ...（共 32 欄位）
    </DetectRoi>
  </DetectRoiList>
  <DetectIoiList/>
</Recipe>
```

### 8.3 RecipeStore（Single Source of Truth）

```
RecipeStore.SelectedRecipe 變更 → Select(name)
  → RecipeService.EnsureRecipeExists(name)
      (配方不存在 → 自動生成預設 DIV 配方，UI 顯示黃色警告)
  → Recipe = 載入的 RecipeModel
  → PrimaryZone = Recipe.DetectRoiList[0]  （共用實例）
  → RecipeReloaded 事件 → 所有訂閱的 ViewModel 更新 UI

所有 ViewModel 讀/寫的都是同一個 ZoneSettingModel 實例
（Step1 快速調參 ↔ ZoneParamEditor 參數列 ↔ MainWindow Recipe 預覽 同步）
```

---

## 9. 離線分析流程（Step 1）

1. 選影像（Browse Image）→ `ImageSharp.Load<L8>`，全解析度顯示。
2. 可選：FFT 估算 PitchX/Y → 套用到 PrimaryZone。
3. 可選：ZoneParamEditor 調整 BrightThreshold / DarkThreshold / PitchX/Y 等。
4. [Test] → `OfflineReviewService.AnalyzeAsync()`：
   a. 影像轉 Mono8 raw bytes（純 managed）。
   b. `RecipeService.ToXmlString(recipe)` → XML 字串 over TCP 送 IP（network-clean）。
   c. `IpClient.LoadRecipeAsync()` → IP 載入本次配方。
   d. `IpClient.SendImageForReviewAsync()` → 命令行 + binary payload。
   e. IP 回 `{"status":"OK","result":{...}}`。
5. `DefectResultModel` 反序列化結果 → 建縮圖牆 + overlay。
6. [OK/NG 歸檔] → 裁切 100×100 缺陷 patch 存本機（AI 訓練樣本）。

---

## 10. 缺陷整理（DefectSort）

**設計原則（network-clean）**：缺陷影像存在 IP/Linux 端，Control 不假設能看到 IP 磁碟。
所有操作透過 JSON 命令：

```
Control 端（UI 操作）         IP 端（就地執行）
─────────────────────         ──────────────────
Parse → LIST_DEFECT_FOLDERS → 回傳 folder list
Sort  → SORT_DEFECTS        → IP 就地 copy 到 output/sorted/
進入夾 → LIST_DEFECT_PATCHES → 回傳 metadata（含 current_class）
取縮圖 → GET_DEFECT_PATCHES_BATCH → 回 PNG base64（~50 張/批）
T/P分類 → SAVE_DEFECT_CLASSIFICATION → IP 歸檔 + 寫 classification.json
```

**即時持久化**：每標一張即送 SAVE（fire-and-forget），中途離開回來 current_class 保留。

---

## 11. 連線管理與心跳

```
ConnectionManager.Start()
  ├─ IP  HeartbeatLoop（背景 Task）：
  │    每 2.5s：
  │      若 IsBusy（有命令持有鎖）→ 視為存活，跳過
  │      否則：
  │        未連線 → ConnectAsync（2s timeout）
  │        CHECK_HEALTH（2s timeout）→ OK → IsIpConnected=true
  │        失敗 → Disconnect() → IsIpConnected=false → 下回合重連
  │      狀態變化才記 log（首次連線 / 中斷 / 重連）
  │
  └─ Grab HeartbeatLoop（同架構，Step 1 若無 Grab 則靜默重試）

MainWindowViewModel DispatcherTimer(1s) → 更新 IsIpConnected 顯示燈
```

---

## 12. 功能對照表：Legacy → Control

| Legacy 功能 | Control 狀態 | 說明 |
|------------|-------------|------|
| TCP 上位機 port 8787 CF_ 命令 | ✅ 實作完成 | UpstreamServer.cs；但尚未 Start() |
| CF_LOAD_RECIPE | ✅ 解析/回應完成 | callback OnLoadRecipe 未接到 IpClient |
| CF_GRAB_START | ✅ 解析/回應完成 | callback OnGrabStart 未接到 GrabClient |
| CF_CHECK_ALIGN | ⚠️ Stub | 固定回 OK + 0 偏移；新架構無 MIL 對位 |
| CF_SET_ALIGN | ⚠️ Stub | 接受命令但無操作 |
| CF_GET_RESULT | ✅ 解析/回應完成 | callback OnGetResult 未接線 |
| 多相機並行（CamProc[]） | ❌ Step 2+ | 新架構由 Grab 機多相機，Control 單 TCP |
| FrameGrabber 7 種後端 | ❌ 移除 | 改為 GrabClient TCP 控制 Grab 機 |
| CudaCore GPU 缺陷檢測 | ❌ 移除 | 改為 IpClient TCP 控制 IP 機 |
| MIL Pattern Match 對位 | ❌ 移除 | 新架構無對位（或由 IP 機處理） |
| AiProc ONNX AI 分類 | ⚠️ 停用 | AI 架構保留在 IP 端但不推論 |
| 離線演算法驗證工具 | ✅ 完整實作 | Step1View（過 TCP 送 IP 分析） |
| 配方 IP Param Editor | ✅ 完整實作 | ZoneParamEditorView（27 個欄位批次編輯）|
| 缺陷檢視/排序（frmSortDefect）| ✅ 完整實作 | DefectSortView（遠端命令版）|
| 小圖人工分類（frmViewDefect）| ✅ 完整實作 | DefectSortView 第二層（縮圖牆 + T/P）|
| ShareSetting.xml 全域設定 | ✅ 遷移 | appsettings.json ShareSetting 節點 |
| RecipeSetting.xml per-recipe | ✅ 遷移 | {RecipeDir}/{recipe}/RecipeSetting.xml |
| LogRecorder（Sys/Err/Warn）| ✅ 實作 | LogService（3 通道），主視窗 3 個清單 |
| Basler 相機通訊（frmBaslerCom）| ❌ 移除 | 相機控制由 Grab 機負責 |
| frmVariance 統計圖 | ❌ 未實作 | 新架構尚未需要 |
| BootConfig.xml 啟動配置 | ✅ 遷移 | appsettings.json（JSON 格式）|
| 配方 RecipeInfo.xml 格式 | ✅ 完整相容 | RecipeModel / ZoneSettingModel 32 欄位 |
| JudgeResult / DefectInfo 格式 | ✅ 完整相容 | DefectResultModel（JSON + XML 雙解析）|

**新增（不在 Legacy 中）：**

| 功能 | 說明 |
|------|------|
| network-clean 設計 | 配方 XML + 結果 JSON + 缺陷小圖 PNG 全走 TCP，雙機免共用檔案系統 |
| FFT Pitch 估算 | PitchEstimator（純 managed；SNR 信心度）|
| 遠端 DefectSort | LIST/SORT/CLASSIFY 命令讓 IP 就地操作（不用 samba/nfs）|
| 即時分類持久化 | 標每張即 SAVE（中途離開不遺失）|
| 自動配方生成 | 找不到配方時自動生成預設 DIV（黃色警告）|
| 自動重連心跳 | 2.5s 週期，斷線靜默重試，狀態 LED 即時更新 |
| SelfTest 無頭驗證 | --selftest 多模式（parse/recipe/send/fft/store/heartbeat/sort/patches/settings）|
| 跨平台 | Avalonia .NET 8（Linux/Windows/macOS 共一份程式）|

### 12.1 考古補充（2026-06-17）：§12 未涵蓋 / 細項缺口（legacy 有、Control 缺）

> Reference 路徑正名：`legacy_win` = **`Reference/PrjCfAoi/`**。以下為 2026-06-17 逐檔考古（含 MainProc/CamProc/Common/Configuration）補出、§12 未列或敘述不足者，file:line 為實際讀到。

| legacy 功能 | legacy 位置(file:line) | Control 現狀 | 評估 |
|------------|----------------------|-------------|------|
| **Rule 改判**（ImageRuleEnable / MeanLowThreshold / HdivWThreshold / NgSizeThreshold）| `CamProc.cs:816-847` | **完全缺** | AI 又停用 → 現行**無任何自動 OK 改判**，缺陷全進人工複核。生產若要降過殺需補（**新缺口**）|
| **多通道 log**（7 通道：Sys/Err/NetRec/NetSend/Prc/Msk/GetImg）| `LogMgr.vb:11-17` | LogService 只 3 通道（Sys/Err/Warn）| NetRec/NetSend/Prc/Msk/GetImg 5 通道缺；網路收發/處理診斷能力降低（**新缺口，次要**）|
| **配方 SaveAs / Align / 多 IP 同步** | `frmAoiSettingEditor.cs:358-690` | RecipeStore 只處理單 IP0 | 跨 IP 配方同步 / SaveAs / Align 未完整遷移（**新缺口**）|
| **AI 模型管理 UI**（掃 .onnx / 刪除 / 配方關聯）| `frmAiModelManager.cs:36-72` | 只有 AiRootPath 設定欄位 | AI 停用 → 管理 UI 未遷移（L0）|
| **frmVariance 模糊度統計**（呼叫 Python blurring）| `frmVariance.cs:73-300` | 無 | §12 已標「未實作」，確認缺 |
| **MaskGen 掩碼生成** | `LibAoiSetting/frmMaskGen.cs` | 無 | 掩碼 ROI 繪製無對應（**新缺口**）|
| **離線工具滑鼠繪製 ROI**（增刪/拖矩形）| `frmAlgorithmTestTools.cs:474-643` | ZoneParamEditor 只有數值位移 x±/y± | 滑鼠繪製 ROI 缺；只能改數值（**新缺口，次要**）|
| Interest ROI（IOI）存圖 | `CamProc.cs:1547-1614`（DetectIoiList）| 無 | IOI 興趣區存圖無對應（IP 端 `<IoiInfoList/>` 也空）|
| AutoFlash 待機閃頻 / 登入權限（frmLogin）/ Basler 串口控制 | `AutoFlash.cs` / `frmAoiSettingEditor.cs:1487-1577` / `frmBaslerCom.cs` | 無 | 確認缺（多為產線/硬體周邊，多數可不補）|
| CF_STOP（中斷取像）/ BypassAlignment review | `MainProc.cs:999-1015` / `CamProc.cs:1688-1812` | UpstreamServer 無 CF_STOP；BypassAlignment 旗標存在但停用 | 確認缺（offline 無停止對象；review_offset 機制無對應）|

> ⚠️ **legacy 三處根目錄常數不一致**（考古發現）：`Common.cs`=`D:\Cf_Aoi`、`Configuration.cs`=`D:\Transfer_Aoi`、`BootConfig.vb`=`D:\uLedInspAOI`——疑為跨產品線（CF/Transfer/uLed AOI）共用碼庫殘留，非單一真相。新架構用 appsettings.json `Paths`（`~` 展開），無此問題。
>
> ⚠️ **μm 座標契約 follow-up（與 Gap #5 同條）**：legacy runtime 缺陷一律 pixel（`CamProc.cs` 未乘 OptRes），CF_GET_RESULT 只回 ResultInfo.xml 路徑+缺陷數。IP 端 Gap #5 新增 `GlobalPosX_um` 為**片面提議**，上位機是否讀 μm 待接真機確認——與 **UpstreamServer 接真實上位機**（§13）屬同一條 follow-up。

---

## 13. 待接線 / 待驗證清單

| 項目 | 現況 | 接線方法 |
|------|------|---------|
| **UpstreamServer.Start()** | 未呼叫，上位機無法連線 | 在 `App.axaml.cs` 或 `MainWindowViewModel` 建立並 `Start()`；用 `AppServices` 管理 lifetime |
| **OnLoadRecipe callback** | null → 接收命令但不執行 | 接到 `IpClient.LoadRecipeAsync()` + `RecipeStore.Select()` |
| **OnGrabStart callback** | null → 接收命令但不執行 | 接到 `GrabClient.SendCommandAsync("GRAB_START")` |
| **OnGetResult callback** | null → 回 ("","0") | 接到 `IpClient.GetStatusAsync()` + 解析 DefectCnt |
| **CF_CHECK_ALIGN 真實版** | 固定回 0 偏移 | 新架構可能永遠 Stub，或改由 IP 機做對位並回傳 |
| **GrabClient 取像命令** | 只有 CHECK_HEALTH | Step 2+：GRAB_START / GRAB_STOP / GET_FRAME |
| **RecipeSetting 送 IP** | XML 存在但未發給 IP | LOAD_RECIPE params 帶上 MaxSaveDefectCount / SaveAiTrain 等 |
| **DebugAlgorithm → IP** | Step1 Test 有 debug flag | SEND_IMAGE_FOR_REVIEW `debug` 參數已接，但全域 ShareSetting 未自動帶 |
| **與真實上位機驗證** | 從未接過真實上位機 | 接線啟動後用模擬器或真實上位機驗證 9 參數格式 + CF_GET_RESULT 回傳內容 |

---

## 14. 設定檔說明

### appsettings.json

```json
{
  "UpstreamServer": {
    "ListenPort": 8787,   // 上位機 TCP port（固定 8787，不可改）
    "Optional": true      // 連線失敗不阻塞啟動
  },
  "Nodes": {
    "IpOffline": { "Host": "192.168.72.2", "Port": 8200, "Mode": "offline-tcp" },
    "IpOnline":  { "Host": "192.168.10.11", "Port": 8200, "Mode": "online" },
    "GrabA":     { "Host": "192.168.10.21", "Port": 8100 }
  },
  "ActiveIpNode": "IpOffline",   // 目前使用的 IP 節點
  "Paths": {
    "RecipeDir": "~/cf-aoi/recipes",
    "OutputDir": "~/cf-aoi/output",
    "ImageDir":  "~/cf-aoi/test_images"
  },
  "ShareSetting": {
    "SaveSourceImage": false,      // 存原圖
    "DebugAlgorithm": false,       // 全域預設：存全部缺陷小圖
    "AiRootPath": "",              // AI/配方根目錄（空=用 RecipeDir）
    "TuningRecipe": false,         // 停用（新架構未啟用）
    "SaveFullImage": false,        // 停用（多幀合圖/MIL）
    "BypassAlignment": false       // 停用（無 MIL 對位）
  }
}
```

路徑展開規則（`RecipeService.ExpandPath()`）：
- `~` → `Environment.SpecialFolder.UserProfile`（Windows UserProfile / Linux/Mac $HOME）
- `~/a/b` → `{home}/a/b`
- 絕對路徑 `/a/b` 或 `C:\a\b` 原樣

---

## 15. SelfTest 無頭驗證

```
CfAoiControl --selftest <子命令> [參數]

子命令         說明
─────────────────────────────────────────────────────────────────
parse  <json> <xml>   驗證 IP 輸出 JSON/XML 解析一致（step 3）
recipe <name>         生成 RecipeInfo.xml 供 IP 載入（step 2 前半）
send   <img> <name>   offline-tcp：連 IP 送圖讀回結果（step 4）
fft    <img>          FFT Pitch 估算（驗證 managed 邏輯）
store  [name]         配方單一資料來源（共用實例 + 存讀 round-trip）
heartbeat [secs]      心跳偵測連線狀態監看（N 秒輸出）
sort                  LIST/SORT 遠端命令（假 IP server，不需 GPU）
patches               小圖分類完整鏈（LIST/BATCH/SAVE + filter 邏輯）
settings              ShareSetting/RecipeSetting round-trip + Step1 初值
```

---

## 16. 關鍵檔案索引

| 主題 | 檔案 |
|------|------|
| 進入點 | [Program.cs](../control/src/Program.cs) |
| DI 容器 | [Services/AppServices.cs](../control/src/Services/AppServices.cs) |
| 系統設定模型 | [Models/SystemConfigModel.cs](../control/src/Models/SystemConfigModel.cs) |
| 設定讀寫 | [Services/ConfigLoader.cs](../control/src/Services/ConfigLoader.cs) |
| 配方資料來源 | [Services/RecipeStore.cs](../control/src/Services/RecipeStore.cs) |
| 配方讀寫 | [Services/RecipeService.cs](../control/src/Services/RecipeService.cs) |
| 離線分析 | [Services/OfflineReviewService.cs](../control/src/Services/OfflineReviewService.cs) |
| FFT Pitch | [Services/PitchEstimator.cs](../control/src/Services/PitchEstimator.cs) |
| 日誌 | [Services/LogService.cs](../control/src/Services/LogService.cs) |
| 無頭驗證 | [Services/SelfTest.cs](../control/src/Services/SelfTest.cs) |
| 上位機 Server | [Controllers/UpstreamServer.cs](../control/src/Controllers/UpstreamServer.cs) |
| IP 連線 | [Controllers/IpClient.cs](../control/src/Controllers/IpClient.cs) |
| Grab 連線 | [Controllers/GrabClient.cs](../control/src/Controllers/GrabClient.cs) |
| 心跳重連 | [Controllers/ConnectionManager.cs](../control/src/Controllers/ConnectionManager.cs) |
| 配方模型 | [Models/RecipeModel.cs](../control/src/Models/RecipeModel.cs) |
| Zone 參數 | [Models/ZoneSettingModel.cs](../control/src/Models/ZoneSettingModel.cs) |
| 結果模型 | [Models/DefectResultModel.cs](../control/src/Models/DefectResultModel.cs) |
| 主視窗 ViewModel | [ViewModels/MainWindowViewModel.cs](../control/src/ViewModels/MainWindowViewModel.cs) |
| Step1（離線分析） | [ViewModels/Step1ViewModel.cs](../control/src/ViewModels/Step1ViewModel.cs) |
| 配方編輯 | [ViewModels/ZoneParamEditorViewModel.cs](../control/src/ViewModels/ZoneParamEditorViewModel.cs) |
| 缺陷整理 | [ViewModels/DefectSortViewModel.cs](../control/src/ViewModels/DefectSortViewModel.cs) |
| 系統設定 | [ViewModels/SystemSettingsViewModel.cs](../control/src/ViewModels/SystemSettingsViewModel.cs) |
| 主視窗 XAML | [Views/MainWindow.axaml](../control/src/Views/MainWindow.axaml) |
| 主視窗 code-behind | [Views/MainWindow.axaml.cs](../control/src/Views/MainWindow.axaml.cs) |
| 設定檔 | [appsettings.json](../control/src/appsettings.json) |

---

*本文件由原始碼逐檔靜態分析整理，對照 Reference/PrjCfAoi/程式完整說明.md 交叉驗證。2026-06-17 補 §12.1 legacy 細項缺口（逐檔考古 MainProc/CamProc/Common/Configuration，file:line 為實際讀到）。格式對齊 [ip_程式完整說明.md](ip_程式完整說明.md) / [grab_程式完整說明.md](grab_程式完整說明.md)。*
