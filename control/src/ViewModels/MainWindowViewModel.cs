using System;
using System.Collections.ObjectModel;
using System.Threading.Tasks;
using Avalonia.Threading;
using CfAoiControl.Controllers;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>
/// 主控台，1:1 對應 legacy frmCfAoi：Status 標籤群 / CF 按鈕區 / 系統 log / Error·Warning 分頁 /
/// 右側 Config·Recipe·Share·RecipeSetting。離線(offline-tcp)無對應的 Grab/Align 鈕停用（決策2）。
/// </summary>
public partial class MainWindowViewModel : ViewModelBase
{
    private readonly AppServices _svc;
    private readonly ConnectionManager _conn;
    private readonly LogService _log;

    public AppServices Services => _svc;
    public Step1ViewModel Step1 { get; }
    public SystemConfigModel Config => _svc.Config;

    // ===== Status 群（每秒更新；對應 lblCurXxx）=====
    [ObservableProperty] private string curCommand = "CF_READY";
    [ObservableProperty] private string curCamStatus = "Idle";
    [ObservableProperty] private string curRecipe = "";
    [ObservableProperty] private string curPanelId = "";
    [ObservableProperty] private string curDetectMode = "OFF-LINE";
    [ObservableProperty] private string aiModel = "—";
    [ObservableProperty] private string offlineFolder = "";
    [ObservableProperty] private bool isIpConnected;

    // ===== Reserve 按鈕區 =====
    [ObservableProperty] private bool showAdvanced;   // Ctrl+F 切換（沿用舊版隱藏慣例）

    // ===== Recipe 區 =====
    public ObservableCollection<string> RecipeNames { get; } = new();
    [ObservableProperty] private string selectedRecipe = "DEFAULT";

    // ===== Log 三區（對應 rtbSys / rtbError / rtbWarning）=====
    public ObservableCollection<LogEntry> SysLog { get; } = new();
    public ObservableCollection<LogEntry> ErrorLog { get; } = new();
    public ObservableCollection<LogEntry> WarningLog { get; } = new();

    public MainWindowViewModel() : this(AppServices.DesignTime()) { }

    public MainWindowViewModel(AppServices svc)
    {
        _svc = svc;
        _conn = svc.Connection;
        _log = svc.Log;
        Step1 = new Step1ViewModel(svc);

        OfflineFolder = svc.Config.Paths.ImageDir;
        RefreshRecipeNames();

        // log 路由到三區（UI thread）
        _log.Logged += e => Dispatcher.UIThread.Post(() =>
        {
            Append(SysLog, e);
            if (e.Level == LogLevel.Error) Append(ErrorLog, e);
            else if (e.Level == LogLevel.Warning) Append(WarningLog, e);
        });

        _conn.Start(svc.Config);

        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        timer.Tick += (_, _) =>
        {
            IsIpConnected = _conn.IsIpConnected;
            CurCamStatus = _conn.IsIpConnected ? "IP Connected" : "IP Disconnected";
        };
        timer.Start();
    }

    private static void Append(ObservableCollection<LogEntry> col, LogEntry e)
    {
        col.Add(e);
        if (col.Count > 500) col.RemoveAt(0);
    }

    // ===== CF 命令（offline 有意義者接後端；Grab/Align/Stop 停用，見 XAML IsEnabled）=====
    [RelayCommand]
    private async Task CfLoadRecipe()
    {
        CurCommand = "CF_LOAD_RECIPE";
        CurRecipe = SelectedRecipe;
        try
        {
            var ensure = await _svc.Recipes.EnsureRecipeExistsAsync(SelectedRecipe);
            var xml = _svc.Recipes.ToXmlString(ensure.Recipe);
            var resp = await _conn.Ip.LoadRecipeAsync(ensure.RecipeXmlPath, CurPanelId, xml);
            if (resp?["status"]?.GetValue<string>() == "OK") _log.Info($"LOAD_RECIPE {SelectedRecipe} OK");
            else _log.Error($"LOAD_RECIPE 失敗：{resp?.ToJsonString()}");
        }
        catch (Exception ex) { _log.Error($"LOAD_RECIPE: {ex.Message}"); }
    }

    [RelayCommand]
    private async Task CfGetResult()
    {
        CurCommand = "CF_GET_RESULT";
        try
        {
            var s = await _conn.Ip.GetStatusAsync();
            _log.Info($"GET_STATUS: {s?.ToJsonString()}");
        }
        catch (Exception ex) { _log.Error($"GET_RESULT: {ex.Message}"); }
    }

    [RelayCommand] private void CfSaveConfig() => _log.Info("Save Config（appsettings 由系統設定頁管理）");

    [RelayCommand]
    private async Task CfSaveRecipe()
    {
        try
        {
            var ensure = await _svc.Recipes.EnsureRecipeExistsAsync(SelectedRecipe);
            await _svc.Recipes.SaveAsync(SelectedRecipe, ensure.Recipe);
            _log.Info($"Recipe {SelectedRecipe} 已存");
        }
        catch (Exception ex) { _log.Error($"SaveRecipe: {ex.Message}"); }
    }

    [RelayCommand] private void Refresh()
    {
        CurCommand = "REFRESH";
        RefreshRecipeNames();
        _log.Info("已重整配方清單");
    }

    [RelayCommand] private void ToggleAdvanced() => ShowAdvanced = !ShowAdvanced;

    private void RefreshRecipeNames()
    {
        RecipeNames.Clear();
        RecipeNames.Add("DEFAULT");
        try
        {
            var root = RecipeService.ExpandPath(_svc.Config.Paths.RecipeDir);
            if (System.IO.Directory.Exists(root))
                foreach (var d in System.IO.Directory.GetDirectories(root))
                {
                    var n = System.IO.Path.GetFileName(d);
                    if (!RecipeNames.Contains(n)) RecipeNames.Add(n);
                }
        }
        catch { }
        if (!RecipeNames.Contains(SelectedRecipe)) SelectedRecipe = "DEFAULT";
    }
}
