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
    public ZoneParamEditorViewModel ZoneEditor { get; }
    public DefectSortViewModel DefectSort { get; }
    public SystemSettingsViewModel SysSettings { get; }
    public SingleCcdSetupViewModel SingleCcdSetup { get; }   // 塊3：單 CCD 設定整合頁
    public SystemConfigModel Config => _svc.Config;
    public RecipeStore Store => _svc.RecipeStore;   // 配方共用來源（主視窗 Recipe 區預覽）
    public ShareSettingModel ShareSetting => _svc.Config.ShareSetting;   // 全域旗標（ShareSetting 面板）

    // ShareSetting 面板「儲存」→ 寫回 appsettings.json（只改 ShareSetting 節點）
    [RelayCommand]
    private void SaveShareSetting()
    {
        try { ConfigLoader.SaveShareSetting(_svc.Config.ShareSetting); _log.Info("ShareSetting 已存 appsettings.json"); }
        catch (Exception ex) { _log.Error($"存 ShareSetting 失敗：{ex.Message}"); }
    }

    // RecipeSetting 面板「儲存」→ 存 per-recipe RecipeSetting.xml
    [RelayCommand]
    private void SaveRecipeSetting()
    {
        try { Store.SaveRecipeSetting(); }
        catch (Exception ex) { _log.Error($"存 RecipeSetting 失敗：{ex.Message}"); }
    }

    // ===== Status 群（每秒更新；對應 lblCurXxx）=====
    [ObservableProperty] private string curCommand = "CF_READY";
    [ObservableProperty] private string curCamStatus = "Idle";
    [ObservableProperty] private string curRecipe = "";
    [ObservableProperty] private string curPanelId = "";
    [ObservableProperty] private string curDetectMode = "OFF-LINE";
    [ObservableProperty] private string aiModel = "—";
    [ObservableProperty] private string offlineFolder = "";
    [ObservableProperty] private bool isIpConnected;
    [ObservableProperty] private bool isGrabConnected;
    [ObservableProperty] private bool isUpstreamConnected;

    // ===== Reserve 按鈕區 =====
    [ObservableProperty] private bool showAdvanced;   // Ctrl+F 切換（沿用舊版隱藏慣例）

    // ===== 導覽：單一視窗側欄切換 5 畫面（純 UI 狀態；對應設計稿 screen）=====
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsDashboard))]
    [NotifyPropertyChangedFor(nameof(IsStep1))]
    [NotifyPropertyChangedFor(nameof(IsZone))]
    [NotifyPropertyChangedFor(nameof(IsSort))]
    [NotifyPropertyChangedFor(nameof(IsSettings))]
    [NotifyPropertyChangedFor(nameof(IsSingleCcd))]
    private string currentScreen = "dashboard";

    public bool IsDashboard => CurrentScreen == "dashboard";
    public bool IsStep1     => CurrentScreen == "step1";
    public bool IsZone      => CurrentScreen == "zone";
    public bool IsSort      => CurrentScreen == "sort";
    public bool IsSettings  => CurrentScreen == "settings";
    public bool IsSingleCcd => CurrentScreen == "singleccd";   // 塊3：單 CCD 設定整合頁

    [RelayCommand] private void Navigate(string? screen)
    { if (!string.IsNullOrEmpty(screen)) CurrentScreen = screen!; }

    // 系統 log 分頁（系統 / 錯誤 / 警告）— 純 UI 狀態
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsLogSys))]
    [NotifyPropertyChangedFor(nameof(IsLogError))]
    [NotifyPropertyChangedFor(nameof(IsLogWarn))]
    private string logTab = "sys";
    public bool IsLogSys   => LogTab == "sys";
    public bool IsLogError => LogTab == "error";
    public bool IsLogWarn  => LogTab == "warn";
    [RelayCommand] private void SetLogTab(string? t) { if (!string.IsNullOrEmpty(t)) LogTab = t!; }

    // ===== Recipe 區：用共用 Store（下拉 + PrimaryZone 預覽）=====

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
        ZoneEditor = new ZoneParamEditorViewModel(svc);
        DefectSort = new DefectSortViewModel(svc);
        SysSettings = new SystemSettingsViewModel(svc);
        SingleCcdSetup = new SingleCcdSetupViewModel(svc);

        // master→detail：宣告陣列點 CCD 槽（SystemSettings.SelectedSlot）→ 進單 CCD 設定整合頁。
        // 無帶參導覽 → 用屬性訂閱；不改既有 NavigateCommand。
        SysSettings.PropertyChanged += (_, e) =>
        {
            if (e.PropertyName == nameof(SystemSettingsViewModel.SelectedSlot) && SysSettings.SelectedSlot is { } slot)
            {
                SingleCcdSetup.LoadSlot(slot);
                CurrentScreen = "singleccd";
            }
        };

        OfflineFolder = svc.Config.Paths.ImageDir;
        Store.RecipeReloaded += () => CurRecipe = Store.SelectedRecipe;

        // log 路由到三區（UI thread）
        _log.Logged += e => Dispatcher.UIThread.Post(() =>
        {
            Append(SysLog, e);
            if (e.Level == LogLevel.Error) Append(ErrorLog, e);
            else if (e.Level == LogLevel.Warning) Append(WarningLog, e);
        });

        _conn.Start(svc.Config, _log);

        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        timer.Tick += (_, _) =>
        {
            IsIpConnected = _conn.IsIpConnected;
            IsGrabConnected = _conn.IsGrabConnected;
            IsUpstreamConnected = _conn.IsUpstreamConnected;
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
        CurRecipe = Store.SelectedRecipe;
        try
        {
            var xml = _svc.Recipes.ToXmlString(Store.Recipe);   // 共用配方（含最新值）
            var resp = await _conn.Ip.LoadRecipeAsync(Store.SelectedRecipe, CurPanelId, xml);
            if (resp?["status"]?.GetValue<string>() == "OK") _log.Info($"LOAD_RECIPE {Store.SelectedRecipe} OK");
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
        try { await Store.SaveAsync(); _log.Info($"Recipe {Store.SelectedRecipe} 已存"); }
        catch (Exception ex) { _log.Error($"SaveRecipe: {ex.Message}"); }
    }

    [RelayCommand] private void Refresh()
    {
        CurCommand = "REFRESH";
        Store.RefreshNames();
        _log.Info("已重整配方清單");
    }

    [RelayCommand] private void ToggleAdvanced() => ShowAdvanced = !ShowAdvanced;
}
