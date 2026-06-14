using System;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>DataGrid 一列：Sort(可勾) + FolderName(唯讀)。對應 legacy SortFolderName。</summary>
public sealed partial class SortFolderItem : ObservableObject
{
    [ObservableProperty] private bool sort;
    public string FolderName { get; init; } = "";
}

/// <summary>
/// 1:1 對應 legacy frmSortDefect：選日期→Parse 列當日資料夾→勾選/全選→Sort 複製 Defect 檔到輸出夾
/// （By ID 則依前綴建子夾）→ rtbLog 時間戳記進度。跨平台：路徑全走 Path.Combine。
/// </summary>
public partial class DefectSortViewModel : ViewModelBase
{
    private readonly AppServices _svc;
    private string _parseRoot = "";   // Parse 當下實際掃描的根目錄

    public DefectSortViewModel(AppServices svc)
    {
        _svc = svc;
        // 來源 = IP 輸出目錄；輸出 = 其下 sorted 子夾（可改）
        var outDir = RecipeService.ExpandPath(_svc.Config.Paths.OutputDir);
        OutputFolder = Path.Combine(outDir, "sorted");
    }

    public ObservableCollection<SortFolderItem> Folders { get; } = new();
    public ObservableCollection<string> LogLines { get; } = new();

    [ObservableProperty] private DateTimeOffset? selectedDate = DateTimeOffset.Now;
    [ObservableProperty] private string outputFolder = "";
    [ObservableProperty] private bool byIdFolder;
    [ObservableProperty] private bool sortAll;
    [ObservableProperty] private bool canSort;       // Parse 後才 true（Sort 鈕 enabled + 變橘）
    [ObservableProperty] private bool isSorting;

    [RelayCommand]
    private void Parse()
    {
        Folders.Clear();
        var srcRoot = RecipeService.ExpandPath(_svc.Config.Paths.OutputDir);
        // 優先掃 {Output}/{yyyyMMdd}，沒有則掃 {Output} 本身
        var dateStr = (SelectedDate ?? DateTimeOffset.Now).ToString("yyyyMMdd");
        var dated = Path.Combine(srcRoot, dateStr);
        _parseRoot = Directory.Exists(dated) ? dated : srcRoot;

        Log($"---- Parse ---- 來源={_parseRoot}");
        if (!Directory.Exists(_parseRoot)) { Log("來源目錄不存在"); CanSort = false; return; }

        var dirs = Directory.GetDirectories(_parseRoot).OrderBy(d => d).ToArray();
        foreach (var d in dirs) Folders.Add(new SortFolderItem { FolderName = Path.GetFileName(d) });
        CanSort = Folders.Count > 0;
        Log($"找到 {Folders.Count} 個資料夾");
        if (Folders.Count == 0) Log("（此目錄下無子資料夾；Sort 仍可用 SortAll 直接整理本層 *.png）");
        CanSort = true;   // 允許直接整理本層
    }

    [RelayCommand]
    private async Task Sort()
    {
        if (IsSorting) return;
        IsSorting = true;
        var dst = OutputFolder;
        var root = _parseRoot;
        var byId = ByIdFolder;
        var all = SortAll;
        var picked = Folders.Where(f => all || f.Sort).Select(f => f.FolderName).ToArray();

        Log("---- Start Sort Defect ----");
        try
        {
            await Task.Run(() =>
            {
                Directory.CreateDirectory(dst);
                int total = 0;
                // 若有子資料夾被選 → 逐資料夾複製；否則整理本層 *.png
                var sources = picked.Length > 0
                    ? picked.Select(n => Path.Combine(root, n))
                    : new[] { root };

                foreach (var srcDir in sources)
                {
                    if (!Directory.Exists(srcDir)) continue;
                    int n = 0;
                    foreach (var file in EnumDefectFiles(srcDir))
                    {
                        var dstDir = byId ? Path.Combine(dst, IdPrefix(Path.GetFileName(file))) : dst;
                        Directory.CreateDirectory(dstDir);
                        File.Copy(file, Path.Combine(dstDir, Path.GetFileName(file)), overwrite: true);
                        n++;
                    }
                    total += n;
                    Log($"  {Path.GetFileName(srcDir)} → 複製 {n} 檔");
                }
                Log($"---- End Sort Defect ---- 共 {total} 檔 → {dst}");
            });
        }
        catch (Exception ex) { Log($"❌ Sort 失敗：{ex.Message}"); }
        finally { IsSorting = false; }
    }

    // 缺陷影像檔（patches/*.png、Defect*.* 等），遞迴抓
    private static System.Collections.Generic.IEnumerable<string> EnumDefectFiles(string dir)
        => Directory.EnumerateFiles(dir, "*.png", SearchOption.AllDirectories)
            .Concat(Directory.EnumerateFiles(dir, "Defect*.*", SearchOption.AllDirectories))
            .Distinct();

    // 依檔名前綴分組（取第一個 '_' 前），對應 legacy By ID Folder
    private static string IdPrefix(string fileName)
    {
        var stem = Path.GetFileNameWithoutExtension(fileName);
        var i = stem.IndexOf('_');
        return i > 0 ? stem[..i] : stem;
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
