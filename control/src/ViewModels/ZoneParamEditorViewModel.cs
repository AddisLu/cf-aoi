using System;
using System.Collections;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Linq;
using System.Reflection;
using System.Threading.Tasks;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>右側 ROI 勾選項（Roi_0/1/2…）。Zone 為共用配方裡的實際 DetectRoi。</summary>
public sealed partial class RoiCheckItem : ObservableObject
{
    public int Index { get; init; }
    public ZoneSettingModel Zone { get; init; } = new();
    public string Label => $"Roi_{Index}";
    [ObservableProperty] private bool isChecked = true;
}

/// <summary>目標 ROI 參照（可切換），ParamRow 透過它讀寫「當前選取 ROI」。</summary>
public sealed class ZoneRef { public ZoneSettingModel? Zone; }

/// <summary>
/// 中間參數列：CheckBox(批次勾選) + Label + 輸入(text/bool/combo) + Update。
/// 值直接讀寫「當前選取 ROI」(ZoneRef.Zone)，編輯即時生效（單一資料來源）。
/// </summary>
public sealed partial class ParamRow : ObservableObject
{
    private readonly ZoneRef _ref;
    private readonly PropertyInfo _pi;

    public string Display { get; }
    public bool IpConsumed { get; }
    public string Kind { get; }
    public IEnumerable? Options { get; }
    public string PropName => _pi.Name;
    public string IpTag => IpConsumed ? "" : "IP待接";

    [ObservableProperty] private bool isChecked;

    public ParamRow(ZoneRef target, string prop, string display, bool ip, string kind, IEnumerable? options = null)
    {
        _ref = target; _pi = typeof(ZoneSettingModel).GetProperty(prop)!;
        Display = display; IpConsumed = ip; Kind = kind; Options = options;
    }

    public bool IsText => Kind == "text";
    public bool IsBool => Kind == "bool";
    public bool IsCombo => Kind is "enumPre" or "enumWay" or "compare";

    public string TextValue
    {
        get => _ref.Zone is null ? "" : _pi.GetValue(_ref.Zone)?.ToString() ?? "";
        set { if (_ref.Zone != null) { try { _pi.SetValue(_ref.Zone, Convert(value)); } catch { } } OnPropertyChanged(); }
    }
    public bool BoolValue
    {
        get => _ref.Zone != null && _pi.GetValue(_ref.Zone) is true;
        set { if (_ref.Zone != null) _pi.SetValue(_ref.Zone, value); OnPropertyChanged(); }
    }
    public object? EnumValue
    {
        get => _ref.Zone is null ? null : _pi.GetValue(_ref.Zone);
        set
        {
            if (_ref.Zone is null || value is null) return;
            try
            {
                _pi.SetValue(_ref.Zone, value is string s && _pi.PropertyType != typeof(string)
                    ? Enum.Parse(_pi.PropertyType, s) : value);
            }
            catch { }
            OnPropertyChanged();
        }
    }

    private object Convert(string v)
    {
        var t = _pi.PropertyType;
        if (t == typeof(int)) return int.TryParse(v, out var i) ? i : 0;
        if (t == typeof(double)) return double.TryParse(v, out var d) ? d : 0.0;
        if (t == typeof(float)) return float.TryParse(v, out var f) ? f : 0f;
        if (t == typeof(bool)) return bool.TryParse(v, out var b) && b;
        return v;
    }

    public void ApplyTo(ZoneSettingModel target)
    { if (_ref.Zone != null) _pi.SetValue(target, _pi.GetValue(_ref.Zone)); }
    public string ValueString => _ref.Zone is null ? "" : _pi.GetValue(_ref.Zone)?.ToString() ?? "";
    public void RaiseChanged()
    {
        OnPropertyChanged(nameof(TextValue));
        OnPropertyChanged(nameof(BoolValue));
        OnPropertyChanged(nameof(EnumValue));
    }
}

/// <summary>
/// 1:1 對應 legacy frmIpParamEditor。配方資料來自共用 RecipeStore（single source of truth）：
/// 切換配方/載入時重建 ROI 清單；編輯選取 ROI 即時改到共用配方，Step1/主視窗同步。
/// </summary>
public partial class ZoneParamEditorViewModel : ViewModelBase
{
    private readonly AppServices _svc;
    private readonly ZoneRef _target = new();
    private ZoneSettingModel? _subscribed;
    private Action? _pendingApply;

    public RecipeStore Store => _svc.RecipeStore;
    public ObservableCollection<RoiCheckItem> Rois { get; } = new();
    public ObservableCollection<ParamRow> ParamRows { get; } = new();

    [ObservableProperty] private RoiCheckItem? selectedRoi;
    [ObservableProperty] private bool updateWithAsk = true;
    [ObservableProperty] private int shiftStep = 10;
    [ObservableProperty] private string statusMessage = "";
    [ObservableProperty] private bool awaitingConfirm;
    [ObservableProperty] private string confirmText = "";

    public ZoneSettingModel? EditZone => SelectedRoi?.Zone;   // Region 綁此（選取 ROI 的實際 Zone）

    // per-IP 對位 Mark（M_AlignRoi）：每台 CCD 自己的對位樣板/參考點，吸收各相機起始點差異。
    // 切 IP 重載 → OnRecipeReloaded 重發此屬性，UI 綁到該 CCD 的 AlignRoi。存配方時一併寫回該 IP 的 RecipeInfo.xml。
    public AlignRoiModel AlignRoi => Store.Recipe.AlignRoi;

    // PatternPath 檔案選擇（由 View 注入 StorageProvider，保持 VM 平台無關）。
    public Func<Task<string?>>? PatternPicker;

    public static ImagePreproc[] ImagePreprocValues => Enum.GetValues<ImagePreproc>();
    public static AlgorithmWayCompare[] AlgorithmWayCompareValues => Enum.GetValues<AlgorithmWayCompare>();
    public static string[] AlgorithmCompareOptions { get; } = { "DIV" };

    public ZoneParamEditorViewModel(AppServices svc)
    {
        _svc = svc;
        BuildParamRows();
        Store.RecipeReloaded += OnRecipeReloaded;
        OnRecipeReloaded();   // 以目前共用配方初始化
    }

    private void BuildParamRows()
    {
        void Add(string prop, string disp, bool ip, string kind, IEnumerable? opt = null)
            => ParamRows.Add(new ParamRow(_target, prop, disp, ip, kind, opt));

        Add("ImagePreproc", "ImagePreProc", false, "enumPre", ImagePreprocValues);
        Add("SmoothTimes", "SmoothTimes", false, "text");
        Add("SmoothTimes2", "SmoothTimes2", false, "text");
        Add("DarkThreshold", "DarkThreshold", true, "text");
        Add("BrightThreshold", "BrightThreshold", true, "text");
        Add("SobelDetectEnable", "SobelEnable", false, "bool");
        Add("SobelDarkThreshold", "SobelDark", false, "text");
        Add("SobelBrightThreshold", "SobelBright", false, "text");
        Add("AlgorithmWay", "AlgorithmWay", false, "text");
        Add("AlgorithmCompare", "AlgorithmCompare", true, "compare", AlgorithmCompareOptions);
        Add("AlgorithmWayCompare", "AlgorithmWayCompare", false, "enumWay", AlgorithmWayCompareValues);
        Add("Adjustment", "Adjustment", false, "text");
        Add("PitchTime", "PitchTime", false, "text");
        Add("ChooseAmount", "ChooseAmount", false, "text");
        Add("PitchX", "PitchX", true, "text");
        Add("PitchY", "PitchY", true, "text");
        Add("SearchX", "SearchX", true, "text");
        Add("SearchY", "SearchY", true, "text");
        Add("EdgePassRatio", "EdgePassRatio", false, "text");
        Add("EdgePassThreshold", "EdgePassThreshold", false, "text");
        Add("BlobMaxSize", "BlobMaxSize", false, "text");
        Add("BlobMinSize", "BlobMinSize", false, "text");
        Add("BlobElongation", "BlobElongation", false, "text");
        Add("BlobFeretElong", "BlobFeretElong", false, "text");
        Add("BlobDarkMergeDistance", "BlobDarkMergeDistance", false, "text");
        Add("BlobBrightMergeDistance", "BlobBrightMergeDistance", false, "text");
        Add("BlobAllMergeDistance", "BlobAllMergeDistance", false, "text");
    }

    // 共用配方切換/重載 → 重建 ROI 清單、選第一個
    private void OnRecipeReloaded()
    {
        Rois.Clear();
        int i = 0;
        foreach (var z in Store.Recipe.DetectRoiList) Rois.Add(new RoiCheckItem { Index = i++, Zone = z });
        SelectedRoi = Rois.FirstOrDefault();
        OnPropertyChanged(nameof(AlignRoi));   // 切 IP/配方 → 對位 Mark 綁定刷新到該 CCD
        StatusMessage = $"載入 {Store.SelectedRecipe}：{Rois.Count} 個 ROI";
    }

    partial void OnSelectedRoiChanged(RoiCheckItem? value)
    {
        // 退訂舊 zone、訂新 zone（外部如 Step1 改值時，中間欄位即時刷新）
        if (_subscribed != null) _subscribed.PropertyChanged -= OnZoneChanged;
        _target.Zone = value?.Zone;
        _subscribed = value?.Zone;
        if (_subscribed != null) _subscribed.PropertyChanged += OnZoneChanged;
        foreach (var r in ParamRows) r.RaiseChanged();
        OnPropertyChanged(nameof(EditZone));
    }

    private void OnZoneChanged(object? s, PropertyChangedEventArgs e)
    { foreach (var r in ParamRows) r.RaiseChanged(); }

    [RelayCommand] private void SelectRoi(RoiCheckItem? roi) { if (roi != null) SelectedRoi = roi; }

    private RoiCheckItem[] Checked() => Rois.Where(r => r.IsChecked).ToArray();

    // ===== Region 位移（套用到勾選 ROI；X 夾 [0,8160]）=====
    [RelayCommand]
    private void ShiftRoi(string dir)
    {
        var rois = Checked();
        if (rois.Length == 0) { StatusMessage = "未勾選任何 ROI"; return; }
        int s = ShiftStep;
        foreach (var r in rois)
        {
            var z = r.Zone;
            switch (dir)
            {
                case "x-": z.StartX -= s; z.EndX -= s; break;
                case "x+": z.StartX += s; z.EndX += s; break;
                case "y-": z.StartY -= s; z.EndY -= s; break;
                case "y+": z.StartY += s; z.EndY += s; break;
            }
            z.StartX = Math.Clamp(z.StartX, 0, 8160); z.EndX = Math.Clamp(z.EndX, 0, 8160);
            z.StartY = Math.Max(0, z.StartY); z.EndY = Math.Max(0, z.EndY);
        }
        StatusMessage = $"ROI 位移 {dir} {s}px（{rois.Length} 個）";
    }

    [RelayCommand]
    private void ApplyRoiRange()
    {
        if (EditZone is not { } src) return;
        var rois = Checked();
        if (rois.Length == 0) { StatusMessage = "未勾選任何 ROI"; return; }
        RequestApply($"套用 ROI 範圍 ({src.StartX},{src.StartY})-({src.EndX},{src.EndY}) 到 {rois.Length} 個 ROI", () =>
        {
            foreach (var r in rois)
            { r.Zone.StartX = src.StartX; r.Zone.StartY = src.StartY; r.Zone.EndX = src.EndX; r.Zone.EndY = src.EndY; }
            StatusMessage = "已套用 ROI 範圍";
        });
    }

    [RelayCommand]
    private void UpdateParam(ParamRow? row)
    {
        if (row is null) return;
        var rois = Checked();
        if (rois.Length == 0) { StatusMessage = "未勾選任何 ROI"; return; }
        RequestApply($"套用 {row.PropName} = {row.ValueString} 到 {rois.Length} 個 ROI", () =>
        {
            foreach (var r in rois) row.ApplyTo(r.Zone);
            StatusMessage = $"已套用 {row.PropName}";
        });
    }

    [RelayCommand]
    private void UpdateCheckedParams()
    {
        var rows = ParamRows.Where(r => r.IsChecked).ToArray();
        var rois = Checked();
        if (rows.Length == 0) { StatusMessage = "未勾選任何參數"; return; }
        if (rois.Length == 0) { StatusMessage = "未勾選任何 ROI"; return; }
        RequestApply($"套用 {rows.Length} 個參數到 {rois.Length} 個 ROI：{string.Join(", ", rows.Select(r => r.PropName))}", () =>
        {
            foreach (var row in rows)
                foreach (var r in rois) row.ApplyTo(r.Zone);
            StatusMessage = $"已批次套用 {rows.Length} 參數 × {rois.Length} ROI";
        });
    }

    [RelayCommand] private void ClearAllChk() { foreach (var r in ParamRows) r.IsChecked = false; }
    [RelayCommand] private void SelectAllChk() { foreach (var r in ParamRows) r.IsChecked = true; }

    [RelayCommand] private async Task Save() => await Store.SaveAsync();

    // 選對位 Mark 樣板檔 → 寫回該 IP 的 AlignRoi.PatternPath（存配方時一併存回 RecipeInfo.xml）
    [RelayCommand]
    private async Task BrowsePattern()
    {
        if (PatternPicker is null) return;
        var path = await PatternPicker();
        if (string.IsNullOrEmpty(path)) return;
        AlignRoi.PatternPath = path;
        OnPropertyChanged(nameof(AlignRoi));   // 通知 PatternPath TextBox 刷新
    }

    private void RequestApply(string summary, Action apply)
    {
        if (UpdateWithAsk) { ConfirmText = summary; _pendingApply = apply; AwaitingConfirm = true; }
        else apply();
    }
    [RelayCommand] private void ConfirmApply() { _pendingApply?.Invoke(); _pendingApply = null; AwaitingConfirm = false; }
    [RelayCommand] private void CancelApply() { _pendingApply = null; AwaitingConfirm = false; StatusMessage = "已取消"; }
}
