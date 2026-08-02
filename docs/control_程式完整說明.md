# Control 程式完整說明

> 版本：**2026-08-02 全面對齊**（dashboard/導覽收斂 5 頁、相機工作台 §6.7、觸發鏈 CF_GRAB_START/CF_STOP 已接、
> GrabClient 11 命令、34 列參數表、三域守門）。前版：2026-06-17 整理（Reference/PrjCfAoi 考古 + §12.1）。
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
│  │ MainWindow（單一視窗，側欄 5 頁切換）        │ │
│  │   dashboard           （主控台）            │ │
│  │   CameraWorkbenchView （相機工作台，§6.7）  │ │
│  │   Step1View           （檢測複判）          │ │
│  │   DefectSortView      （缺陷分類）          │ │
│  │   SystemSettingsView  （系統設定=連線）     │ │
│  │   ↳ SingleCcdSetupView 內嵌於工作台 Step4  │ │
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

> ⚠️ 導覽收斂（2026-07-31）後所有畫面皆為**主視窗內切換頁**（`IsVisible` 綁 `CurrentScreen`），
> 下述流程中的「開 XXX 視窗」一律讀作「切到該頁」；資料流邏輯不變。獨立導覽入口現為 5 頁：
> 主控台 / 相機工作台 / 檢測複判(Step1View) / 缺陷分類(DefectSort) / 系統設定。

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
       ├─ Step1ViewModel / ZoneParamEditorViewModel / DefectSortViewModel.new()
       ├─ SystemSettingsViewModel.new()（載入 array_topology + 綁定引擎）
       ├─ SingleCcdSetupViewModel.new()（組合獨立 Step1+ZoneEditor 實例）
       ├─ CameraWorkbenchViewModel.new(svc, SysSettings, SingleCcdSetup)（工作台五步驟）
       ├─ LogService.Logged → 路由到 SysLog/ErrorLog/WarningLog
       ├─ ConnectionManager.Start()   → 啟動 IP + Grab 心跳迴圈（背景）
       ├─ UpstreamWiring.Bind(svc.Upstream, svc) + svc.Upstream.Start()  → 上位機 CF_/8787 監聽（已接線）
       └─ DispatcherTimer(1s) → 更新 IsIpConnected / IsUpstreamConnected / CurCamStatus
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
  
  [Save] → RecipeStore.SaveAsync() → RecipeService.Save(recipeName, recipe, SelectedIp)
         → {RecipeDir}/{recipe}/{SelectedIp}/RecipeInfo.xml   （SelectedIp = 分區儲存鍵，預設 IP0；
            工作台選槽時 = 該槽 recipe_partition）
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

### 4.5 上位機命令處理流程（UpstreamServer + UpstreamWiring，**已接線 2026-06-19**）

接線：`AppServices.Build()` 建 `UpstreamServer(8787)`（Optional，監聽失敗不阻塞）；`MainWindowViewModel` ctor 呼叫
`UpstreamWiring.Bind(svc.Upstream, svc)` 綁回呼後 `Start()`。回呼**重用既有 IP 流程**（`Controllers/UpstreamWiring.cs`）：

```
上位機 → TCP port 8787 → UpstreamServer.HandleClientAsync()  （client 連上 → OnConnectedChanged → 上位機燈轉綠）
  ├─ CF_LOAD_RECIPE|{recipe}|{panelId}|{datetime}|||||||{detectMode}
  │    → OnLoadRecipe = RecipeStore.Select(recipe) + IpClient.LoadRecipeAsync(recipe,panelId,xml)
  │    ← OK（IP 回 OK）/ ERR（IP 未連或失敗）
  ├─ CF_GET_RESULT
  │    → OnGetResult = IpClient.ListDefectFoldersAsync("") → 組「路徑,逗號 + 缺陷數,逗號」(非 JSON)
  │    ← OK|{paths}|{counts}|...（端到端實得例：OK|IP04_Origin000001_DEFAULT,…|0,0,0,0,1,1）
  │
  ├─ CF_GRAB_START|{timeoutMs}                                          ★2026-07-21 觸發鏈已接
  │    → OnGrabStart = GrabClient.GrabStartAsync(timeoutMs, Grab.FramesPerPanel)（已 ARM 時僅 ms 級）
  │    ← OK / ERR（Grab 未連線或失敗 → 誠實 ERR，不假 OK；fpp=0 連續模式會 Log.Warn）
  ├─ CF_STOP（#25）                                                      ★2026-07-21 觸發鏈已接
  │    → OnStop = GrabClient.GrabStopAsync()（停取像＋斷 RDMA，解除 ARM）
  │    ← OK / ERR
  ├─ CF_CHECK_ALIGN  → 不綁 → ERR|...|CHECK_ALIGN 未支援（待 #1/Step4）  ★誠實失敗（不再回假 OK|0|0）
  ├─ CF_SET_ALIGN    → 不綁 → ERR|...|SET_ALIGN 未支援（待 #1/Step4）    ★誠實失敗（不再永遠 OK）
  ├─ CF_READY        ← OK
  └─ 未知命令         ← ERR|...|unknown cmd: {cmd}
```
註：CF_LOAD_RECIPE 除送 IP 外，另做 **Grab 預熱**（Grab LOAD_RECIPE + GRAB_ARM，冪等；預熱失敗不擋
LOAD_RECIPE、只 Log.Warn，延遲風險由 CF_GRAB_START 誠實回報）。

> **分級（不混）**：`--selftest upstream`/`grabtrigger` in-process = **L2**；`scripts/upstream_simulator.py` ↔ 真 Control ↔ 真 IP 端到端 = **L3 ✓(2026-06-19；2026-07-31 生產迴圈重驗)**。
> **真上位機協議認帳（欄位/序列/μm 是否如實機預期）= L4 做不了**；**μm 契約(#5)= IP 片面提議 = L4**。
> 決策 A：對位（CHECK/SET_ALIGN）仍不綁 → 誠實失敗；**#25 CF_STOP / CF_GRAB_START 已接真 Grab（2026-07-21）**；#26 BypassAlignment 未做。

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
| `SelfTest` | `Services/SelfTest.cs` | `--selftest` 無頭驗證，18 子命令：parse/recipe/send/fft/store/heartbeat(+auto)/sort/patches/settings/camera/topology/singleccd/upstream/**grabtrigger/remoteimg/recipemgmt/recipesaving/workbench** |

### 控制層

| 類別 | 檔案 | 職責 |
|------|------|------|
| `UpstreamServer` | `Controllers/UpstreamServer.cs` | TCP Server @8787；解析 CF_ 命令（Split('｜')）；9 參數 OK/ERR 回應；**已 Start()**；未綁的回呼（CHECK/SET_ALIGN）→誠實失敗 ERR |
| `UpstreamWiring` | `Controllers/UpstreamWiring.cs` | 靜態 `Bind(server, svc)` 把 CF_ 回呼接到既有流程：OnLoadRecipe→`IpClient.LoadRecipeAsync` **+ Grab LOAD_RECIPE/GRAB_ARM 預熱**、**OnGrabStart→`GrabStartAsync`（fpp 傳遞）、OnStop→`GrabStopAsync`**、OnGetResult→`ListDefectFoldersAsync`、OnConnectedChanged→`SetUpstreamConnected`；CHECK/SET_ALIGN 不綁 |
| `IpClient` | `Controllers/IpClient.cs` | TCP Client @8200；newline-delimited JSON；支援 binary payload（SEND_IMAGE_FOR_REVIEW/CHECK_ALIGN）；`SemaphoreSlim` 確保序列；另有 LIST_DIR/GET_IMAGE_PREVIEW/REVIEW_LOCAL_IMAGE（遠端影像）|
| `GrabClient` | `Controllers/GrabClient.cs` | TCP Client @8100；**11 命令**（見 §7.3）：列舉/綁定/觸發鏈/曝光增益/TUNE_MEAN/機器層參數 |
| `ConnectionManager` | `Controllers/ConnectionManager.cs` | 心跳迴圈（2.5s；**5s 逾時 × 連續 2 次失敗才判斷線**＝★8）；自動重連；`SetUpstreamConnected`（上位機 inbound 燈）；`IsBusy` 跳過心跳 |

### ViewModel 層

| 類別 | 對應 UI | 對應 Legacy |
|------|---------|------------|
| `MainWindowViewModel` | MainWindow.axaml | 主視窗（dashboard + 側欄 5 頁導覽 + log 路由 + 上位機接線/心跳啟動）|
| `CameraWorkbenchViewModel` | CameraWorkbenchView.axaml | **新（2026-07-31）**：相機工作台五步驟（§6.7）；重用 SystemSettings 綁定引擎 + SingleCcdSetup |
| `Step1ViewModel` | Step1View.axaml | `frmAlgorithmTestTools`（檢測複判/離線驗證；含遠端影像模式）|
| `ZoneParamEditorViewModel` | （無獨立導覽入口）| `frmIpParamEditor`（**34 列** ParamRow + 對位 Mark；表單由 SingleCcdSetupView 進階摺疊呈現）|
| `DefectSortViewModel` | DefectSortView.axaml | `frmSortDefect` + `frmViewDefect`（缺陷整理/分類）|
| `SystemSettingsViewModel` | SystemSettingsView.axaml | 連線設定頁 + **綁定/相機引擎**（topology join 四態/SET_CAM_MAP/曝光增益——UI 呈現移至工作台，引擎與工作台共用同一實例）|
| `SingleCcdSetupViewModel` | SingleCcdSetupView.axaml | 單 CCD 檢測工作台（內嵌於工作台 Step 4），組合 `Step1ViewModel`(影像) + `ZoneParamEditorViewModel`(ROI/參數) 兩實例 |
| `RemoteImageBrowserViewModel` | RemoteImageBrowserView.axaml | **新**：「從 IP 載入」遠端影像瀏覽 modal（LIST_DIR 導航）|

### 控制項 / 模型（新增，2026-06-19）

| 類別 | 檔案 | 職責 |
|------|------|------|
| `RoiImageView` | `Controls/RoiImageView.axaml(.cs)` | 影像/ROI 共用控制項（從 Step1View 抽出）；StyledProperty 介面：`Source/ImageWidth/ImageHeight/PixelData/EditZone/AllZones/Defects/SelectedDefectIndex/Caption` + 輸出 `AxisText/ValueText/ZoomText/RegionText`；縮放/平移/框 ROI 把手/數值/量測/缺陷 overlay。**EditZone 可注入=編任一 ROI、AllZones 畫全部 ROI** |
| `ArrayTopologyModel` | `Models/ArrayTopologyModel.cs` | 機台層拓樸宣告（`ccd_total_count` + `ComputeUnitModel[]` + `CcdSlotModel[]`）；`Parse/Load`（本機 `config/array_topology.json` 優先，回退 `.example`）。約束①(ccd_id/recipe_partition 並存)②(宣告≠綁定) |
| `ComputeUnitGroup` | `ViewModels/SystemSettingsViewModel.cs` | 運算單元分群（工作台左欄槽位來源）；卡片欄位（連線/處理 N/負載% 估算）**目前無 XAML 綁定**（宣告陣列 UI 移除後保留供日後） |
| `SlotBinding` / `SlotBindKind` | `ViewModels/SystemSettingsViewModel.cs` | 宣告槽 × 偵測相機 join 結果（Gap #21）：四態 Bound/MacMismatch/Offline/Declared；工作台槽位卡的資料來源 |
| `CameraInfoModel` | `Models/CameraInfoModel.cs` | LIST_CAMERAS 列舉結果；`Status` 以 grab 的 `bound` 判定（**非** persistent，2026-07-30 語意修正）|
| `MacUtil` | `Models/MacUtil.cs` | MAC 正規化（去 `:`/`-`/`.`/空白轉大寫），行為對齊 grab `CamManager::normalize_mac`；不正規化就比對＝假「MAC 不符」告警 |

---

## 6. UI 視窗與功能

### 6.1 主視窗 MainWindow（dashboard + 側欄導覽）

> 2026-07-31 導覽收斂改版：單一視窗、側欄 5 頁（**主控台 / 相機工作台 / 檢測複判 / 缺陷分類 / 系統設定**），
> 各頁以 `IsVisible` 切換（不另開視窗）。頂列 = 檢測模式徽章 + 目前命令 + IP/Grab/上位機三連線燈
> + A/B 主題切換（控制室/實驗室，DynamicResource token 即時換膚）。

**主控台（dashboard）內容：**

- 狀態磚 6 格：目前命令 / 相機狀態 / 目前配方 / Panel ID / 檢測模式 / AI 模型。
- CF 命令卡：LOAD_RECIPE、GET_RESULT（⚠️ 已知限制：此本地鈕現送 `GET_STATUS`，與上位機 CF_GET_RESULT 路徑不同）、
  SAVE_CONFIG、SAVE_RECIPE、REFRESH；GRAB/ALIGN/STOP 本地鈕停用（取像/停止由上位機 CF_ 觸發鏈驅動，見 §4.5）。
- 配方卡：配方下拉（RecipeStore）+ BTH/DTH/PitchX/PitchY 預覽（綁 PrimaryZone，共用實例即時同步）。
- 系統 log 三分頁（系統/錯誤/警告，各含筆數；LogService.Logged 經 Dispatcher 路由）。

連線燈資料流：`ConnectionManager`（背景心跳）→ `MainWindowViewModel` 以 `DispatcherTimer(1s)` 輪詢複本 → 頂列燈。

### 6.2 檢測複判（Step1View；原 Algorithm Test，2026-07-31 寬幅版面）

> 導覽名「檢測複判」：線上 Run 貨異常時 Load 存圖（本機 / 從 IP 遠端）→ Test → 看檢出與誤判原因。
> 版面與工作台 Step 4 同比例（影像全寬橫幅）。功能表如下（邏輯未變）：

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

### 6.3 IP Param Editor（34 列參數表；獨立頁現無導覽入口）

> 表單邏輯在 `ZoneParamEditorViewModel`（**34 列** `ParamRow`＝27 列 legacy＋SUB/融合/LSC 新欄 7 列），
> UI 由 SingleCcdSetupView「進階參數」摺疊面板呈現（§6.6）；`Views/ZoneParamEditorView.axaml` 檔案保留但無導覽入口。

| 區域 | 功能 |
|------|------|
| ROI 清單 | Roi_0/1/2…（可勾選批次）；選中 ROI = EditZone（影像上框/拖同步）|
| 參數列表 | 34 個參數（CheckBox + Label + 值輸入 + 單列 Update）；「IP待接」標籤顯示 IP 未消化的欄位 |
| ROI 位移 | x-/x+/y-/y+ 按 ShiftStep 移動（夾 [0, 8160]）|
| 批次 Update | 勾選多列 + 多 ROI → 批次套用（UpdateWithAsk=true 先顯示確認框）|
| 套用 ROI 範圍 | 把當前選取 ROI 的邊界複製到勾選的所有 ROI |
| Save | 存到 {RecipeDir}/{recipe}/{SelectedIp}/RecipeInfo.xml（SelectedIp=分區儲存鍵）|

**參數分類（34 個）：**

| 分類 | 參數 |
|------|------|
| 前處理 | ImagePreProc / SmoothTimes / SmoothTimes2 |
| 閾值 | DarkThreshold / BrightThreshold / SobelEnable / SobelDark / SobelBright |
| 演算法 | AlgorithmWay / AlgorithmCompare（**stale 欄位**；權威=`M_AlgorithmWayCompare` 三域守門，見 docs/CLAUDE.md §5）/ AlgorithmWayCompare（★ mode 選擇器 SUB/DIV-voting/DIV）/ Adjustment / PitchTime / ChooseAmount |
| 核心（IP 吃的） | PitchX / PitchY / SearchX / SearchY |
| 邊緣 | EdgePassRatio / EdgePassThreshold |
| Blob | BlobMaxSize / BlobMinSize / BlobElongation / BlobFeretElong / BlobDarkMergeDistance / BlobBrightMergeDistance / BlobAllMergeDistance |
| 融合/LSC（SUB 管線移植新增 7 欄）| MeanLowThreshold / EnableMultiscale / LscEnable / LscK1 / LscK2 / LscK3 / LscMaxGain |

### 6.4 缺陷分類 Sort Defect（DefectSortView）

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

**僅剩「連線設定」一個 tab**（2026-07-31 導覽收斂：原「相機參數 tab／宣告陣列／運算單元帶／點槽進單 CCD」
已全部整併進相機工作台 §6.7；`SystemSettingsViewModel` 的綁定/相機引擎仍在，由工作台重用**同一實例**）：

- IP Host / Port、上位機 Port、RecipeDir / OutputDir / ImageDir（值來自 appsettings.json）。
- [測試連線] → 對輸入的 Host/Port 連線 + CHECK_HEALTH，結果顯示於下方。
- 節點狀態卡：Grab 連線燈。
- ⚠️ 已知限制：此頁欄位編輯**不落盤**（無儲存命令，appsettings.json 需手改）；「測試連線」直接以共用 IpClient 連測試位址。

### 6.6 單 CCD 檢測工作台（SingleCcdSetupView；2026-07-31 寬幅改版，內嵌於工作台 Step 4）

8160×3000/5000 線掃圖 = 超寬幅 → 版面為「**上方全寬影像橫幅（RoiImageView，高 330）+ 下方三欄**」：

- 上：全寬 `RoiImageView`（滾輪縮放/平移/框 ROI 把手/量 Pitch；`EditZone`=選中 ROI、`AllZones`=全部 ROI）
  + 緊貼狀態列（ImageSize/Axis/Value/Zoom/Region）。
- 下三欄：**快速調參**（BTH/DTH/PitchX/Y + FFT 估算 + ROI 清單）｜**檢出結果**（缺陷數 + 縮圖牆，點縮圖大圖定位）
  ｜**進階**（34 列參數全表 + 對位 Mark，皆預設摺疊）。
- header：CCD 名 + 由哪台運算 + [載入影像]/[從 IP 載入]/[Test 送 IP]/[儲存配方]。
- 組合既有 `Step1ViewModel`(影像) + `ZoneParamEditorViewModel`(ROI/參數) 兩**獨立實例**，配方經共用 RecipeStore 同步；
  `LoadSlot` 設 `RecipeStore.SelectedIp = slot.recipe_partition`（約束①：儲存鍵 IP0…、UI 顯 CCD 名）。
- 獨立導覽入口已移除：進入方式 = 相機工作台 Step 4（§6.7）。

### 6.7 相機工作台（CameraWorkbenchView，2026-07-31 操作員五步驟動線）

> 設計稿：`docs/design/operator_ui/`；selftest：`--selftest workbench`（21/21 PASS）。
> 檔案：`Views/CameraWorkbenchView.axaml` + `ViewModels/CameraWorkbenchViewModel.cs`（**重用既有引擎，非重做**）。

**左欄（槽位卡；Step 4 時自動收合讓出全寬）**：`Engine.ComputeUnits`（宣告×偵測 join）攤平成 `WorkbenchSlot`
清單——每張卡 = CCD 名 + 四態 chip（已綁定綠 / MAC 不符紅 / 離線灰 / 未綁琥珀）+ 五步驟進度點；
下方「未指派的相機」（bound=false，綁定唯一入口）+「↻ 重新列舉」。

**右側五步驟（= 生產順序）：**

| 步驟 | 內容 | 引擎（重用） |
|------|------|------|
| ① 綁定 | 未綁：候選清單→[綁定]；已綁：檢視＋解除（二段確認）；MAC 不符：「去檢查接線 / 確認換機」二出口 | `SystemSettingsViewModel.BindSelected/UnbindSelected`（SET_CAM_MAP **完整表**、拒搶槽、cam_id=拓樸槽索引）|
| ② 取像 | 曝光/增益滑桿 + [抓一幀]（TUNE_MEAN 回 mean_gray）+ 判讀（<3 暗場防呆 / <30 偏暗 / 正常）+ [套用並再抓] | `GrabClient.TuneMeanAsync / SetCamParamsAsync` |
| ③ 對位 | M_AlignRoi 數值教學（AlignEnable / ReferX/Y / 搜尋窗 / 範本檔）→ [儲存 Mark] 寫進該槽分區配方 | `RecipeStore.Save()`（SelectedIp=該槽 recipe_partition）|
| ④ 調參 | 內嵌 `SingleCcdSetupView`（完整檢測鏈，§6.6）+ [此相機調參完成] 進度標記 | `SingleCcdSetupViewModel` |
| ⑤ 套用 | 勾選目標槽 → 檢測參數（34 欄）/ 曝光增益 / 對位 Mark（預設**不**複製，各教各的）批次複製 | `RecipeService.CopyParamsToIps`（磁碟對磁碟、保留目標 Mark）|

⚠️ 已知限制：② 縮圖預覽待 grab `GET_FRAME_PREVIEW`（新命令未做）；③ [試對位] 停用（CHECK_ALIGN 待實拍流程），
且 Mark 的 golden 樣板**目前不會**隨 LOAD_RECIPE 自動送 IP（`IpClient.LoadRecipeAsync` 的 `goldenPngBase64`
尚無呼叫端）；⑤ 複製來源讀**磁碟上已儲存**的配方——套用前請先按「儲存配方」。

---

## 7. TCP 協議（上位機 / IP / Grab）

### 7.1 上位機 → Control（port 8787）

格式：`CF_{CMD}|p1|p2|…\r\n`，回應 9 參數 `OK|p1|…|p8|{p9=errMsg}\r\n`

| 命令 | 格式 | 回應（現況 2026-08-02）|
|------|------|------|
| CF_LOAD_RECIPE | `CF_LOAD_RECIPE|{recipe}|{panelId}|{datetime}|||||||{detectMode}` | `OK`（IP 載入成功；另做 Grab LOAD_RECIPE+GRAB_ARM **預熱**，預熱失敗僅 Log.Warn）/ `ERR|…|{msg}` |
| CF_GRAB_START  | `CF_GRAB_START|{timeoutMs}` | ✅ **已接真 Grab（2026-07-21）**：GRAB_START（timeout_ms + frames_per_panel）→ OK / ERR（Grab 未連或失敗，誠實失敗）|
| CF_STOP（#25） | `CF_STOP` | ✅ **已接真 Grab（2026-07-21）**：GRAB_STOP（teardown 解除 ARM）→ OK / ERR |
| CF_CHECK_ALIGN | `CF_CHECK_ALIGN` | **ERR**（對位未接線，誠實失敗；不再回假 `OK|0|0`）|
| CF_SET_ALIGN   | `CF_SET_ALIGN|{result}|{shiftX}|{shiftY}` | **ERR**（對位未接線，誠實失敗）|
| CF_GET_RESULT  | `CF_GET_RESULT` | `OK|{資料夾,逗號}|{缺陷數,逗號}|…`（由 IP `LIST_DEFECT_FOLDERS` 組，非 JSON）。⚠️ 已知限制：IP 離線/例外時現回 `OK||0` 空結果（待改誠實 ERR）|
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

`GrabClient` 現有 **11 命令**：

| 命令 | 用途 | 重要 params |
|------|------|------------|
| CHECK_HEALTH | 心跳 | 無 |
| LIST_CAMERAS | 列舉相機（含 `bound`/`ccd_id` 綁定憑據；舊版 grab 缺欄 → 退回全未綁不崩）| 無 |
| SET_CAM_MAP | Gap #21 綁定：送**完整**映射表，Grab 寫 cam_map.json 並重載 | `entries[{mac,cam_id,ccd_id}]` |
| LOAD_RECIPE | 更新 Grab 端 panel_id（FrameHeader.panelId hash 來源）| `recipe`, `panel_id` |
| GRAB_ARM | 預熱（開相機陣列+套曝光增益+RDMA connect），冪等 | 無 |
| GRAB_START | 觸發本體（已 ARM 時僅 ms 級 start_all）| `timeout_ms`, `frames_per_panel`（0=連續 legacy）|
| GRAB_STOP | 停取像＋斷 RDMA（完全 teardown，解除 ARM）| 無 |
| SET_CAM_PARAMS | 設曝光/增益（回 read-back actual，Gap #2）| `cam_id`, `exposure_us`, `gain_raw` |
| GET_CAM_PARAMS | 讀曝光/增益（相機未開時回 cam_config.json 值）| `cam_id` |
| TUNE_MEAN | 免 RDMA 抓 1 幀回 mean gray（調參效果驗證/工作台 Step 2）| `cam_id`, `exposure_us`, `gain_raw` |
| GET_CAM_NODES | 讀 GigE 機器層參數（PixelFormat/Auto/Trigger/ROI/封包）| 無 |

---

## 8. 配方管理

### 8.1 目錄結構

```
{RecipeDir}/                           預設 ~/cf-aoi/recipes（執行期目錄，非 repo recipes/）
└── {RecipeName}/
    ├── RecipeSetting.xml              per-recipe 存圖設定（MaxSaveDefectCount 等）
    └── {IpName}/                      per-CCD 分區：每台 CCD 一份（IP0、IP1…；現行儲存鍵=IpName）
        └── RecipeInfo.xml             AlignRoi + DetectRoiList[] 配方主檔
```
> 約束①：UI/docs 用 **CCD** 名，但**儲存鍵仍是 `IP0`(IpName)**；`array_topology.json` 的 `recipe_partition` 即此。`RecipeStore.SelectedIp` 決定載哪台 CCD 的 `{recipe}/{IpName}/RecipeInfo.xml`（切 IP 重載）。`RecipeIps`(appsettings) 宣告可編 IP 清單。

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
      <AlgorithmCompare>DIV</AlgorithmCompare>  <!-- ⚠ stale 欄位（勿依賴）：演算法域權威=下行 -->
      <M_AlgorithmWayCompare>Awc_8_Way_Star_Div</M_AlgorithmWayCompare>
                                                <!-- 三域守門權威：SUB / DIV-voting / DIV，見 docs/CLAUDE.md §5 -->
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
| TCP 上位機 port 8787 CF_ 命令 | ✅ 已接線 L2/L3 | UpstreamServer + UpstreamWiring.Bind + Start（2026-06-19）|
| CF_LOAD_RECIPE | ✅ 接 IP | OnLoadRecipe → IpClient.LoadRecipeAsync |
| CF_GRAB_START | ✅ **已接真 Grab（2026-07-21）** | OnGrabStart → GrabStartAsync（timeout/fpp 傳遞；Grab 離線誠實 ERR）|
| CF_CHECK_ALIGN | ✅ 誠實失敗 | offline 未對位 → ERR（不再回假 OK\|0\|0）|
| CF_SET_ALIGN | ✅ 誠實失敗 | offline 不套用 → ERR |
| CF_GET_RESULT | ✅ 接 IP | OnGetResult → ListDefectFoldersAsync 組 path,count（非 JSON）|
| CF_STOP（#25）| ✅ **已接真 Grab（2026-07-21）** | OnStop → GrabStopAsync（teardown 解除 ARM）|
| 多相機並行（CamProc[]） | ❌ Step 2+ | 新架構由 Grab 機多相機，Control 單 TCP |
| FrameGrabber 7 種後端 | ❌ 移除 | 改為 GrabClient TCP 控制 Grab 機 |
| CudaCore GPU 缺陷檢測 | ❌ 移除 | 改為 IpClient TCP 控制 IP 機 |
| MIL Pattern Match 對位 | ❌ 移除 | 新架構無對位（或由 IP 機處理） |
| AiProc ONNX AI 分類 | ⚠️ 停用 | AI 架構保留在 IP 端但不推論 |
| 離線演算法驗證工具 | ✅ 完整實作 | Step1View（過 TCP 送 IP 分析） |
| 配方 IP Param Editor | ✅ 完整實作 | 34 欄位批次編輯（表單內嵌於工作台 Step 4 / SingleCcdSetupView 進階摺疊）|
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
| SelfTest 無頭驗證 | --selftest 18 子命令（§15；含 grabtrigger/remoteimg/recipemgmt/recipesaving/workbench）|
| 跨平台 | Avalonia .NET 8（Linux/Windows/macOS 共一份程式）|
| 相機工作台五步驟（2026-07-31）| 綁定→取像→對位→調參→套用 單頁動線（§6.7）|
| MAC↔CCD 綁定（Gap #21）| SET_CAM_MAP 完整表 + join 四態 + MAC 正規化（Control L2 / Grab L3 2026-07-30）|
| 從 IP 載入影像 | 遠端瀏覽 + 縮小預覽 + REVIEW_LOCAL_IMAGE 全解析度檢測（bit-exact，端到端 L3 2026-06-22）|

### 12.1 考古補充（2026-06-17）：§12 未涵蓋 / 細項缺口（legacy 有、Control 缺）

> Reference 路徑正名：`legacy_win` = **`Reference/PrjCfAoi/`**。以下為 2026-06-17 逐檔考古（含 MainProc/CamProc/Common/Configuration）補出、§12 未列或敘述不足者，file:line 為實際讀到。

| legacy 功能 | legacy 位置(file:line) | Control 現狀 | 評估 |
|------------|----------------------|-------------|------|
| **Rule 改判**（ImageRuleEnable / MeanLowThreshold / HdivWThreshold / NgSizeThreshold）| `CamProc.cs:816-847` | ✅ **已補（#16）**：`RecipeSavingModel.ImageRule*` → LOAD_RECIPE `recipe_saving` → IP defect_rules | `--selftest recipesaving` 驗 JSON 契約 + e2e（預設停用不破 bit-exact）|
| **多通道 log**（7 通道：Sys/Err/NetRec/NetSend/Prc/Msk/GetImg）| `LogMgr.vb:11-17` | LogService 只 3 通道（Sys/Err/Warn）| NetRec/NetSend/Prc/Msk/GetImg 5 通道缺；網路收發/處理診斷能力降低（**新缺口，次要**）|
| **配方 SaveAs / Align / 多 IP 同步** | `frmAoiSettingEditor.cs:358-690` | **部分補**：SaveToAllIps(#33)、CopyParamsToMany(#7)、CopyParamsToIps(工作台 Step5)、per-IP AlignRoi(#34) | SaveAs（另存新配方名）仍缺 |
| **AI 模型管理 UI**（掃 .onnx / 刪除 / 配方關聯）| `frmAiModelManager.cs:36-72` | 只有 AiRootPath 設定欄位 | AI 停用 → 管理 UI 未遷移（L0）|
| **frmVariance 模糊度統計**（呼叫 Python blurring）| `frmVariance.cs:73-300` | 無 | §12 已標「未實作」，確認缺 |
| **MaskGen 掩碼生成** | `LibAoiSetting/frmMaskGen.cs` | 無 | 掩碼 ROI 繪製無對應（**新缺口**）|
| **離線工具滑鼠繪製 ROI**（增刪/拖矩形）| `frmAlgorithmTestTools.cs:474-643` | ✅ **已補**：`RoiImageView` 框 ROI/八把手拖曳/數值微調 | Step1/單 CCD/工作台共用 |
| Interest ROI（IOI）存圖 | `CamProc.cs:1547-1614`（DetectIoiList）| 無 | IOI 興趣區存圖無對應（IP 端 `<IoiInfoList/>` 也空）|
| AutoFlash 待機閃頻 / 登入權限（frmLogin）/ Basler 串口控制 | `AutoFlash.cs` / `frmAoiSettingEditor.cs:1487-1577` / `frmBaslerCom.cs` | 無 | 確認缺（多為產線/硬體周邊，多數可不補）|
| CF_STOP（中斷取像）/ BypassAlignment review | `MainProc.cs:999-1015` / `CamProc.cs:1688-1812` | **CF_STOP 已接（2026-07-21 → Grab GRAB_STOP）**；BypassAlignment 旗標存在但停用 | #26 review_offset 機制仍無對應 |

> ⚠️ **legacy 三處根目錄常數不一致**（考古發現）：`Common.cs`=`D:\Cf_Aoi`、`Configuration.cs`=`D:\Transfer_Aoi`、`BootConfig.vb`=`D:\uLedInspAOI`——疑為跨產品線（CF/Transfer/uLed AOI）共用碼庫殘留，非單一真相。新架構用 appsettings.json `Paths`（`~` 展開），無此問題。
>
> ⚠️ **μm 座標契約 follow-up（與 Gap #5 同條）**：legacy runtime 缺陷一律 pixel（`CamProc.cs` 未乘 OptRes），CF_GET_RESULT 只回 ResultInfo.xml 路徑+缺陷數。IP 端 Gap #5 新增 `GlobalPosX_um` 為**片面提議**，上位機是否讀 μm 待接真機確認——與 **UpstreamServer 接真實上位機**（§13）屬同一條 follow-up。

---

## 13. 待接線 / 待驗證清單

| 項目 | 現況 | 備註 |
|------|------|---------|
| **上位機 CF_ 接線**（Start + OnLoadRecipe/OnGetResult/OnConnectedChanged） | ✅ **已接線 L2/L3**（2026-06-19）| UpstreamWiring.Bind；GET_RESULT 端到端回真實 path+count |
| **取像/對位 CF_**（GRAB/CHECK/SET_ALIGN） | ✅ **誠實失敗 ERR** | offline 不綁（決策 A）；真對位/取像 = #1/Step4 |
| **真上位機協議認帳 + μm 契約(#5)** | ⚠️ **L4（做不了）** | 模擬器只證格式/交握；真實 Master 是否如預期讀欄位/μm 待接真機 |
| **RoiImageView 互動** | L1 待 Mac 目視 | 縮放/平移/框 ROI 把手/數值/缺陷導航/量 Pitch 與 Step1 一致（抽出後行為不變）|
| **單 CCD 工作台版面** | ✅ 寬幅改版 + Mac 目視（2026-07-31）| 上全寬影像橫幅 + 下三欄（§6.6）；內嵌於工作台 Step 4 |
| **SystemSettings 機台層 tab** | 已移除（2026-07-31 導覽收斂）| 宣告陣列/相機參數整併進相機工作台（§6.7）；系統設定僅剩連線 tab |
| **MAC↔CCD 綁定(#21)** | ✅ 已落地（Control L2 / Grab L3，2026-07-30）| SET_CAM_MAP 完整表；工作台 Step 1 為綁定 UI |
| **A1 實拍底圖（工作台 Step 2 縮圖）** | 待 grab `GET_FRAME_PREVIEW`（未做）| 單 CCD 頁底圖現載入 TIFF / 從 IP 載入 |
| **GrabClient 取像命令** | ✅ 已做（觸發鏈 2026-07-21）| GRAB_ARM / GRAB_START / GRAB_STOP（見 §7.3）；缺 GET_FRAME_PREVIEW |
| **對位鏈（CHECK_ALIGN/SET_ALIGN + golden 送 IP）** | 未接 | `goldenPngBase64` 尚無呼叫端；待實拍流程（6 相機日）|
| **RecipeSetting / DebugAlgorithm → IP** | 部分接 | LOAD_RECIPE 帶 recipe_saving / SEND_IMAGE debug 已接，全域 ShareSetting 未自動帶 |

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
    "IpOffline": { "Host": "addis-b850m-ds3h.tailffdb68.ts.net", "Port": 8200, "Mode": "offline-tcp" },  // 可填 Tailscale 主機名
    "IpOnline":  { "Host": "192.168.10.11", "Port": 8200, "Mode": "online" },
    "GrabA":     { "Host": "100.92.102.95", "Port": 8100 }
  },
  "ActiveIpNode": "IpOffline",   // 目前使用的 IP 節點
  "RecipeIps": [ "IP0" ],        // 配方可編輯的 IP/CCD 清單（多台 IP 機加 "IP1"…）
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
camera                假 LIST_CAMERAS（多台 bound/unbound）→ 分群 + KPI（離線維持 0，不假造）
topology              機台拓樸 fixture：載入 + 依 compute_unit 分群 + 全槽未綁不標線上 + 不假 merge 列舉相機；塊2 處理 N/負載公式/連線規則
singleccd             單 CCD 工作台：組合既有 VM + LoadSlot 設 SelectedIp + header 顯 CCD 名 + EditZone=選中 ROI
upstream              上位機 CF_ in-process 交握：READY/LOAD_RECIPE接IP/GET_RESULT path+count/CHECK·SET_ALIGN 誠實失敗/燈轉綠（L2）
grabtrigger           觸發鏈：CF_LOAD_RECIPE→ARM 預熱 + CF_GRAB_START（timeout/fpp 傳遞）+ CF_STOP + Grab 離線誠實 ERR（L2）
remoteimg             從 IP 載入：遠端瀏覽/預覽=全解析度寬高/Test 路由 REVIEW_LOCAL_IMAGE（L2，假 IP server）
recipemgmt            #33 配方管理（Delete/SaveAll/開資料夾）+ #7 跨配方批次複製（暫存目錄隔離）
recipesaving          #16/#32 recipe_saving JSON 契約逐鍵對齊 IP（選填 <host> 連真 IP e2e 驗 bypass）
workbench             相機工作台五步驟：join 四態/完整表綁定/暗場判讀/Mark 落檔/複製不覆蓋 Mark/進度點（21 case）
heartbeat auto        ★8 心跳門檻自動化：單次逾時不翻線/連續 2 次才斷/恢復自動重連（7 case）
```
> 一鍵：`scripts/run.sh selftest <子命令>`。端到端上位機 L3：先開 Control，再跑 `scripts/upstream_simulator.py`。

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
| 上位機接線（CF_ 回呼）| [Controllers/UpstreamWiring.cs](../control/src/Controllers/UpstreamWiring.cs) |
| 影像/ROI 共用控制項 | [Controls/RoiImageView.axaml.cs](../control/src/Controls/RoiImageView.axaml.cs) |
| 單 CCD 工作台 | [ViewModels/SingleCcdSetupViewModel.cs](../control/src/ViewModels/SingleCcdSetupViewModel.cs) ／ [Views/SingleCcdSetupView.axaml](../control/src/Views/SingleCcdSetupView.axaml) |
| 相機工作台（五步驟）| [ViewModels/CameraWorkbenchViewModel.cs](../control/src/ViewModels/CameraWorkbenchViewModel.cs) ／ [Views/CameraWorkbenchView.axaml](../control/src/Views/CameraWorkbenchView.axaml) |
| 相機列舉 / MAC 正規化 | [Models/CameraInfoModel.cs](../control/src/Models/CameraInfoModel.cs) ／ [Models/MacUtil.cs](../control/src/Models/MacUtil.cs) |
| 遠端影像瀏覽（從 IP 載入）| [ViewModels/RemoteImageBrowserViewModel.cs](../control/src/ViewModels/RemoteImageBrowserViewModel.cs) ／ [Views/RemoteImagePickerHelper.cs](../control/src/Views/RemoteImagePickerHelper.cs) |
| 機台拓樸模型 | [Models/ArrayTopologyModel.cs](../control/src/Models/ArrayTopologyModel.cs) ／ [config/array_topology.example.json](../control/src/config/array_topology.example.json) |
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

*本文件由原始碼逐檔靜態分析整理，對照 Reference/PrjCfAoi/程式完整說明.md 交叉驗證。2026-06-17 補 §12.1 legacy 細項缺口（逐檔考古 MainProc/CamProc/Common/Configuration，file:line 為實際讀到）。**2026-08-02 全面對齊現行程式**（dashboard/工作台 §6.7/觸發鏈/34 列/三域守門）。格式對齊 [ip_程式完整說明.md](ip_程式完整說明.md) / [grab_程式完整說明.md](grab_程式完整說明.md)。*
