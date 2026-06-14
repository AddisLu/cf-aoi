using System;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia.Media.Imaging;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using SixLabors.ImageSharp;
using SixLabors.ImageSharp.PixelFormats;
using SixLabors.ImageSharp.Processing;

namespace CfAoiControl.ViewModels;

/// <summary>單一缺陷縮圖（對應 legacy listView1 LargeIcon 項目）。</summary>
public sealed class DefectThumb
{
    public Bitmap? Image { get; init; }
    public string Label { get; init; } = "";       // dg{roi}_i{idx}
    public DefectModel Defect { get; init; } = new();
}

/// <summary>
/// 對應 legacy frmAlgorithmTestTools：載圖→調參→Test(送IP)→縮圖牆→點選。
/// 影像區為「基本」：顯示全圖(縮放適合) + 缺陷框 overlay（亮紅/暗藍），不做縮放/平移/畫ROI/像素讀出。
/// </summary>
public partial class Step1ViewModel : ViewModelBase
{
    private readonly AppServices _svc;
    private const int PatchSize = 100;
    private const int ThumbSize = 64;
    private const int DisplayMaxW = 1632;   // 顯示用縮圖最大寬（8160/5）

    public Step1ViewModel(AppServices svc)
    {
        _svc = svc;
        SelectedImagePath = DefaultImagePath();
        RefreshRecipeNames();
    }

    // 影像 / 配方
    [ObservableProperty] private string selectedImagePath = "";
    [ObservableProperty] private string selectedRecipe = "DEFAULT";
    public ObservableCollection<string> RecipeNames { get; } = new();

    // 快速調參（BrightThreshold→BTH, DarkThreshold→DTH）
    [ObservableProperty] private double brightThreshold = 1.4;
    [ObservableProperty] private double darkThreshold = 0.6;
    [ObservableProperty] private int pitchX = 26;
    [ObservableProperty] private int pitchY = 19;
    [ObservableProperty] private int maxDrawDefectCnt = 200;   // 對應 nudMaxDrawDefectCnt（封頂避免過多縮圖）

    // 狀態
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(RunAnalysisCommand))]
    private bool isAnalyzing;
    [ObservableProperty] private bool showAutoGenWarning;
    // 影像是否已載入記憶體（Test 的 CanExecute 依此；改變時通知重新評估）
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(RunAnalysisCommand))]
    private bool imageLoaded;

    // 影像顯示 + overlay
    [ObservableProperty] private Bitmap? sourceBitmap;
    [ObservableProperty] private int imageWidth = 1;
    [ObservableProperty] private int imageHeight = 1;
    public ObservableCollection<DefectModel> DrawDefects { get; } = new();   // overlay 用（封頂）
    public ObservableCollection<DefectThumb> Thumbs { get; } = new();        // 縮圖牆
    [ObservableProperty] private DefectThumb? selectedThumb;
    [ObservableProperty] private DefectModel? selectedDefect;

    // 8 格狀態列
    [ObservableProperty] private string imageSizeText = "ImageSize : -";
    [ObservableProperty] private string axisText = "| Axis : -";
    [ObservableProperty] private string valueText = "| Value : -";
    [ObservableProperty] private string zoomText = "| Zoom : Fit";
    [ObservableProperty] private string selectedText = "| Selected : -";
    [ObservableProperty] private string recipeText = "| Recipe : -";
    [ObservableProperty] private string regionText = "| Region : -";
    [ObservableProperty] private string defectCntText = "| DefectCnt : 0";

    public Func<Task<string?>>? FilePicker { get; set; }

    partial void OnSelectedThumbChanged(DefectThumb? value)
    {
        SelectedDefect = value?.Defect;
        SelectedText = value is null ? "| Selected : -"
            : $"| Selected : {value.Label} ({value.Defect.GlobalPosX},{value.Defect.GlobalPosY})";
    }

    // 對應 btnLoadImage：選檔後立即讀入記憶體、顯示、更新 ImageSize、enable Test
    [RelayCommand]
    private async Task BrowseImage()
    {
        if (FilePicker is null) return;
        var p = await FilePicker();
        if (string.IsNullOrEmpty(p)) return;
        SelectedImagePath = p!;
        LoadImageForDisplay(p!);
    }

    /// <summary>讀入影像並顯示在黑底區（ImageSharp，純 managed；跨平台）。</summary>
    private void LoadImageForDisplay(string path)
    {
        try
        {
            using var src = Image.Load<L8>(path);
            BuildDisplayBitmap(src);
            DrawDefects.Clear();
            Thumbs.Clear();
            SelectedThumb = null;
            RegionText = "| Region : 全幅";
            DefectCntText = "| DefectCnt : 0";
            ImageLoaded = true;                 // → RunAnalysisCommand 重新評估 CanExecute
            _svc.Log.Info($"已載入影像 {Path.GetFileName(path)} ({ImageWidth}x{ImageHeight})");
        }
        catch (Exception ex)
        {
            ImageLoaded = false;
            _svc.Log.Error($"載入影像失敗：{ex.Message}");
        }
    }

    /// <summary>建立顯示用縮圖（等比縮到 DisplayMaxW）並安全替換 SourceBitmap。</summary>
    private void BuildDisplayBitmap(Image<L8> src)
    {
        ImageWidth = src.Width;
        ImageHeight = src.Height;
        ImageSizeText = $"ImageSize : {src.Width}x{src.Height}";
        using var disp = src.Clone(c => c.Resize(new ResizeOptions
        {
            Mode = ResizeMode.Max, Size = new Size(DisplayMaxW, DisplayMaxW * 10)
        }));
        using var ms = new MemoryStream();
        disp.SaveAsPng(ms); ms.Position = 0;
        var old = SourceBitmap;
        SourceBitmap = new Bitmap(ms);          // 先換新（UI 重綁）再釋放舊，避免存取已釋放點陣圖
        old?.Dispose();
    }

    // Test 可按條件：有影像（已載入）+ 非分析中（配方恆有預設）
    private bool CanRun() => !IsAnalyzing && ImageLoaded;

    // 對應 btnTest：送 IP 分析（offline-tcp, network-clean）
    [RelayCommand(CanExecute = nameof(CanRun))]
    private async Task RunAnalysis()
    {
        IsAnalyzing = true;
        try
        {
            var overrideZone = new ZoneSettingModel
            {
                BrightThreshold = BrightThreshold, DarkThreshold = DarkThreshold,
                PitchX = PitchX, PitchY = PitchY, AlgorithmCompare = "DIV",
            };
            var outcome = await _svc.Review.AnalyzeImageAsync(SelectedImagePath, SelectedRecipe, 0, overrideZone);
            ShowAutoGenWarning = outcome.RecipeAutoGenerated;
            var result = outcome.Result;

            // 載入原圖一次：建顯示縮圖 + 缺陷縮圖牆 + overlay
            BuildVisuals(SelectedImagePath, result);

            RecipeText = $"| Recipe : {SelectedRecipe}";
            DefectCntText = $"| DefectCnt : {result.DefectCnt}";
        }
        catch (Exception ex) { _svc.Log.Error($"分析失敗：{ex.Message}"); }
        finally { IsAnalyzing = false; }
    }

    private void BuildVisuals(string imagePath, DefectResultModel result)
    {
        Thumbs.Clear();
        DrawDefects.Clear();
        SelectedThumb = null;

        using var src = Image.Load<L8>(imagePath);
        BuildDisplayBitmap(src);
        RegionText = "| Region : 全幅";

        // overlay + 縮圖（封頂 MaxDrawDefectCnt）
        int half = ThumbSize / 2;
        int idx = 0;
        foreach (var d in result.AllDefects)
        {
            if (idx >= MaxDrawDefectCnt) break;
            DrawDefects.Add(d);

            int x = Math.Clamp(d.GlobalPosX - half, 0, Math.Max(0, src.Width - ThumbSize));
            int y = Math.Clamp(d.GlobalPosY - half, 0, Math.Max(0, src.Height - ThumbSize));
            int w = Math.Min(ThumbSize, src.Width - x), h = Math.Min(ThumbSize, src.Height - y);
            Bitmap? thumb = null;
            try
            {
                using var patch = src.Clone(c => c.Crop(new Rectangle(x, y, w, h)));
                using var ms = new MemoryStream();
                patch.SaveAsPng(ms); ms.Position = 0;
                thumb = new Bitmap(ms);
            }
            catch { }
            Thumbs.Add(new DefectThumb { Image = thumb, Label = $"dg0_i{idx}", Defect = d });
            idx++;
        }
        if (result.DefectCnt > MaxDrawDefectCnt)
            _svc.Log.Warn($"缺陷 {result.DefectCnt} 超過顯示上限 {MaxDrawDefectCnt}，僅顯示前 {MaxDrawDefectCnt} 個");
    }

    [RelayCommand] private async Task SaveRecipe()
    {
        var ensure = await _svc.Recipes.EnsureRecipeExistsAsync(SelectedRecipe);
        var z = ensure.Recipe.DetectRoiList.FirstOrDefault();
        if (z is null) { z = new ZoneSettingModel(); ensure.Recipe.DetectRoiList.Add(z); }
        z.BrightThreshold = BrightThreshold; z.DarkThreshold = DarkThreshold;
        z.PitchX = PitchX; z.PitchY = PitchY; z.AlgorithmCompare = "DIV";
        await _svc.Recipes.SaveAsync(SelectedRecipe, ensure.Recipe);
        _svc.Log.Info("Setting/Recipe 已儲存");
    }

    // 缺陷 OK/NG 一鍵歸檔（AI 訓練）
    [RelayCommand] private void ClassifyOk() => FileSelectedDefect("ok");
    [RelayCommand] private void ClassifyNg() => FileSelectedDefect("ng");

    private void FileSelectedDefect(string klass)
    {
        if (SelectedDefect is null || !File.Exists(SelectedImagePath)) return;
        try
        {
            var dir = Path.Combine(RecipeService.ExpandPath(_svc.Config.Paths.OutputDir), klass);
            Directory.CreateDirectory(dir);
            using var img = Image.Load<L8>(SelectedImagePath);
            int half = PatchSize / 2;
            int x = Math.Clamp(SelectedDefect.GlobalPosX - half, 0, Math.Max(0, img.Width - PatchSize));
            int y = Math.Clamp(SelectedDefect.GlobalPosY - half, 0, Math.Max(0, img.Height - PatchSize));
            int w = Math.Min(PatchSize, img.Width - x), h = Math.Min(PatchSize, img.Height - y);
            using var patch = img.Clone(c => c.Crop(new Rectangle(x, y, w, h)));
            var name = $"{Path.GetFileNameWithoutExtension(SelectedImagePath)}_{SelectedDefect.GlobalPosX}_{SelectedDefect.GlobalPosY}_{SelectedDefect.Type}.png";
            patch.SaveAsPng(Path.Combine(dir, name));
            _svc.Log.Info($"已歸檔 {klass.ToUpper()}：{name}");
        }
        catch (Exception ex) { _svc.Log.Error($"歸檔失敗：{ex.Message}"); }
    }

    private void RefreshRecipeNames()
    {
        RecipeNames.Clear();
        RecipeNames.Add("DEFAULT");
        try
        {
            var root = RecipeService.ExpandPath(_svc.Config.Paths.RecipeDir);
            if (Directory.Exists(root))
                foreach (var d in Directory.GetDirectories(root))
                {
                    var n = Path.GetFileName(d);
                    if (!RecipeNames.Contains(n)) RecipeNames.Add(n);
                }
        }
        catch { }
        if (!RecipeNames.Contains(SelectedRecipe)) SelectedRecipe = "DEFAULT";
    }

    private string DefaultImagePath()
    {
        try
        {
            var dir = RecipeService.ExpandPath(_svc.Config.Paths.ImageDir);
            if (Directory.Exists(dir))
            {
                var first = Directory.EnumerateFiles(dir).FirstOrDefault(f =>
                    f.EndsWith(".tif", StringComparison.OrdinalIgnoreCase) ||
                    f.EndsWith(".png", StringComparison.OrdinalIgnoreCase) ||
                    f.EndsWith(".bmp", StringComparison.OrdinalIgnoreCase));
                if (first != null) return first;
            }
        }
        catch { }
        return "";
    }
}
