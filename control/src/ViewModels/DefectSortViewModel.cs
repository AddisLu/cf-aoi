using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>DataGrid 一列：Sort(可勾) + FolderName(唯讀) + DefectCount。對應 legacy SortFolderName。</summary>
public sealed partial class SortFolderItem : ObservableObject
{
    [ObservableProperty] private bool sort;
    public string FolderName { get; init; } = "";
    public string PanelId { get; init; } = "";
    public int DefectCount { get; init; }
}

/// <summary>缺陷小圖一張：metadata + 縮圖 + 人工分類（"未分類"/"TrueDefect"/"Particle"）。</summary>
public sealed partial class PatchItem : ObservableObject
{
    public string PatchId { get; init; } = "";
    public int RunIndex { get; init; }
    public int RoiIndex { get; init; }
    public int GcX { get; init; }
    public int GcY { get; init; }
    public int Size { get; init; }
    public string Type { get; init; } = "";   // Bright / Dark

    [ObservableProperty] private string currentClass = "未分類";  // 變更 → 邊框色（converter）
    [ObservableProperty] private Bitmap? thumb;                    // IP 批次取回後填入

    public bool IsClassified => CurrentClass is "TrueDefect" or "Particle";
    // 小圖下方標籤：型別(亮/暗) (X,Y) Size #run
    public string Caption => $"{(Type == "Bright" ? "亮" : "暗")} ({GcX},{GcY}) sz{Size} #{RunIndex}";
}

/// <summary>
/// 遠端命令版（缺陷影像存在運算端 IP/Linux，非 Control 本地）：
///   Parse → 送 LIST_DEFECT_FOLDERS 給 IP，列回當日缺陷資料夾清單填入 DataGrid。
///   Sort  → 送 SORT_DEFECTS 給 IP，IP 就地把選中 panel 的缺陷檔歸類到 output/{子目錄}，回傳結果。
/// 「輸出夾」是運算端 output 下的相對子目錄（非本地絕對路徑）。By ID / SortAll 保留。
/// Step1View 的即時 OK/NG 手動分屬另一用途（少量即時缺陷，在 Control 端操作），不在此處。
/// </summary>
public partial class DefectSortViewModel : ViewModelBase
{
    private readonly AppServices _svc;

    public DefectSortViewModel(AppServices svc)
    {
        _svc = svc;
        OutputFolder = "sorted";   // 運算端 output 下的相對子目錄
    }

    public ObservableCollection<SortFolderItem> Folders { get; } = new();
    public ObservableCollection<string> LogLines { get; } = new();

    [ObservableProperty] private DateTimeOffset? selectedDate = DateTimeOffset.Now;
    [ObservableProperty] private string outputFolder = "";
    [ObservableProperty] private bool byIdFolder;
    [ObservableProperty] private bool sortAll;
    [ObservableProperty] private bool canSort;       // Parse 成功後才 true（Sort 鈕 enabled + 變橘）
    [ObservableProperty] private bool isSorting;

    // IP 的日期資料夾為 yyyyMMdd（對齊 legacy frmSortDefect / MainProc），命令也用此格式。
    private string DateStr => (SelectedDate ?? DateTimeOffset.Now).ToString("yyyyMMdd");

    [RelayCommand]
    private async Task Parse()
    {
        Folders.Clear();
        CanSort = false;
        var ip = _svc.Connection.Ip;
        if (!ip.IsConnected) { Log("❌ IP 未連線，無法列出缺陷資料夾"); return; }

        Log($"---- Parse ---- 向 IP 查詢 {DateStr} 的缺陷資料夾");
        try
        {
            var resp = await ip.ListDefectFoldersAsync(DateStr);
            if (resp?["status"]?.GetValue<string>() != "OK")
            {
                Log($"❌ IP 回應失敗：{resp?["error"]?.GetValue<string>() ?? "(無回應)"}");
                return;
            }
            var arr = resp["folders"] as JsonArray;
            if (arr != null)
                foreach (var f in arr)
                {
                    if (f is null) continue;
                    Folders.Add(new SortFolderItem
                    {
                        FolderName = f["folder_name"]?.GetValue<string>() ?? "",
                        PanelId = f["panel_id"]?.GetValue<string>() ?? "",
                        DefectCount = (int?)f["defect_count"] ?? 0,
                    });
                }
            CanSort = true;   // Parse 成功（即使 0 筆，仍允許嘗試 Sort）
            Log($"找到 {Folders.Count} 個缺陷資料夾");
            if (Folders.Count == 0) Log("（IP output 中該日期無缺陷結果）");
        }
        catch (Exception ex) { Log($"❌ Parse 失敗：{ex.Message}"); }
    }

    [RelayCommand]
    private async Task Sort()
    {
        if (IsSorting) return;
        var ip = _svc.Connection.Ip;
        if (!ip.IsConnected) { Log("❌ IP 未連線，無法歸檔"); return; }

        var subdir = string.IsNullOrWhiteSpace(OutputFolder) ? "sorted" : OutputFolder.Trim();
        var byId = ByIdFolder;
        var picked = (SortAll ? Folders : Folders.Where(f => f.Sort))
            .Select(f => f.FolderName).ToArray();
        if (picked.Length == 0) { Log("⚠ 未選任何資料夾（勾選或勾 SortAll）"); return; }

        IsSorting = true;
        Log($"---- Start Sort Defect ---- 命令 IP 歸檔 {picked.Length} 個 panel → output/{subdir}"
            + (byId ? "（By ID 分組）" : ""));
        try
        {
            var resp = await ip.SortDefectsAsync(DateStr, subdir, byId, picked);
            if (resp?["status"]?.GetValue<string>() != "OK")
            {
                Log($"❌ IP 回應失敗：{resp?["error"]?.GetValue<string>() ?? "(無回應)"}");
                return;
            }
            if (resp["results"] is JsonArray results)
                foreach (var r in results)
                {
                    if (r is null) continue;
                    var folder = r["folder"]?.GetValue<string>() ?? "?";
                    var copied = (int?)r["copied"] ?? 0;
                    var msg = r["message"]?.GetValue<string>();
                    Log($"  {folder} → 複製 {copied} 檔" + (string.IsNullOrEmpty(msg) ? "" : $"（{msg}）"));
                }
            var total = (int?)resp["total"] ?? 0;
            var dir = resp["output_dir"]?.GetValue<string>() ?? subdir;
            Log($"---- End Sort Defect ---- 共 {total} 檔 → {dir}");
        }
        catch (Exception ex) { Log($"❌ Sort 失敗：{ex.Message}"); }
        finally { IsSorting = false; }
    }

    // ========================================================================
    // 第二層：小圖人工分類（雙擊資料夾進入 → 縮圖牆 + 鍵盤 T/P 快速分類 → 存回 IP）
    // ========================================================================
    private readonly List<PatchItem> _all = new();          // 全部小圖（master）
    public ObservableCollection<PatchItem> Patches { get; } = new();  // 套 filter 後顯示於縮圖牆

    // 篩選：顯示全部 / 只顯示未分類（預設，標到一半回來可接著標）/ 只 TrueDefect / 只 Particle
    public string[] Filters { get; } = { "只顯示未分類", "顯示全部", "只顯示 TrueDefect", "只顯示 Particle" };

    [ObservableProperty] private bool inPatchView;          // true = 小圖檢視；false = 資料夾列表
    [ObservableProperty] private string currentFolder = "";
    [ObservableProperty] private PatchItem? selectedPatch;
    [ObservableProperty] private bool isLoadingPatches;
    [ObservableProperty] private string selectedFilter = "只顯示未分類";
    // 頂部統計（恆以 _all 全集計算）
    [ObservableProperty] private int totalCount;
    [ObservableProperty] private int classifiedCount;
    [ObservableProperty] private int trueDefectCount;
    [ObservableProperty] private int particleCount;
    [ObservableProperty] private int unclassifiedCount;

    private const int BatchSize = 50;   // GET_DEFECT_PATCHES_BATCH 每批張數

    partial void OnSelectedFilterChanged(string value) => ApplyFilter();

    /// <summary>雙擊資料夾 → 進入小圖檢視：LIST metadata → 批次取縮圖。</summary>
    public async Task OpenFolderAsync(string folder)
    {
        if (string.IsNullOrEmpty(folder)) return;
        var ip = _svc.Connection.Ip;
        if (!ip.IsConnected) { Log("❌ IP 未連線，無法載入小圖"); return; }

        CurrentFolder = folder;
        InPatchView = true;
        _all.Clear();
        Patches.Clear();
        SelectedPatch = null;
        IsLoadingPatches = true;
        Log($"---- 進入小圖檢視 ---- {folder}");
        try
        {
            var resp = await ip.ListDefectPatchesAsync(DateStr, folder);
            if (resp?["status"]?.GetValue<string>() != "OK")
            {
                Log($"❌ LIST_DEFECT_PATCHES 失敗：{resp?["error"]?.GetValue<string>() ?? "(無回應)"}");
                IsLoadingPatches = false;
                return;
            }
            if (resp["patches"] is JsonArray arr)
                foreach (var p in arr)
                {
                    if (p is null) continue;
                    _all.Add(new PatchItem
                    {
                        PatchId = p["patch_id"]?.GetValue<string>() ?? "",
                        RunIndex = (int?)p["run_index"] ?? 0,
                        RoiIndex = (int?)p["roi_index"] ?? 0,
                        GcX = (int?)p["GC_X"] ?? 0,
                        GcY = (int?)p["GC_Y"] ?? 0,
                        Size = (int?)p["Size"] ?? 0,
                        Type = p["Type"]?.GetValue<string>() ?? "",
                        CurrentClass = p["current_class"]?.GetValue<string>() ?? "未分類",
                    });
                }
            RecomputeStats();
            ApplyFilter();   // 套用 filter（預設只顯示未分類：續標上次未標的）
            Log($"  metadata {_all.Count} 張（已分類 {ClassifiedCount}，顯示 {Patches.Count}），載入縮圖中…");

            // 批次取縮圖（背景，全集），decode 後填回 UI。
            await LoadThumbnailsAsync(folder);
            Log($"  縮圖載入完成（{_all.Count(p => p.Thumb != null)}/{_all.Count}）");
        }
        catch (Exception ex) { Log($"❌ 載入小圖失敗：{ex.Message}"); }
        finally { IsLoadingPatches = false; }
    }

    private async Task LoadThumbnailsAsync(string folder)
    {
        var ip = _svc.Connection.Ip;
        for (int i = 0; i < _all.Count; i += BatchSize)
        {
            var batch = _all.Skip(i).Take(BatchSize).ToList();
            var ids = batch.Select(p => p.PatchId).ToList();
            JsonNode? resp;
            try { resp = await ip.GetDefectPatchesBatchAsync(DateStr, folder, ids); }
            catch (Exception ex) { Log($"  ⚠ 批次取縮圖失敗：{ex.Message}"); continue; }
            if (resp?["patches"] is not JsonArray got) continue;

            var byId = batch.ToDictionary(p => p.PatchId, p => p);
            foreach (var g in got)
            {
                if (g is null) continue;
                var pid = g["patch_id"]?.GetValue<string>();
                var b64 = g["png_base64"]?.GetValue<string>();
                if (pid is null || b64 is null || !byId.TryGetValue(pid, out var item)) continue;
                var bmp = DecodePng(b64);
                if (bmp != null) PostToUi(() => item.Thumb = bmp);
            }
        }
    }

    private static Bitmap? DecodePng(string base64)
    {
        try { return new Bitmap(new MemoryStream(Convert.FromBase64String(base64))); }
        catch { return null; }   // headless（無 render backend）或壞資料 → 跳過
    }

    [RelayCommand]
    private void BackToFolders() => InPatchView = false;

    private bool PassFilter(PatchItem p) => SelectedFilter switch
    {
        "只顯示未分類"        => !p.IsClassified,
        "只顯示 TrueDefect"   => p.CurrentClass == "TrueDefect",
        "只顯示 Particle"     => p.CurrentClass == "Particle",
        _                     => true,   // 顯示全部
    };

    // 依 filter 重建可見集合，盡量保留選取（不在則選同位置/第一張）。
    private void ApplyFilter()
    {
        var keep = SelectedPatch;
        Patches.Clear();
        foreach (var p in _all) if (PassFilter(p)) Patches.Add(p);
        if (keep != null && Patches.Contains(keep)) SelectedPatch = keep;
        else SelectedPatch = Patches.Count > 0 ? Patches[0] : null;
    }

    // 縮圖下方按鈕：標某張（不自動跳）。
    [RelayCommand] private void MarkTrue(PatchItem? p) => Classify(p, "TrueDefect", advance: false);
    [RelayCommand] private void MarkParticle(PatchItem? p) => Classify(p, "Particle", advance: false);

    /// <summary>分類選中的小圖並自動跳下一張。鍵盤 T/P 呼叫。</summary>
    public void ClassifySelected(string klass) => Classify(SelectedPatch, klass, advance: true);

    // 統一分類：設類別 → 即時存回 IP（持久化）→ 更新統計 → 若被 filter 隱藏則移出可見集合。
    private void Classify(PatchItem? p, string klass, bool advance)
    {
        if (p is null) return;
        int idx = Patches.IndexOf(p);
        p.CurrentClass = klass;
        PersistOne(p);            // 即時存檔（不等 Sort）
        RecomputeStats();
        if (!PassFilter(p) && idx >= 0)
        {
            Patches.RemoveAt(idx);   // 例如「只顯示未分類」→ 標完即消失
            SelectedPatch = Patches.Count > 0 ? Patches[Math.Min(idx, Patches.Count - 1)] : null;
        }
        else if (advance) MoveSelection(1);
    }

    /// <summary>←→ 切換選中小圖。</summary>
    public void MoveSelection(int delta)
    {
        if (Patches.Count == 0) return;
        int idx = SelectedPatch is null ? 0 : Patches.IndexOf(SelectedPatch);
        idx = Math.Clamp(idx + delta, 0, Patches.Count - 1);
        SelectedPatch = Patches[idx];
    }

    // 即時把單張分類存回 IP（fire-and-forget；IP 端命令循序處理、IpClient 內部加鎖序列化）。
    private void PersistOne(PatchItem p)
    {
        var ip = _svc.Connection.Ip;
        if (!ip.IsConnected) return;
        var folder = CurrentFolder;
        var one = new[] { (p.PatchId, p.CurrentClass) };
        _ = Task.Run(async () =>
        {
            try { await ip.SaveDefectClassificationAsync(DateStr, folder, one); }
            catch (Exception ex) { Log($"⚠ 即時存分類失敗（{p.PatchId}）：{ex.Message}"); }
        });
    }

    private void RecomputeStats()
    {
        TotalCount = _all.Count;
        TrueDefectCount = _all.Count(p => p.CurrentClass == "TrueDefect");
        ParticleCount = _all.Count(p => p.CurrentClass == "Particle");
        ClassifiedCount = TrueDefectCount + ParticleCount;
        UnclassifiedCount = TotalCount - ClassifiedCount;
    }

    /// <summary>整批重存（保險用；分類已即時存回，此鈕重送全部已分類確認歸檔）。</summary>
    [RelayCommand]
    private async Task SaveClassification()
    {
        var ip = _svc.Connection.Ip;
        if (!ip.IsConnected) { Log("❌ IP 未連線，無法存分類"); return; }
        var classified = _all.Where(p => p.IsClassified)
            .Select(p => (p.PatchId, p.CurrentClass)).ToList();
        if (classified.Count == 0) { Log("⚠ 尚無已分類的小圖"); return; }

        Log($"---- 存分類 ---- {CurrentFolder}：送 {classified.Count} 筆給 IP 歸檔");
        try
        {
            var resp = await ip.SaveDefectClassificationAsync(DateStr, CurrentFolder, classified);
            if (resp?["status"]?.GetValue<string>() != "OK")
            {
                Log($"❌ 存分類失敗：{resp?["error"]?.GetValue<string>() ?? "(無回應)"}");
                return;
            }
            var t = (int?)resp["TrueDefect"] ?? 0;
            var p = (int?)resp["Particle"] ?? 0;
            var dir = resp["output_dir"]?.GetValue<string>() ?? CurrentFolder;
            Log($"  歸檔完成：TrueDefect {t}、Particle {p} → {dir}/{{TrueDefect,Particle}}/");
        }
        catch (Exception ex) { Log($"❌ 存分類失敗：{ex.Message}"); }
    }

    private static void PostToUi(Action a)
    {
        try { var d = Dispatcher.UIThread; if (d.CheckAccess()) a(); else d.Post(a); }
        catch { a(); }
    }

    private void Log(string msg)
    {
        var line = $"{DateTime.Now:yyyy/MM/dd, HH:mm:ss} - {msg}";
        void Add() { LogLines.Add(line); if (LogLines.Count > 1000) LogLines.RemoveAt(0); }
        try
        {
            var d = Avalonia.Threading.Dispatcher.UIThread;
            if (d.CheckAccess()) Add(); else d.Post(Add);
        }
        catch { Add(); }   // headless（selftest）fallback
    }
}
