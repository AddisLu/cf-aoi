using System;
using System.ComponentModel;
using System.Threading;
using System.Threading.Tasks;
using CfAoiControl.Controllers;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>系統設定（連線設定 + 相機參數）。連線設定來自 appsettings.json；相機參數透過 GrabClient SET/GET_CAM_PARAMS。</summary>
public partial class SystemSettingsViewModel : ViewModelBase
{
    private readonly AppServices _svc;

    public SystemSettingsViewModel(AppServices svc)
    {
        _svc = svc;
        var ip = svc.Config.ActiveIp;
        IpHost = ip?.Host ?? "127.0.0.1";
        IpPort = ip?.Port ?? 8200;
        RecipeDir = svc.Config.Paths.RecipeDir;
        OutputDir = svc.Config.Paths.OutputDir;
        ImageDir = svc.Config.Paths.ImageDir;
        UpstreamPort = svc.Config.UpstreamServer.ListenPort;

        // 轉發 ConnectionManager.IsGrabConnected 的 PropertyChanged → 本 VM 的 IsGrabConnected
        svc.Connection.PropertyChanged += OnConnectionPropertyChanged;
    }

    // ── 連線設定 tab ──────────────────────────────────────────────
    [ObservableProperty] private string ipHost = "";
    [ObservableProperty] private int ipPort;
    [ObservableProperty] private string recipeDir = "";
    [ObservableProperty] private string outputDir = "";
    [ObservableProperty] private string imageDir = "";
    [ObservableProperty] private int upstreamPort;
    [ObservableProperty] private string testResult = "";

    [RelayCommand]
    private async Task TestConnection()
    {
        TestResult = "連線中…";
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            await _svc.Connection.Ip.ConnectAsync(IpHost, IpPort, cts.Token);
            var health = await _svc.Connection.Ip.CheckHealthAsync(cts.Token);
            TestResult = $"✓ IP OK：{health?.ToJsonString()}";
        }
        catch (Exception ex) { TestResult = $"❌ {ex.Message}"; }
    }

    // ── 相機 tab ─────────────────────────────────────────────────
    // Gap #2 ExposureTimeAbs 2~10000µs；GainRaw int 256~2047（Stage 0 實機確認）

    /// <summary>轉發 ConnectionManager.IsGrabConnected；Apply 按鈕 IsEnabled 綁定到此。</summary>
    public bool IsGrabConnected => _svc.Connection.IsGrabConnected;

    [ObservableProperty] private double exposureUs = 70.0;
    [ObservableProperty] private int gainRaw = 256;

    // read-back actual（SET 後填入；GET 後填入）
    [ObservableProperty] private string exposureActualText = "實際：尚未讀取";
    [ObservableProperty] private string gainActualText = "實際：尚未讀取";

    [ObservableProperty] private string camStatus = "";

    [RelayCommand]
    private async Task ApplyCamParams()
    {
        CamStatus = "套用中…";
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var r = await _svc.Connection.Grab.SetCamParamsAsync(0, ExposureUs, GainRaw, cts.Token);
            if (r is null) { CamStatus = "ERR：Grab 回應失敗"; return; }
            ExposureActualText = $"實際：{r.ExposureUsActual:F1} µs";
            GainActualText     = $"實際：{r.GainRawActual} raw";
            CamStatus = "已套用";
        }
        catch (Exception ex) { CamStatus = $"ERR：{ex.Message}"; }
    }

    [RelayCommand]
    private async Task RefreshCamParams()
    {
        CamStatus = "讀取中…";
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var r = await _svc.Connection.Grab.GetCamParamsAsync(0, cts.Token);
            if (r is null) { CamStatus = "ERR：Grab 回應失敗"; return; }
            ExposureUs = r.ExposureUsActual;
            GainRaw    = r.GainRawActual;
            ExposureActualText = $"實際：{r.ExposureUsActual:F1} µs";
            GainActualText     = $"實際：{r.GainRawActual} raw";
            CamStatus = "已讀取";
        }
        catch (Exception ex) { CamStatus = $"ERR：{ex.Message}"; }
    }

    private void OnConnectionPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(ConnectionManager.IsGrabConnected))
            OnPropertyChanged(nameof(IsGrabConnected));
    }
}
