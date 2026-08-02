using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using CfAoiControl.Controllers;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>
/// 相機工作台（操作員單頁動線，設計見 docs/design/operator_ui/）：
/// 左欄槽位卡（宣告×偵測 join + 五步驟進度點）→ 右側五步驟面板
/// ①綁定 ②取像驗證 ③對位 Mark ④調參與缺陷 ⑤套用到其他相機（= 生產順序）。
/// 邏輯全部重用既有引擎：綁定/join/調參 = SystemSettingsViewModel（Gap #21/#2）、
/// 配方/對位 = RecipeStore（#6/#34-A2）、檢測 = SingleCcdSetup（塊3）、複製 = RecipeService。
/// </summary>
public partial class CameraWorkbenchViewModel : ViewModelBase
{
    private readonly AppServices _svc;

    /// <summary>綁定/相機引擎（與系統設定頁共用同一實例 → 狀態一致）。</summary>
    public SystemSettingsViewModel Engine { get; }
    /// <summary>Step 4 嵌入的檢測工作台（與單 CCD 頁共用同一實例）。</summary>
    public SingleCcdSetupViewModel SingleCcd { get; }
    public RecipeStore Store => _svc.RecipeStore;

    public CameraWorkbenchViewModel(AppServices svc, SystemSettingsViewModel engine,
                                    SingleCcdSetupViewModel singleCcd)
    {
        _svc = svc; Engine = engine; SingleCcd = singleCcd;
        Engine.PropertyChanged += OnEnginePropertyChanged;
        RebuildRail();
    }

    /// <summary>以引擎的 KPI 變更當作「相機清單已更新」的訊號，重建左欄。
    /// ⚠️ 已知限制（docs/code_review_20260802.md K9）：`[ObservableProperty]` 同值不發通知——
    /// 若拔換相機後四個 KPI 數字剛好不變（例如 1 換 1），本方法不會被呼叫，`Unassigned` 會殘留上一輪的物件。</summary>
    private void OnEnginePropertyChanged(object? s, PropertyChangedEventArgs e)
    {
        // 引擎每次列舉/綁定後 KPI 會變 → 重建左欄（含 join 後的槽位狀態）
        if (e.PropertyName is nameof(Engine.BoundCount) or nameof(Engine.UnboundCount)
            or nameof(Engine.OfflineCount) or nameof(Engine.DeclaredSlotCount))
            RebuildRail();
    }

    // ── 左欄：槽位卡 ─────────────────────────────────────────────────────────
    public ObservableCollection<WorkbenchSlot> Rail { get; } = new();
    /// <summary>未指派的相機（bound=false）——綁定唯一入口。</summary>
    public ObservableCollection<CameraInfoModel> Unassigned { get; } = new();

    [ObservableProperty] private WorkbenchSlot? selectedSlot;
    public bool HasSlot => SelectedSlot is not null;

    // 每槽 session 進度（②取像 mean / ④調參 / ⑤套用；①③由狀態推導）。
    // **生命週期刻意為「本次 App 執行期」**：以 ccd_id 為鍵存在此字典，不落盤——
    // 進度點是給操作員看「這台我做到哪」的臨時提示，真正的持久狀態在別處（①=cam_map.json、
    // ③=配方 XML 的 M_AlignRoi），重開程式後由那些真相重新推導，不會出現「檔案沒存但顯示已完成」的假象。
    // 因為以 ccd_id 為鍵，RebuildRail 重建 WorkbenchSlot 物件時進度不會遺失（同槽拿回同一個 SlotProgress）。
    private readonly Dictionary<string, SlotProgress> _progress = new();
    private SlotProgress ProgressOf(string ccd)
        => _progress.TryGetValue(ccd, out var p) ? p : _progress[ccd] = new SlotProgress();

    /// <summary>重建左欄槽位卡。呼叫時機：① 建構 ② 引擎 KPI 變（列舉/綁定完成）③ 綁定/解除後手動呼叫。
    /// 每次都**重新 new WorkbenchSlot**（因為 join 結果 SlotBinding 由引擎重算），但進度物件依 ccd_id 沿用；
    /// 重建後盡量保留原本選中的槽（找不到才退回第一個），避免操作員每次列舉都被踢回 CCD00。</summary>
    public void RebuildRail()
    {
        var sel = SelectedSlot?.CcdId;
        Rail.Clear(); Unassigned.Clear();
        foreach (var g in Engine.ComputeUnits)
            foreach (var sb in g.Slots)
                Rail.Add(new WorkbenchSlot(sb, ProgressOf(sb.CcdId)));
        // 未綁定的相機 = 綁定步驟的唯一候選來源（約束②：bound=false 者永不指派到任何槽）
        foreach (var c in Engine.Cameras.Where(c => !c.Bound)) Unassigned.Add(c);
        SelectedSlot = Rail.FirstOrDefault(r => r.CcdId == sel) ?? Rail.FirstOrDefault();
        OnPropertyChanged(nameof(HasSlot));
    }

    [RelayCommand] private void SelectRailSlot(WorkbenchSlot? s) { if (s is not null) SelectedSlot = s; }

    /// <summary>切換槽 = 把「所有下游狀態」一次對齊到這一台（工作台的核心動作）：
    /// 引擎的選中槽/相機（綁定與曝光命令都吃引擎的選中狀態）→ 配方分區（LoadSlot 會設
    /// RecipeStore.SelectedIp = 該槽 recipe_partition 並重載該 CCD 的配方）→ 對位欄位 → 各步驟面板衍生屬性。
    /// ⚠️ 已知限制（docs/code_review_20260802.md K4）：LoadSlot 會從磁碟重載配方，
    /// **上一台尚未儲存的調參會直接被丟棄且無提示**——切槽前請先按「儲存配方」。</summary>
    partial void OnSelectedSlotChanged(WorkbenchSlot? value)
    {
        OnPropertyChanged(nameof(HasSlot));
        if (value is null) return;
        // 引擎的選中槽/相機跟著走（綁定/調參命令吃引擎的選中狀態）
        Engine.SelectSlotCommand.Execute(value.Binding.Slot);
        if (value.Binding.Camera is not null) Engine.SelectCameraCommand.Execute(value.Binding.Camera);
        // 配方/對位/檢測切到該槽的分區
        SingleCcd.LoadSlot(value.Binding.Slot);
        LoadAlignFields();
        RefreshStepPanels();
    }

    [RelayCommand] private Task RefreshCameras() => Engine.LoadCamerasCommand.ExecuteAsync(null);

    // ── 步驟導航 ─────────────────────────────────────────────────────────────
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsStep1))]
    [NotifyPropertyChangedFor(nameof(IsStep2))]
    [NotifyPropertyChangedFor(nameof(IsStep3))]
    [NotifyPropertyChangedFor(nameof(IsStep4))]
    [NotifyPropertyChangedFor(nameof(IsStep5))]
    private int currentStep = 1;

    public bool IsStep1 => CurrentStep == 1;
    public bool IsStep2 => CurrentStep == 2;
    public bool IsStep3 => CurrentStep == 3;
    public bool IsStep4 => CurrentStep == 4;
    public bool IsStep5 => CurrentStep == 5;

    [RelayCommand] private void GoStep(string? n)
    { if (int.TryParse(n, out var v) && v is >= 1 and <= 5) CurrentStep = v; RefreshStepPanels(); }

    /// <summary>統一通知所有「由選中槽/引擎狀態推導出來」的唯讀屬性刷新。
    /// 這些屬性沒有自己的欄位（都是 getter 即時算），故換槽或換步驟時必須手動 raise，UI 才會更新。</summary>
    private void RefreshStepPanels()
    {
        OnPropertyChanged(nameof(BindPanelMode));
        OnPropertyChanged(nameof(BindModeIsBind));
        OnPropertyChanged(nameof(BindModeIsBound));
        OnPropertyChanged(nameof(BindModeIsMismatch));
        OnPropertyChanged(nameof(BoundCamText));
        OnPropertyChanged(nameof(MismatchText));
        OnPropertyChanged(nameof(MeanText));
        OnPropertyChanged(nameof(MeanJudgment));
        RefreshCopyTargets();
    }

    // ── Step 1：綁定 ─────────────────────────────────────────────────────────
    /// <summary>bind = 未綁定（顯示候選清單）；bound = 已綁定（檢視/解除）；mismatch = MAC 不符。</summary>
    public string BindPanelMode => SelectedSlot?.Binding.Kind switch
    {
        SlotBindKind.Bound       => "bound",
        SlotBindKind.MacMismatch => "mismatch",
        _                        => "bind",
    };
    public bool BindModeIsBind     => BindPanelMode == "bind";
    public bool BindModeIsBound    => BindPanelMode == "bound";
    public bool BindModeIsMismatch => BindPanelMode == "mismatch";

    public string BoundCamText => SelectedSlot?.Binding.Camera is { } c
        ? $"{c.Model} · SN {c.Serial}\nMAC {c.MacDisplay}\nIP {c.IpDisplay}"
        : "—";

    public string MismatchText => SelectedSlot?.Binding is { Kind: SlotBindKind.MacMismatch } b
        ? $"預期 {b.Slot.ExpectedMacDisplay}\n實際 {b.Camera?.MacDisplay}（SN {b.Camera?.Serial}）"
        : "";

    [ObservableProperty] private CameraInfoModel? selectedCandidate;
    [ObservableProperty] private bool showUnbindConfirm;
    public string BindStatus => Engine.BindActionStatus;

    /// <summary>候選 → 綁到目前槽（引擎 #21-f：拒搶槽 + 完整表語意）。</summary>
    [RelayCommand]
    private async Task BindCandidate()
    {
        if (SelectedCandidate is null || SelectedSlot is null) return;
        Engine.SelectCameraCommand.Execute(SelectedCandidate);
        Engine.SelectSlotCommand.Execute(SelectedSlot.Binding.Slot);
        await Engine.BindSelectedCommand.ExecuteAsync(null);
        OnPropertyChanged(nameof(BindStatus));
        RebuildRail();
    }

    [RelayCommand] private void AskUnbind()    => ShowUnbindConfirm = true;
    [RelayCommand] private void CancelUnbind() => ShowUnbindConfirm = false;

    [RelayCommand]
    private async Task ConfirmUnbind()
    {
        ShowUnbindConfirm = false;
        if (SelectedSlot?.Binding.Camera is not { } cam) return;
        Engine.SelectCameraCommand.Execute(cam);
        await Engine.UnbindSelectedCommand.ExecuteAsync(null);
        OnPropertyChanged(nameof(BindStatus));
        RebuildRail();
    }

    /// <summary>MAC 不符 →「確認換機」：改綁實際出現的那台（原相機自動回未指派）。</summary>
    [RelayCommand]
    private async Task RebindActual()
    {
        if (SelectedSlot?.Binding is not { Kind: SlotBindKind.MacMismatch, Camera: { } cam }) return;
        Engine.SelectCameraCommand.Execute(cam);
        Engine.SelectSlotCommand.Execute(SelectedSlot.Binding.Slot);
        await Engine.BindSelectedCommand.ExecuteAsync(null);
        OnPropertyChanged(nameof(BindStatus));
        RebuildRail();
    }

    // ── Step 2：取像驗證（TUNE_MEAN 抓一幀；曝光/增益 = 引擎 Gap #2）────────────
    // 直通引擎的值（不另存一份），確保「滑桿顯示的」與「命令實際送出的」是同一個數。
    // ⚠️ 已知限制（docs/code_review_20260802.md K8）：只在自己 set 時 raise——引擎因換相機而
    // 非同步改寫 ExposureUs/GainRaw（RefreshCamParams）時本處收不到通知，滑桿會停在上一台的值。
    public double ExposureUs { get => Engine.ExposureUs; set { Engine.ExposureUs = value; OnPropertyChanged(); } }
    public int GainRaw       { get => Engine.GainRaw;    set { Engine.GainRaw = value;    OnPropertyChanged(); } }

    public string MeanText => SelectedSlot is { } s && ProgressOf(s.CcdId).MeanGray is { } m
        ? m.ToString("F1") : "—";

    /// <summary>mean gray 判讀（6 相機架設現場最常見的誤判防呆）：
    /// 幾乎全黑時操作員容易誤判「相機壞了」而去拆線，實際上多半是光源沒開或曝光太低，
    /// 故 &lt;3 直接點名「暗場、非相機故障」；工作點約 90。</summary>
    public string MeanJudgment
    {
        get
        {
            if (SelectedSlot is null || ProgressOf(SelectedSlot.CcdId).MeanGray is not { } m) return "";
            if (m < 3)  return "⚠ 暗場（沒開光源）— 非相機故障";
            if (m < 30) return "偏暗 — 建議加曝光/增益或補光";
            return "✓ 亮度正常";
        }
    }

    [ObservableProperty] private string captureStatus = "";

    /// <summary>抓一幀：以目前滑桿值 TUNE_MEAN（套用+抓幀+回 mean）。</summary>
    [RelayCommand]
    private async Task GrabOne()
    {
        if (SelectedSlot?.Binding.Camera is not { } cam) { CaptureStatus = "此槽未綁定相機"; return; }
        CaptureStatus = "抓幀中…";
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(20));
            var r = await _svc.Connection.Grab.TuneMeanAsync(cam.CamId, ExposureUs, GainRaw, cts.Token);
            if (r is null) { CaptureStatus = "ERR：Grab 回應失敗（取像中?）"; return; }
            ProgressOf(SelectedSlot.CcdId).MeanGray = r.MeanGray;
            SelectedSlot.RefreshDots();
            OnPropertyChanged(nameof(MeanText));
            OnPropertyChanged(nameof(MeanJudgment));
            CaptureStatus = $"實際 exp={r.ExposureUsActual:F0}µs gain={r.GainRawActual}";
        }
        catch (Exception ex) { CaptureStatus = $"ERR：{ex.Message}"; }
    }

    /// <summary>套用（寫 cam_config，跟槽位走）並再抓一幀。</summary>
    [RelayCommand]
    private async Task ApplyAndGrab()
    {
        if (SelectedSlot?.Binding.Camera is not { } cam) { CaptureStatus = "此槽未綁定相機"; return; }
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            var r = await _svc.Connection.Grab.SetCamParamsAsync(cam.CamId, ExposureUs, GainRaw, cts.Token);
            if (r is null) { CaptureStatus = "ERR：套用失敗"; return; }
        }
        catch (Exception ex) { CaptureStatus = $"ERR：{ex.Message}"; return; }
        await GrabOne();
    }

    // ── Step 3：對位 Mark（#34-A2 M_AlignRoi，寫進該槽分區配方）──────────────
    [ObservableProperty] private bool alignEnable;
    [ObservableProperty] private int alignReferX;
    [ObservableProperty] private int alignReferY;
    [ObservableProperty] private int alignSearchW;
    [ObservableProperty] private int alignSearchH;
    [ObservableProperty] private string alignPatternPath = "";
    [ObservableProperty] private string alignStatus = "";

    private void LoadAlignFields()
    {
        var a = Store.Recipe.AlignRoi;
        AlignEnable = a.AlignEnable;
        AlignReferX = a.ReferX; AlignReferY = a.ReferY;
        AlignSearchW = a.SearchWidth; AlignSearchH = a.SearchHeight;
        AlignPatternPath = a.PatternPath;
        AlignStatus = "";
    }

    /// <summary>把 Mark 欄位寫回**該槽分區**的配方（SelectedSlot 變更時已把 RecipeStore.SelectedIp
    /// 切到該槽的 recipe_partition，故 Store.Save() 自然落在 {recipe}/{分區}/RecipeInfo.xml）。
    /// 每台 CCD 各教各的 Mark（legacy 模型 A：本地 ROI + 各自對位 Mark 吸收起始點差異）。</summary>
    [RelayCommand]
    private void SaveAlign()
    {
        if (SelectedSlot is null) return;
        var a = Store.Recipe.AlignRoi;
        a.AlignEnable = AlignEnable;
        a.ReferX = AlignReferX; a.ReferY = AlignReferY;
        a.SearchWidth = AlignSearchW; a.SearchHeight = AlignSearchH;
        a.PatternPath = AlignPatternPath;
        Store.Save();
        ProgressOf(SelectedSlot.CcdId).AlignSaved = true;
        SelectedSlot.RefreshDots();
        AlignStatus = $"✓ 已存入 {Store.SelectedRecipe}/{SelectedSlot.Binding.Slot.RecipePartition}";
    }

    // ── Step 4：調參與缺陷 = 嵌入 SingleCcdSetup（完整檢測鏈）。標記進度用。──────
    [RelayCommand]
    private void MarkTuned()
    {
        if (SelectedSlot is null) return;
        ProgressOf(SelectedSlot.CcdId).Tuned = true;
        SelectedSlot.RefreshDots();
    }

    // ── Step 5：套用到其他相機（跨相機分區複製 + 可選曝光/增益）────────────────
    [ObservableProperty] private bool copyDetectParams = true;
    [ObservableProperty] private bool copyExposureGain = true;
    [ObservableProperty] private bool copyAlignMark;     // 預設不複製（各教各的）
    [ObservableProperty] private string copyStatus = "";

    public ObservableCollection<CopyTarget> CopyTargets { get; } = new();

    /// <summary>目標清單 = 除來源槽外的所有槽（已綁定者預設勾選、未綁定不可選，見 CopyTarget.CanSelect）。
    /// ⚠️ 已知限制（docs/code_review_20260802.md K18）：每次切換步驟/換槽都會重建，
    /// 使用者手動調整過的勾選狀態會被重設回預設值。</summary>
    private void RefreshCopyTargets()
    {
        CopyTargets.Clear();
        foreach (var r in Rail.Where(r => r.CcdId != SelectedSlot?.CcdId))
            CopyTargets.Add(new CopyTarget(r));
    }

    /// <summary>
    /// Step 5 批次套用：檢測參數走「配方分區複製」（磁碟對磁碟），曝光/增益逐台下發 SET_CAM_PARAMS。
    /// ⚠️ 已知限制（docs/code_review_20260802.md K4）：CopyParamsToIps 的**來源是磁碟上已儲存的
    /// RecipeInfo.xml**，不是畫面上的記憶體值——Step 4 調完若沒按「儲存配方」，複製出去的會是舊參數
    /// （且狀態列仍回報 ✓）。套用前務必先存檔。
    /// 曝光/增益失敗不中斷整批：逐台記在該列 Result，最後彙總「成功 N / 失敗 M」，
    /// 避免第 3 台失敗就讓後面 3 台完全沒下發。
    /// </summary>
    [RelayCommand]
    private async Task ApplyToTargets()
    {
        if (SelectedSlot is null) { CopyStatus = "先選來源槽"; return; }
        var targets = CopyTargets.Where(t => t.Selected).ToList();
        if (targets.Count == 0) { CopyStatus = "沒有選擇目標"; return; }

        int okRecipes = 0, okCams = 0, fail = 0;
        var src = SelectedSlot.Binding.Slot;
        if (CopyDetectParams)
        {
            try
            {
                okRecipes = _svc.Recipes.CopyParamsToIps(
                    Store.SelectedRecipe, src.RecipePartition,
                    targets.Select(t => t.Slot.Binding.Slot.RecipePartition), CopyAlignMark);
            }
            catch (Exception ex) { CopyStatus = $"ERR：{ex.Message}"; return; }
        }
        if (CopyExposureGain)
        {
            foreach (var t in targets)
            {
                if (t.Slot.Binding.Camera is not { } cam) { t.Result = "跳過（未綁定）"; continue; }
                try
                {
                    using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
                    var r = await _svc.Connection.Grab.SetCamParamsAsync(cam.CamId, ExposureUs, GainRaw, cts.Token);
                    if (r is null) { t.Result = "✗ 曝光套用失敗"; fail++; }
                    else { t.Result = "✓"; okCams++; }
                }
                catch (Exception ex) { t.Result = $"✗ {ex.Message}"; fail++; }
            }
        }
        ProgressOf(SelectedSlot.CcdId).Copied = true;
        SelectedSlot.RefreshDots();
        CopyStatus = $"✓ 參數 {okRecipes} 分區 · 曝光/增益 {okCams} 台"
                     + (fail > 0 ? $" · 失敗 {fail}（見各列）" : "")
                     + " — 建議每台回 Step 4 各 Test 一次";
    }
}

/// <summary>每槽的 session 進度（②~⑤；①③亦有持久面：綁定=join、對位=配方檔）。</summary>
public sealed class SlotProgress
{
    public double? MeanGray;   // Step 2 最近一次抓幀
    public bool AlignSaved;    // Step 3 本 session 存過 Mark
    public bool Tuned;         // Step 4 標記完成
    public bool Copied;        // Step 5 套用過
}

/// <summary>左欄槽位卡：宣告×偵測 join（SlotBinding）+ 五步驟進度點。</summary>
public sealed partial class WorkbenchSlot : ObservableObject
{
    public WorkbenchSlot(SlotBinding binding, SlotProgress progress)
    { Binding = binding; Progress = progress; }

    public SlotBinding Binding { get; }
    public SlotProgress Progress { get; }
    public string CcdId => Binding.CcdId;
    public SlotBindKind Kind => Binding.Kind;

    public string ChipLabel => Kind switch
    {
        SlotBindKind.Bound => "已綁定", SlotBindKind.MacMismatch => "MAC 不符",
        SlotBindKind.Offline => "離線", _ => "未綁定",
    };

    public string MetaLine => Binding.Camera is { } c
        ? $"{c.Model} · SN {c.Serial}" + (Progress.MeanGray is { } m ? $" · mean {m:F1}" : "")
        : (Kind == SlotBindKind.Offline ? $"預期 {Binding.Slot.ExpectedMacDisplay}" : "等待綁定");

    // 五步驟進度點：①綁定 ②取像 ③對位 ④調參 ⑤套用。
    // ① 由 **持久狀態**（join 結果 = cam_map.json）推導：MAC 不符也算「有相機就位」故亦為 done；
    // ②③④⑤ 讀本次 session 的 SlotProgress（見 _progress 註解：刻意不落盤）。
    public bool Done1 => Kind is SlotBindKind.Bound or SlotBindKind.MacMismatch;
    public bool Done2 => Progress.MeanGray is not null;
    public bool Done3 => Progress.AlignSaved;
    public bool Done4 => Progress.Tuned;
    public bool Done5 => Progress.Copied;

    /// <summary>進度點/文字全部是無欄位的衍生屬性（SlotProgress 為普通類別、不發通知），
    /// 故每次改完 Progress 的欄位都要由呼叫端顯式喚起本方法，卡片才會更新。</summary>
    public void RefreshDots()
    {
        OnPropertyChanged(nameof(Done1)); OnPropertyChanged(nameof(Done2));
        OnPropertyChanged(nameof(Done3)); OnPropertyChanged(nameof(Done4));
        OnPropertyChanged(nameof(Done5)); OnPropertyChanged(nameof(MetaLine));
        OnPropertyChanged(nameof(ChipLabel)); OnPropertyChanged(nameof(Kind));
    }
}

/// <summary>Step 5 目標列。</summary>
public sealed partial class CopyTarget : ObservableObject
{
    public CopyTarget(WorkbenchSlot slot)
    { Slot = slot; Selected = CanSelect; }

    public WorkbenchSlot Slot { get; }
    public string CcdId => Slot.CcdId;
    public string Meta => Slot.Binding.Camera?.Model ?? (Slot.Kind == SlotBindKind.Offline ? "離線" : "未綁定");
    public bool CanSelect => Slot.Kind is SlotBindKind.Bound or SlotBindKind.MacMismatch;
    [ObservableProperty] private bool selected;
    [ObservableProperty] private string result = "";
}
