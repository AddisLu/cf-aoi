using System;
using System.Collections.ObjectModel;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>遠端檔案瀏覽器一項（IP 機磁碟的目錄或影像）。</summary>
public sealed class RemoteEntry
{
    public string Name { get; init; } = "";
    public bool IsDir { get; init; }
    public long Size { get; init; }
    public string Path { get; init; } = "";
    public string Display => IsDir ? $"📁  {Name}" : $"🖼  {Name}   ({Size / 1024} KB)";
}

/// <summary>
/// 遠端影像瀏覽器（modal 對話框 VM）：透過 IpClient.ListDirAsync 列 IP 機目錄/影像，
/// 像本機檔案視窗一樣導航；選影像回傳遠端路徑（影像不搬到 Mac，後續取縮小預覽 + IP 端全解析度檢測）。
/// 沿用 DefectSort 的遠端列舉 UX（IpClient SendCommandAsync）。
/// </summary>
public partial class RemoteImageBrowserViewModel : ObservableObject
{
    private readonly AppServices _svc;

    public ObservableCollection<RemoteEntry> Entries { get; } = new();
    [ObservableProperty] private string currentDir = "";
    [ObservableProperty] private RemoteEntry? selectedEntry;
    [ObservableProperty] private string status = "";

    public RemoteImageBrowserViewModel(AppServices svc, string startDir)
    {
        _svc = svc;
        CurrentDir = string.IsNullOrWhiteSpace(startDir) ? "." : startDir;
    }

    /// <summary>進入/重整目前路徑（CurrentDir）。「前往」按鈕用。</summary>
    [RelayCommand] private Task Go() => NavigateAsync(CurrentDir);

    public async Task NavigateAsync(string path)
    {
        var ip = _svc.Connection.Ip;
        if (!ip.IsConnected) { Status = "❌ IP 未連線"; return; }
        Status = "讀取中…";
        try
        {
            var resp = await ip.ListDirAsync(path);
            if (resp?["status"]?.GetValue<string>() != "OK")
            { Status = $"ERR：{resp?["error"]?.GetValue<string>() ?? resp?.ToJsonString()}"; return; }
            CurrentDir = resp["dir"]?.GetValue<string>() ?? path;
            Entries.Clear();
            if (resp["entries"] is JsonArray arr)
                foreach (var e in arr)
                    Entries.Add(new RemoteEntry
                    {
                        Name  = e?["name"]?.GetValue<string>() ?? "",
                        IsDir = (bool?)e?["is_dir"] ?? false,
                        Size  = (long?)e?["size"] ?? 0,
                        Path  = e?["path"]?.GetValue<string>() ?? "",
                    });
            Status = $"{CurrentDir} — {Entries.Count} 項";
        }
        catch (Exception ex) { Status = $"ERR：{ex.Message}"; }
    }

    /// <summary>雙擊：目錄→進入。回傳 true 表示「選了影像」（由 View 關閉對話框並回傳 Path）。</summary>
    public async Task<bool> OpenAsync(RemoteEntry e)
    {
        if (e.IsDir) { await NavigateAsync(e.Path); return false; }
        SelectedEntry = e;
        return true;
    }
}
