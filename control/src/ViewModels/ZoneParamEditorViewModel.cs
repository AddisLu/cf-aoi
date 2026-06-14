using System;
using System.Collections;
using System.Collections.ObjectModel;
using System.Linq;
using System.Reflection;
using System.Threading.Tasks;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>右側 ROI 勾選項（Roi_0/1/2…）。</summary>
public sealed partial class RoiCheckItem : ObservableObject
{
    public int Index { get; init; }
    public ZoneSettingModel Zone { get; init; } = new();
    public string Label => $"Roi_{Index}";
    [ObservableProperty] private bool isChecked = true;
}

/// <summary>
/// 中間參數列：CheckBox(批次勾選) + Label + 輸入(text/bool/combo) + Update。
/// 值透過反射代理到共用編輯緩衝 ZoneSettingModel（避免換實例導致綁定失聯）。
/// </summary>
public sealed partial class ParamRow : ObservableObject
{
    private readonly ZoneSettingModel _buf;
    private readonly PropertyInfo _pi;

    public string Display { get; }
    public bool IpConsumed { get; }                 // false → 標「IP 待接」
    public string Kind { get; }                     // text/bool/enumPre/enumWay/compare
    public IEnumerable? Options { get; }
    public string PropName => _pi.Name;
    public string IpTag => IpConsumed ? "" : "IP待接";

    [ObservableProperty] private bool isChecked;

    public ParamRow(ZoneSettingModel buf, string prop, string display, bool ip, string kind, IEnumerable? options = null)
    {
        _buf = buf; _pi = typeof(ZoneSettingModel).GetProperty(prop)!;
        Display = display; IpConsumed = ip; Kind = kind; Options = options;
    }

    public bool IsText => Kind == "text";
    public bool IsBool => Kind == "bool";
    public bool IsCombo => Kind is "enumPre" or "enumWay" or "compare";

    public string TextValue
    {
        get => _pi.GetValue(_buf)?.ToString() ?? "";
        set { try { _pi.SetValue(_buf, Convert(value)); } catch { } OnPropertyChanged(); }
    }
    public bool BoolValue
    {
        get => _pi.GetValue(_buf) is true;
        set { _pi.SetValue(_buf, value); OnPropertyChanged(); }
    }
    public object? EnumValue
    {
        get => _pi.GetValue(_buf);
        set
        {
            if (value is null) return;
            try
            {
                _pi.SetValue(_buf, value is string s && _pi.PropertyType != typeof(string)
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

    public void ApplyTo(ZoneSettingModel target) => _pi.SetValue(target, _pi.GetValue(_buf));
    public string ValueString => _pi.GetValue(_buf)?.ToString() ?? "";
    public void RaiseChanged()
    {
        OnPropertyChanged(nameof(TextValue));
        OnPropertyChanged(nameof(BoolValue));
        OnPropertyChanged(nameof(EnumValue));
    }
}

/// <summary>
/// 1:1 對應 legacy frmIpParamEditor：左 Region 位移、中 27 列參數、右多 ROI 勾選。
/// 用既有 ZoneSettingModel(32 欄)；AlgorithmCompare 鎖 DIV；編輯→存 RecipeInfo.xml→IP 讀。
/// </summary>
public partial class ZoneParamEditorViewModel : ViewModelBase
{
    private readonly AppServices _svc;
    private RecipeModel _recipe = new();
    private Action? _pendingApply;

    public ZoneParamEditorViewModel(AppServices svc)
    {
        _svc = svc;
        BuildParamRows();
        RefreshRecipeNames();
        _ = LoadRecipeAsync(SelectedRecipe);
    }

    public ObservableCollection<string> RecipeNames { get; } = new();
    public ObservableCollection<RoiCheckItem> Rois { get; } = new();
    public ObservableCollection<ParamRow> ParamRows { get; } = new();
    public ZoneSettingModel Buffer { get; } = new();   // Region + 參數輸入綁此

    [ObservableProperty] private string selectedRecipe = "DEFAULT";
    [ObservableProperty] private bool updateWithAsk = true;
    [ObservableProperty] private int shiftStep = 10;
    [ObservableProperty] private string statusMessage = "";
    [ObservableProperty] private bool awaitingConfirm;
    [ObservableProperty] private string confirmText = "";

    public static ImagePreproc[] ImagePreprocValues => Enum.GetValues<ImagePreproc>();
    public static AlgorithmWayCompare[] AlgorithmWayCompareValues => Enum.GetValues<AlgorithmWayCompare>();
    public static string[] AlgorithmCompareOptions { get; } = { "DIV" };   // 鎖 DIV

    private void BuildParamRows()
    {
        void Add(string prop, string disp, bool ip, string kind, IEnumerable? opt = null)
            => ParamRows.Add(new ParamRow(Buffer, prop, disp, ip, kind, opt));

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

    partial void OnSelectedRecipeChanged(string value) => _ = LoadRecipeAsync(value);

    private async Task LoadRecipeAsync(string name)
    {
        var ensure = await _svc.Recipes.EnsureRecipeExistsAsync(name);
        _recipe = ensure.Recipe;
        Rois.Clear();
        int i = 0;
        foreach (var z in _recipe.DetectRoiList) Rois.Add(new RoiCheckItem { Index = i++, Zone = z });
        if (Rois.Count > 0) LoadRoiToBuffer(Rois[0]);
        StatusMessage = $"載入 {name}：{Rois.Count} 個 ROI";
    }

    // 點 ROI → 把其參數載入編輯緩衝（中間輸入隨之更新）
    [RelayCommand]
    private void LoadRoiToBuffer(RoiCheckItem? roi)
    {
        if (roi is null) return;
        Buffer.CopyFrom(roi.Zone);
        foreach (var r in ParamRows) r.RaiseChanged();
        OnPropertyChanged(nameof(Buffer));
        StatusMessage = $"已載入 {roi.Label} 參數到編輯區";
    }

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
        if (rois.Length > 0) LoadRoiToBuffer(rois[0]);
        StatusMessage = $"ROI 位移 {dir} {s}px（{rois.Length} 個）";
    }

    // 把編輯區的 ROI 範圍(Start/End) 套到勾選 ROI
    [RelayCommand]
    private void ApplyRoiRange()
    {
        var rois = Checked();
        if (rois.Length == 0) { StatusMessage = "未勾選任何 ROI"; return; }
        RequestApply($"套用 ROI 範圍 ({Buffer.StartX},{Buffer.StartY})-({Buffer.EndX},{Buffer.EndY}) 到 {rois.Length} 個 ROI", () =>
        {
            foreach (var r in rois)
            { r.Zone.StartX = Buffer.StartX; r.Zone.StartY = Buffer.StartY; r.Zone.EndX = Buffer.EndX; r.Zone.EndY = Buffer.EndY; }
            StatusMessage = "已套用 ROI 範圍";
        });
    }

    // ===== 單一參數 Update =====
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

    // ===== 批次：所有勾選的參數 → 勾選 ROI =====
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

    [RelayCommand]
    private async Task Save()
    {
        await _svc.Recipes.SaveAsync(SelectedRecipe, _recipe);
        StatusMessage = $"已存配方 {SelectedRecipe}（IP 可載入）";
        _svc.Log.Info($"配方 {SelectedRecipe} 已儲存（ZoneParamEditor）");
    }

    // ===== 確認流程（chkUpdateWithAsk）=====
    private void RequestApply(string summary, Action apply)
    {
        if (UpdateWithAsk) { ConfirmText = summary; _pendingApply = apply; AwaitingConfirm = true; }
        else apply();
    }
    [RelayCommand] private void ConfirmApply() { _pendingApply?.Invoke(); _pendingApply = null; AwaitingConfirm = false; }
    [RelayCommand] private void CancelApply() { _pendingApply = null; AwaitingConfirm = false; StatusMessage = "已取消"; }

    private void RefreshRecipeNames()
    {
        RecipeNames.Clear();
        RecipeNames.Add("DEFAULT");
        var root = RecipeService.ExpandPath(_svc.Config.Paths.RecipeDir);
        if (System.IO.Directory.Exists(root))
            foreach (var d in System.IO.Directory.GetDirectories(root))
            {
                var n = System.IO.Path.GetFileName(d);
                if (!RecipeNames.Contains(n)) RecipeNames.Add(n);
            }
    }
}
