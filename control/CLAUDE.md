# Control 程式 — CLAUDE.md（Avalonia 跨平台版）

> 先讀 `../CLAUDE.md`，再讀本文件。
> **UI 框架：Avalonia（跨平台，可在 Linux/Windows/macOS 執行）**
> **初始化：執行 `../scripts/setup_control.sh`**

---

## 1. 技術棧

| 項目 | 選擇 | 說明 |
|------|------|------|
| 語言 | C# 12 / .NET 8 | 跨平台 |
| UI | **Avalonia** | Linux / Windows / macOS |
| MVVM | **CommunityToolkit.Mvvm** | `[ObservableProperty]` `[RelayCommand]` |
| UI 主題 | Avalonia.Themes.Fluent | Windows 11 風格 |
| Zone 參數編輯 | **手寫資料驅動表單**（ZoneParamEditor，**34 列**；SUB/融合/LSC 參數加入後 27→34）| 對齊 legacy `DetectRoi`（32 欄）＋融合新欄；**非** AvaloniaPropertyGrid、**非** 54 參數 |
| 影像顯示 | **SixLabors.ImageSharp + Avalonia Bitmap** | 缺陷 patch / 大圖；**不用 OpenCV**（跨平台） |
| 設定 | `appsettings.json` | Microsoft.Extensions.Configuration |
| DI | Microsoft.Extensions.Hosting | 服務注入 |

---

## 2. 程式碼結構

```
control/
├── CLAUDE.md
├── scripts/
│   └── setup_control.sh    ← 執行此 script 初始化
└── src/
    ├── CfAoiControl.csproj  ← Avalonia 11.2 專案（AvaloniaUseCompiledBindingsByDefault=true）
    ├── Program.cs            ← 進入點（--selftest 走無頭 SelfTest，不啟動 GUI）
    ├── App.axaml(.cs)        ← Avalonia Application（AppServices.Build + MainWindow）
    ├── ViewLocator.cs / ViewModels/ViewModelBase.cs ← MVVM 樣板
    ├── appsettings.json      ← 連線/路徑/Grab/RecipeIps/ShareSetting 設定
    ├── config/array_topology.example.json ← 37 槽宣告模板（本機 array_topology.json 不版控）
    │
    ├── Views/                ← 單一主視窗 + 側欄 5 頁（2026-07-31 導覽收斂；非多視窗）
    │   ├── MainWindow.axaml(.cs)         ← dashboard（狀態磚/CF 命令卡/log 三分頁）+ 側欄導覽 + A/B 主題切換
    │   ├── CameraWorkbenchView.axaml(.cs)← 相機工作台：左欄槽位卡 + 五步驟（綁定/取像/對位/調參/套用）
    │   ├── Step1View.axaml(.cs)          ← 檢測複判（原離線分析；寬幅版面：影像全寬橫幅）
    │   ├── DefectSortView.axaml(.cs)     ← 缺陷遠端歸檔 + 小圖人工分類（frmSortDefect）
    │   ├── SystemSettingsView.axaml(.cs) ← 系統設定（**僅剩連線設定 tab**；宣告陣列/相機參數已整併進工作台）
    │   ├── SingleCcdSetupView.axaml(.cs) ← 單 CCD 檢測工作台（內嵌於工作台 Step 4；寬幅：上全寬影像+下三欄）
    │   ├── ZoneParamEditorView.axaml(.cs)← 34 列表單獨立頁（**現無導覽入口**；表單邏輯由 SingleCcdSetupView 進階摺疊直接綁 ParamRows）
    │   ├── RemoteImageBrowserView.axaml(.cs) ← 「從 IP 載入」遠端影像瀏覽對話框
    │   └── RemoteImagePickerHelper.cs    ← 開啟遠端瀏覽對話框（Step1/單 CCD 共用）
    │
    ├── ViewModels/           ← ViewModel 層（MVVM）
    │   ├── MainWindowViewModel.cs        ← 導覽/狀態磚/log 路由/上位機接線與心跳啟動
    │   ├── CameraWorkbenchViewModel.cs   ← 工作台五步驟（重用 SystemSettings 綁定引擎 + SingleCcdSetup）
    │   ├── Step1ViewModel.cs
    │   ├── ZoneParamEditorViewModel.cs   ← 34 列 ParamRow（反射代理選中 ROI）+ 對位 Mark
    │   ├── DefectSortViewModel.cs
    │   ├── SystemSettingsViewModel.cs    ← 連線設定 + **綁定/相機引擎**（topology join 四態、SET_CAM_MAP、曝光增益；UI 大多由工作台呈現）
    │   ├── SingleCcdSetupViewModel.cs    ← 組合 Step1ViewModel + ZoneParamEditorViewModel 兩實例
    │   └── RemoteImageBrowserViewModel.cs
    │
    ├── Controls/             ← 自繪控制項
    │   ├── DefectOverlayControl.cs    ← 大圖缺陷圓圈 overlay（隨縮放，線寬固定）
    │   └── RoiImageView.axaml(.cs)    ← 影像/ROI 共用控制項（縮放/平移/框 ROI 把手/量測/缺陷導航）
    │
    ├── Converters/AppConverters.cs   ← 12 個 IValueConverter（燈色/log 色/槽位四態色/步驟點…）
    │
    ├── Controllers/          ← 業務邏輯（平台無關）
    │   ├── UpstreamServer.cs          ← TCP ← 上位機（CF_ / 8787 / 9 參數，已 Start + 已接線）
    │   ├── UpstreamWiring.cs          ← CF_ 回呼接既有流程（LoadRecipe→IP+Grab 預熱、GRAB_START/STOP→Grab、GetResult→IP）
    │   ├── IpClient.cs                ← TCP → IP（含 UTF-8 整行解碼，見不變式）
    │   ├── GrabClient.cs              ← TCP → Grab（LIST_CAMERAS/SET_CAM_MAP/GRAB_ARM·START·STOP/曝光增益/TUNE_MEAN/GET_CAM_NODES）
    │   ├── IHeartbeatClient.cs        ← 心跳介面（IpClient/GrabClient 實作）
    │   └── ConnectionManager.cs       ← 定期 CHECK_HEALTH（5s 逾時×連續 2 次才斷）+ 自動重連 + SetUpstreamConnected
    │
    ├── Models/               ← 資料結構（C# 慣用名 + [XmlElement]/[JsonPropertyName] 映射 legacy）
    │   ├── RecipeModel.cs             ← Recipe → AlignRoi + DetectRoiList + DetectIoiList
    │   ├── ZoneSettingModel.cs        ← 對齊 legacy DetectRoi（32 欄）+ 融合/LSC 新欄（ObservableObject）
    │   ├── DefectResultModel.cs       ← JudgeResult → RoiInfoList → DefectInfoList（JSON+XML 雙解析）
    │   ├── ArrayTopologyModel.cs      ← 機台層拓樸宣告（37 槽 / 運算單元；config/array_topology.json）
    │   ├── CameraInfoModel.cs         ← LIST_CAMERAS 列舉結果（bound/ccd_id = 綁定憑據）
    │   ├── MacUtil.cs                 ← MAC 正規化（行為對齊 grab CamManager::normalize_mac）
    │   ├── CamNodesModel.cs / CamParamsModel.cs ← GigE 機器層參數 / 曝光增益 read-back
    │   ├── RecipeSavingModel.cs       ← per-recipe RecipeSetting.xml（#16 Rule/#32 邊界 → recipe_saving JSON）
    │   ├── ShareSettingModel.cs       ← 全域旗標（appsettings ShareSetting）
    │   ├── SystemConfigModel.cs
    │   └── FrameHolder.cs             ← ⚠ 目前無任何引用（死碼；CHECK_ALIGN 裁搜尋 ROI 備用）
    │
    └── Services/
        ├── AppServices.cs             ← 手動 DI 容器（Build/DesignTime）
        ├── ConfigLoader.cs            ← appsettings.json 讀寫（SaveShareSetting 只改該節點）
        ├── RecipeService.cs           ← 配方讀寫 + XML 序列化 + ~ 展開 + CopyParamsToIps（工作台 Step5）
        ├── RecipeStore.cs             ← 配方單一資料來源（single source of truth）
        ├── OfflineReviewService.cs    ← Step1 送 IP（network-clean；含遠端 REVIEW_LOCAL_IMAGE）
        ├── PitchEstimator.cs          ← 純 managed FFT 估 Pitch
        ├── LogService.cs
        └── SelfTest.cs                ← --selftest 無頭驗證（18 個子命令，見 docs/control_程式完整說明.md §15）
```

---

## 3. MVVM 基本模式（CommunityToolkit.Mvvm）

所有 ViewModel 繼承 `ObservableObject`，用 attribute 自動生成屬性和命令：

```csharp
// Step1ViewModel.cs
public partial class Step1ViewModel : ObservableObject
{
    // 自動生成 public float Bth { get; set; } + PropertyChanged
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasOverrideParams))]
    private float bth = 1.30f;

    [ObservableProperty] private float dth = 0.75f;
    [ObservableProperty] private int   pitchX = 26;
    [ObservableProperty] private int   pitchY = 20;
    [ObservableProperty] private string selectedImagePath = "";
    [ObservableProperty] private string selectedRecipe = "DEFAULT";
    [ObservableProperty] private bool   isAnalyzing = false;
    [ObservableProperty] private string statusMessage = "就緒";
    [ObservableProperty] private DefectResultModel? lastResult;
    [ObservableProperty] private bool   showAutoGenWarning = false;

    // 自動生成 public ICommand RunAnalysisCommand { get; }
    [RelayCommand(CanExecute = nameof(CanRunAnalysis))]
    private async Task RunAnalysisAsync()
    {
        IsAnalyzing = true;
        StatusMessage = "傳送影像中...";
        try {
            // 閾值欄位為 BrightThreshold/DarkThreshold（非 ThB/ThD）。實作上快速調參直接綁
            // RecipeStore.PrimaryZone（單一資料來源），不再各自 new override；此處僅示意映射。
            var overrideZone = new ZoneSettingModel {
                BrightThreshold = Bth, DarkThreshold = Dth, PitchX = PitchX, PitchY = PitchY
            };
            var recipeResult = await _recipeService.EnsureRecipeExistsAsync(SelectedRecipe);
            ShowAutoGenWarning = recipeResult.IsAutoGenerated;

            LastResult = await _offlineReviewService.AnalyzeImageAsync(
                SelectedImagePath, SelectedRecipe, camId: 0, overrideZone);
            StatusMessage = $"完成：{LastResult.Summary.TotalDefects} 個缺陷，{LastResult.ProcessingMs:F1}ms";
        }
        catch (Exception ex) { StatusMessage = $"❌ {ex.Message}"; }
        finally { IsAnalyzing = false; }
    }
    private bool CanRunAnalysis() => !IsAnalyzing && !string.IsNullOrEmpty(SelectedImagePath);
}
```

---

## 4. Avalonia XAML 模式

> ⚠️ 本節原有 MainWindow / Step1View 的逐行 XAML 範例已移除——**示意用途，實際版面一律以對應 `.axaml` 為準**
> （範例曾與現況嚴重脫節：主視窗已改為 dashboard + 側欄 5 頁導覽，非舊步驟按鈕版）。

現行 XAML 慣例（讀 `Views/*.axaml` 時的背景知識）：

- **單一主視窗**：`MainWindow.axaml` 內以 `IsVisible="{Binding IsXxx}"` 切換 5 個內容 Panel
  （主控台 dashboard / 相機工作台 / 檢測複判 Step1View / 缺陷分類 DefectSort / 系統設定），各 View 以
  `<views:XxxView DataContext="{Binding Xxx}"/>` 內嵌，**不另開視窗**（例外：RemoteImageBrowserView 為 modal 對話框）。
- **主題 token**：顏色/圓角/字體全走 `{DynamicResource ...}`（`Styles/ThemeConsole.axaml` / `ThemeLab.axaml`），
  頂列 A/B 鈕即時換膚（`MainWindow.axaml.cs` 抽換 MergedDictionaries[0]）。
- **Compiled bindings 預設開**（csproj `AvaloniaUseCompiledBindingsByDefault=true` + 各檔 `x:DataType`）；
  少數複雜路徑檔案（RoiImageView / SingleCcdSetupView）標 `x:CompileBindings="False"` 用反射綁定。
- 版面用等比 `Grid`/`DockPanel`（非 Canvas 絕對座標，因 Mac Retina/縮放）。

---

## 5. Zone 參數編輯（手寫資料驅動表單，對齊 DetectRoi 32 欄）

> ⚠️ **無 AvaloniaPropertyGrid、非 54 參數**。表單源自 legacy `frmIpParamEditor`：
> 左 ROI 位移(+/-)、中 **34 列參數**（27 列 legacy ＋ SUB/融合/LSC 新欄 7 列；每列 CheckBox + Label +
> 輸入(TextBox/ComboBox) + Update）、右多 ROI 勾選，底部 Clear(粉)/Select(綠)/Update Chk(金) 批次鈕。
> 資料來源綁 `RecipeStore`（單一資料來源）。**獨立頁現無導覽入口**——同一份 `ParamRows` 由工作台 Step 4
> 內嵌的 `SingleCcdSetupView`「進階參數」摺疊面板直接呈現。

```
ParamRow（資料驅動）：以反射代理選中 ROI 的欄位，訂閱 PropertyChanged 即時同步回 ZoneSettingModel。
IP 端未消費的參數（AlgorithmWay/Blob* 等）標「IP待接」。閾值列為 BrightThreshold/DarkThreshold。
```

---

## 6. 從 Reference 遷移的對照

| Reference 來源 | control/src/ 目標 | 方式 |
|---------------|-----------------|------|
| `PrjCfAoi/Class/MainProc.cs`（TCP server 部分）| `Controllers/UpstreamServer.cs` | 🔧 移除 MIL/Camera |
| `PrjCfAoi/Class/MainProc.cs`（流程協調）| （**未建此檔**：職責由 `Controllers/UpstreamWiring.cs` + 各 ViewModel 分擔）| 🔧 改為 TCP 呼叫 |
| `ClibCf/Recipe.cs` | `Models/RecipeModel.cs` | 🔧 保留 XML 格式 |
| `ClibCf/JudgeResult.cs` | `Models/DefectResultModel.cs` | 🔧 加 JSON 反序列化 |
| `PrjAoiSettingEditor/frmIpParamEditor.cs` | `Views/ZoneParamEditorView.axaml` | 🔧 WinForms → Avalonia 手寫 34 列資料驅動表單（非 PropertyGrid）|
| `PrjAoiSettingEditor/frmSortDefect.cs` | `Views/DefectSortView.axaml` | 🔧 改遠端命令 + 小圖人工分類 |
| `PrjCfAoi/frmCfAoi.cs` | `Views/MainWindow.axaml` | 🔧 重新設計，移除 MIL Display |
| MilNetHelper, CamProc, AiProc | — | ❌ 完全移除 |

---

## 7. 連線設定（appsettings.json）

```json
{
  "UpstreamServer": { "ListenPort": 8787, "Optional": true },
  "Nodes": {
    "IpOffline": { "Host": "127.0.0.1", "Port": 8200, "Mode": "offline-tcp" },
    "IpOnline":  { "Host": "192.168.10.11", "Port": 8200, "Mode": "online" },
    "GrabA":     { "Host": "192.168.10.21", "Port": 8100 }
  },
  "ActiveIpNode": "IpOffline",
  "Grab": { "FramesPerPanel": 0 },          // CF_GRAB_START 帶給 Grab 的每片張數；0=連續（生產應設 N）
  "RecipeIps": [ "IP0" ],                   // 配方可編輯的 IP/CCD 分區清單（多分區加 "IP1"…；預設留空防 config 疊加重複）
  "Paths": {
    "RecipeDir": "~/cf-aoi/recipes",
    "OutputDir": "~/cf-aoi/output",
    "ImageDir":  "~/cf-aoi/test_images",
    "RemoteImageDir": ""                    // 「從 IP 載入」遠端瀏覽起始目錄（IP 機路徑；空=IP 工作目錄）
  },
  "ShareSetting": {                         // 全域旗標（=legacy ShareSetting.xml；SaveShareSetting 只覆寫此節點）
    "SaveSourceImage": false, "DebugAlgorithm": false, "AiRootPath": "",
    "TuningRecipe": false, "SaveFullImage": false, "BypassAlignment": false
  }
}
```

---

## 8. 建置與執行

```bash
# Linux / macOS
cd ~/cf-aoi/control/src
dotnet run                    # 開發模式
dotnet build -c Release       # 建置

# Windows（同一份程式碼）
dotnet publish -r win-x64 --self-contained -o publish/

# 打包成單一執行檔（Linux）
dotnet publish -r linux-x64 --self-contained \
    -p:PublishSingleFile=true -o publish/
```

---

## 9. 不變式

1. **上位機協議 = `CF_` 前綴 / port 8787 / `|` 分隔 / 9 參數回應**。舊文件的 `LoadRecipe|RECIPE|PANEL`
   （port 8000 簡化介面）**已作廢**。
   ✅ **狀態：已接線啟動**——2026-06-19 `UpstreamWiring.Bind` + `Start()`（`MainWindowViewModel` ctor）；
   2026-07-21 觸發鏈 CF_LOAD_RECIPE→IP+Grab 預熱、CF_GRAB_START/CF_STOP→真 Grab；2026-07-31 生產迴圈端到端 **L3**。
   驗證：`--selftest upstream`/`grabtrigger` = L2、`scripts/upstream_simulator.py` 端到端 = L3。
   ⚠️ **真上位機協議認帳（欄位/序列/μm 是否如實機預期）= L4 仍做不了**；CHECK/SET_ALIGN 不綁（誠實 ERR）、#26 BypassAlignment 未做。
2. **RecipeInfo.xml 格式凍結**，不可改 Schema（= legacy `Recipe`，閾值欄位 `BrightThreshold`/`DarkThreshold`，
   非 ThB/ThD）。演算法域走**三域守門**（`<M_AlgorithmWayCompare>` 權威：SUB / DIV-voting 融合 / DIV；
   `<AlgorithmCompare>` 為 stale 欄位，無法判定或域值錯配一律拒載）——完整規則見 `docs/CLAUDE.md` §5。
3. 連線失敗不阻塞啟動（UpstreamServer, IpClient, GrabClient 都靜默重試）。
4. 自動生成的配方顯示黃色 ⚠ 警告，直到使用者確認 PitchX/PitchY。
5. `appsettings.json` 無任何 hardcode 位址。
6. ViewModel 不直接存取 TCP，透過 Controllers 層呼叫。
7. **Model 線上名稱（XML element / JSON key）須與 IP 實際輸出 byte 對齊**：
   - `DefectResultModel`/`RoiResultModel`/`DefectModel` ↔ IP `result_saver.cpp` 的 JSON key 與
     legacy `JudgeResult` XML element（`GC_X/GlobalPosX/Size/Type/GL_Mean/...`，GPU 無的填 0/-1、`Filter="NoFilter"`）。
   - `ZoneSettingModel` ↔ legacy `DetectRoi`（**32 欄**，`BrightThreshold→BTH`、`DarkThreshold→DTH`）。
   - 用 `[XmlElement]`/`[JsonPropertyName]` 映射，C# 屬性可用**慣用名**。
8. **配方單一資料來源（single source of truth）**：主視窗 Recipe 區 / Step1 快速調參 / ZoneParamEditor
   共用同一份 `RecipeStore`（`PrimaryZone` = `Recipe.DetectRoiList[0]`）；任一處修改其他即時反映，
   切換配方一起重載。**不可**各 View 各自載入獨立副本（會不同步）。
9. **跨平台（Mac/Windows/Linux 同一份程式碼）**：影像用 **SixLabors.ImageSharp + Avalonia Bitmap**，
   **不用 OpenCV**；路徑一律 `Path.Combine` + `~` 展開（`RecipeService.ExpandPath`），不寫死分隔線。
10. **network-clean**：跨機（Mac↔Linux）不共用檔案系統。`LOAD_RECIPE` 送配方 **XML 內容**（非路徑）；
    Step1 結果走 TCP 回傳；DefectSort 缺陷小圖以 **PNG bytes(base64)** 經 TCP 取回。
    `IpClient` 的 TCP 讀取**整行累積 bytes 後一次 UTF-8 解碼**（不可逐 byte→char，否則中文亂碼）。
11. **連線心跳偵測**：`ConnectionManager` 每 2.5s `CHECK_HEALTH`（5s 逾時、**連續 2 次失敗才判斷線**＝★8
    實測門檻；IsBusy 時跳過視為存活），失敗→紅燈 + 自動重連（避免「假綠燈」靜默失效）。Grab / 上位機同此架構。
12. **UI 佈局**：2026-07-31 起為 **dashboard + 側欄 5 頁 + 相機工作台五步驟動線**（已非早期「1:1 複製舊版」；
    版面見 docs/control_程式完整說明.md §6）。保留的原則：等比 `Grid`（**非 Canvas 絕對座標**，因 Mac
    Retina/縮放）；尚無對應功能的按鈕一律 `IsEnabled=False` + tooltip 說明緣由。
13. **AI 現況**：IP 端 RF 模型已載入但**暫不使用**（TrueDefect 樣本不足）。缺陷分類靠 **DefectSort 人工標
    TrueDefect/Particle**（即時持久化 + classification.json），結果即未來 AI 重訓的標註資料。
