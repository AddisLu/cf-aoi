using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
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
    public int Index { get; init; }                 // 在完整缺陷清單中的索引（導航用）
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
    private const int ThumbCap = 200;       // 縮圖牆封頂（解碼效能；大圖框/導航不受限）

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
    // 影像是否已載入記憶體（Test/FFT 的 CanExecute 依此；改變時通知重新評估）
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(RunAnalysisCommand))]
    [NotifyCanExecuteChangedFor(nameof(EstimateFftCommand))]
    private bool imageLoaded;

    // 影像顯示 + overlay
    [ObservableProperty] private Bitmap? sourceBitmap;
    [ObservableProperty] private int imageWidth = 1;
    [ObservableProperty] private int imageHeight = 1;
    // 原始灰階 bytes（L8，row-major）供像素值讀出；不序列化、由 code-behind 讀。
    public byte[]? PixelData { get; private set; }
    public ObservableCollection<DefectThumb> Thumbs { get; } = new();        // 縮圖牆
    [ObservableProperty] private DefectThumb? selectedThumb;
    [ObservableProperty] private DefectModel? selectedDefect;

    // 缺陷導航（code-behind 讀此清單給 overlay 控制項 + 跳轉）
    public List<DefectModel> NavDefects { get; } = new();
    [ObservableProperty] private int selectedDefectIndex = -1;
    [ObservableProperty] private int resultVersion;   // 每次分析 +1，通知 code-behind 重綁 overlay

    // 檢測上限 / 警告 / 密度（功能 B）
    public const int DetectionCap = 10000;            // IP 端固定（cuda_kernels MAX_DEFECTS）
    [ObservableProperty] private bool defectCntAtCap;

    // FFT 估算 Pitch（功能 A）
    [ObservableProperty] private bool hasFftResult;
    [ObservableProperty] private string fftResultText = "";
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(EstimateFftCommand))]
    private bool isEstimatingFft;
    private int _fftPitchX = 26, _fftPitchY = 19;

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
        // 點縮圖 → 設定導航索引（code-behind 監看 → 跳轉大圖）
        if (value != null) SelectedDefectIndex = value.Index;
    }

    partial void OnSelectedDefectIndexChanged(int value)
    {
        if (value >= 0 && value < NavDefects.Count)
        {
            SelectedDefect = NavDefects[value];
            SelectedText = $"| Selected : 第 {value + 1}/{NavDefects.Count}";
        }
        else { SelectedDefect = null; SelectedText = "| Selected : -"; }
    }

    // ===== 功能 A：FFT 估算 Pitch =====
    private bool CanEstimateFft() => ImageLoaded && !IsEstimatingFft;

    [RelayCommand(CanExecute = nameof(CanEstimateFft))]
    private async Task EstimateFft()
    {
        var px = PixelData; int w = ImageWidth, h = ImageHeight;
        if (px is null) return;
        IsEstimatingFft = true;
        try
        {
            var r = await Task.Run(() => PitchEstimator.Estimate(px, w, h));
            _fftPitchX = r.PitchX; _fftPitchY = r.PitchY;
            HasFftResult = true;
            FftResultText = $"FFT 估算：PitchX≈{r.PitchX}（{r.ConfX}）, PitchY≈{r.PitchY}（{r.ConfY}）";
            _svc.Log.Info($"FFT 估算 PitchX={r.PitchX}(SNR{r.SnrX:F1}) PitchY={r.PitchY}(SNR{r.SnrY:F1})");
        }
        catch (Exception ex) { _svc.Log.Error($"FFT 估算失敗：{ex.Message}"); }
        finally { IsEstimatingFft = false; }
    }

    // 不自動覆蓋：使用者看到估算值後按「套用」才填入（方便對照手動 vs 估算）
    [RelayCommand]
    private void ApplyFft()
    {
        if (!HasFftResult) return;
        PitchX = _fftPitchX; PitchY = _fftPitchY;
        _svc.Log.Info($"已套用 FFT 估算：PitchX={PitchX}, PitchY={PitchY}");
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
            Thumbs.Clear();
            NavDefects.Clear();
            SelectedThumb = null;
            SelectedDefectIndex = -1;
            DefectCntAtCap = false;
            RegionText = "| Region : 全幅";
            DefectCntText = "| DefectCnt : 0";
            ResultVersion++;                    // 清空 overlay
            ImageLoaded = true;                 // → Run/FFT Command 重新評估 CanExecute
            _svc.Log.Info($"已載入影像 {Path.GetFileName(path)} ({ImageWidth}x{ImageHeight})");
        }
        catch (Exception ex)
        {
            ImageLoaded = false;
            _svc.Log.Error($"載入影像失敗：{ex.Message}");
        }
    }

    /// <summary>
    /// 建立全解析度顯示點陣圖（BGRA WriteableBitmap，nearest-neighbor）供放大看網格量 Pitch，
    /// 並保存原始灰階 bytes 供像素值讀出。載入新圖時 Dispose 舊 WriteableBitmap、釋放舊 PixelData。
    /// </summary>
    private void BuildDisplayBitmap(Image<L8> src)
    {
        int w = src.Width, h = src.Height;
        ImageWidth = w; ImageHeight = h;
        ImageSizeText = $"ImageSize : {w}x{h}";

        // 原始灰階 bytes（供 Axis/Value 讀出）— 換新陣列，舊的交給 GC
        PixelData = null;
        var px = new byte[w * h];
        src.CopyPixelDataTo(px);
        PixelData = px;

        // 全解析度 BGRA（L8→灰階 BGRA），逐列寫入避免 163MB 連續配置
        var wb = new WriteableBitmap(new Avalonia.PixelSize(w, h), new Avalonia.Vector(96, 96),
                                     Avalonia.Platform.PixelFormat.Bgra8888, Avalonia.Platform.AlphaFormat.Opaque);
        using (var fb = wb.Lock())
        {
            var row = new byte[w * 4];
            for (int y = 0; y < h; y++)
            {
                int so = y * w;
                for (int x = 0; x < w; x++)
                {
                    byte g = px[so + x];
                    int o = x * 4;
                    row[o] = g; row[o + 1] = g; row[o + 2] = g; row[o + 3] = 255;
                }
                Marshal.Copy(row, 0, IntPtr.Add(fb.Address, y * fb.RowBytes), w * 4);
            }
        }
        var old = SourceBitmap;
        SourceBitmap = wb;                      // 先換新（UI 重綁）再釋放舊，避免存取已釋放點陣圖
        old?.Dispose();                         // 釋放上一張 163MB WriteableBitmap
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

            // 載入原圖一次：建顯示圖 + 縮圖牆 + 導航清單 + 上限/密度
            BuildVisuals(SelectedImagePath, result);
            RecipeText = $"| Recipe : {SelectedRecipe}";
        }
        catch (Exception ex) { _svc.Log.Error($"分析失敗：{ex.Message}"); }
        finally { IsAnalyzing = false; }
    }

    private void BuildVisuals(string imagePath, DefectResultModel result)
    {
        Thumbs.Clear();
        NavDefects.Clear();
        SelectedThumb = null;
        SelectedDefectIndex = -1;

        using var src = Image.Load<L8>(imagePath);
        BuildDisplayBitmap(src);
        RegionText = "| Region : 全幅";

        NavDefects.AddRange(result.AllDefects);   // 完整清單（大圖框 + 導航涵蓋全部）

        // 縮圖牆（封頂 ThumbCap；Index = 完整清單索引）
        int half = ThumbSize / 2;
        for (int idx = 0; idx < NavDefects.Count && idx < ThumbCap; idx++)
        {
            var d = NavDefects[idx];
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
            Thumbs.Add(new DefectThumb { Image = thumb, Label = $"dg0_i{idx}", Index = idx, Defect = d });
        }
        if (NavDefects.Count > ThumbCap)
            _svc.Log.Warn($"縮圖僅顯示前 {ThumbCap}/{NavDefects.Count}（大圖框與 ←/→ 導航涵蓋全部）");

        // 功能 B：檢測上限 + 缺陷密度
        DefectCntAtCap = result.DefectCnt >= DetectionCap;
        double mpx = (double)ImageWidth * ImageHeight / 1_000_000.0;
        double density = mpx > 0 ? result.DefectCnt / mpx : 0;
        DefectCntText = $"| DefectCnt : {result.DefectCnt}（{density:F1}/Mpx）";
        if (DefectCntAtCap)
            _svc.Log.Warn($"⚠ 缺陷數達上限 {DetectionCap}，實際更多，表示參數過嚴或 Pitch 不符，請調整（這通常不是真實缺陷數）");
        else if (density > 50)
            _svc.Log.Warn($"⚠ 缺陷密度偏高（{density:F0}/Mpx），可能整片誤報（把網格當缺陷），請確認 Pitch/閾值");

        ResultVersion++;   // 通知 code-behind 用 NavDefects 重綁 overlay
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
