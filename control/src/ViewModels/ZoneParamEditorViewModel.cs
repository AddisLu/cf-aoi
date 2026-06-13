using System.Collections.ObjectModel;
using System.Linq;
using System.Threading.Tasks;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>
/// 配方 zone 編輯（A5 #3 分組欄位編輯 + A5 #2 批次套用到全部 ROI + ROI 增量位移）。
/// SelectedZone 為 ZoneSettingModel（32 欄，ObservableObject），View 以分組表單雙向綁定。
/// </summary>
public partial class ZoneParamEditorViewModel : ViewModelBase
{
    private readonly AppServices _svc;

    public ZoneParamEditorViewModel(AppServices svc)
    {
        _svc = svc;
        RefreshRecipeNames();
    }

    public ObservableCollection<string> RecipeNames { get; } = new();
    public ObservableCollection<ZoneSettingModel> Zones { get; } = new();

    // 編輯器下拉選項
    public static ImagePreproc[] ImagePreprocValues => System.Enum.GetValues<ImagePreproc>();
    public static AlgorithmWayCompare[] AlgorithmWayCompareValues => System.Enum.GetValues<AlgorithmWayCompare>();
    public static string[] AlgorithmCompareOptions { get; } = { "DIV" }; // 只支援 DIV

    [ObservableProperty] private string selectedRecipe = "DEFAULT";
    [ObservableProperty] private ZoneSettingModel? selectedZone;
    [ObservableProperty] private string statusMessage = "";
    [ObservableProperty] private int roiShiftStep = 10;

    partial void OnSelectedRecipeChanged(string value) => _ = LoadRecipeAsync(value);

    private async Task LoadRecipeAsync(string name)
    {
        var ensure = await _svc.Recipes.EnsureRecipeExistsAsync(name);
        Zones.Clear();
        foreach (var z in ensure.Recipe.DetectRoiList) Zones.Add(z);
        SelectedZone = Zones.FirstOrDefault();
        StatusMessage = $"載入 {name}：{Zones.Count} 個 zone";
    }

    [RelayCommand]
    private async Task Save()
    {
        var recipe = new RecipeModel();
        foreach (var z in Zones) recipe.DetectRoiList.Add(z);
        await _svc.Recipes.SaveAsync(SelectedRecipe, recipe);
        StatusMessage = "已儲存配方";
    }

    /// <summary>A5 #2：把選取 zone 的演算法參數複製到全部 zone（保留各自 ROI/Pitch）。</summary>
    [RelayCommand]
    private void CopyToAll()
    {
        if (SelectedZone is null) return;
        foreach (var z in Zones)
        {
            if (ReferenceEquals(z, SelectedZone)) continue;
            z.BrightThreshold = SelectedZone.BrightThreshold;
            z.DarkThreshold = SelectedZone.DarkThreshold;
            z.SearchX = SelectedZone.SearchX;
            z.SearchY = SelectedZone.SearchY;
            z.AlgorithmWay = SelectedZone.AlgorithmWay;
            z.AlgorithmCompare = SelectedZone.AlgorithmCompare;
            z.BlobMinSize = SelectedZone.BlobMinSize;
            z.BlobMaxSize = SelectedZone.BlobMaxSize;
        }
        StatusMessage = "已套用至全部 zone（保留各自 ROI / Pitch）";
    }

    // ROI 增量位移（邊界夾制 0..上限），對齊 legacy frmIpParamEditor 的 +/- 操作
    [RelayCommand] private void ShiftRoi(string dir)
    {
        if (SelectedZone is null) return;
        int s = RoiShiftStep;
        switch (dir)
        {
            case "x-": SelectedZone.StartX -= s; SelectedZone.EndX -= s; break;
            case "x+": SelectedZone.StartX += s; SelectedZone.EndX += s; break;
            case "y-": SelectedZone.StartY -= s; SelectedZone.EndY -= s; break;
            case "y+": SelectedZone.StartY += s; SelectedZone.EndY += s; break;
        }
        StatusMessage = $"ROI 位移 {dir} {s}px";
    }

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
