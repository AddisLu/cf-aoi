using System;
using System.Collections.ObjectModel;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
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

    private string DateStr => (SelectedDate ?? DateTimeOffset.Now).ToString("yyyy-MM-dd");

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
