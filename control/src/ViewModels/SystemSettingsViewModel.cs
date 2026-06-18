using System;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Linq;
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

    // ── 相機 tab：相機陣列總覽 ────────────────────────────────────
    // 資料由 LIST_CAMERAS 真實列舉驅動（不 hardcode）。現只 1 台 → 顯示 1 台。

    /// <summary>全部相機（實體陣列一字排開用）。</summary>
    public ObservableCollection<CameraInfoModel> Cameras { get; } = new();
    /// <summary>分群：待綁定（置頂）/ 已綁定 / 離線。</summary>
    public ObservableCollection<CameraInfoModel> UnboundCameras { get; } = new();
    public ObservableCollection<CameraInfoModel> BoundCameras   { get; } = new();
    public ObservableCollection<CameraInfoModel> OfflineCameras { get; } = new();

    // KPI：配置 = 列舉數（無 config↔CCD 映射前 = 偵測數，映射 = Gap #21）；離線 = 0（無映射來源）。
    [ObservableProperty] private int configuredCount;
    [ObservableProperty] private int onlineCount;
    [ObservableProperty] private int boundCount;
    [ObservableProperty] private int unboundCount;
    [ObservableProperty] private int offlineCount;

    [ObservableProperty] private CameraInfoModel? selectedCamera;
    public bool HasSelection => SelectedCamera is not null;
    private int CurCamId => SelectedCamera?.CamId ?? 0;   // per-camera 操作的 cam_id

    partial void OnSelectedCameraChanged(CameraInfoModel? value)
    {
        OnPropertyChanged(nameof(HasSelection));
        // 選中相機 → 拉該台目前曝光/增益填明細（連線時才打）
        if (value is not null && IsGrabConnected) _ = RefreshCamParams();
    }

    /// <summary>LIST_CAMERAS 真實列舉 → 填表 + 分群 + 算 KPI + 預選第一台。</summary>
    [RelayCommand]
    private async Task LoadCameras()
    {
        CamStatus = "列舉中…";
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var list = await _svc.Connection.Grab.ListCamerasAsync(cts.Token);
            if (list is null) { CamStatus = "ERR：Grab 回應失敗"; return; }

            Cameras.Clear(); UnboundCameras.Clear(); BoundCameras.Clear(); OfflineCameras.Clear();
            foreach (var c in list)
            {
                Cameras.Add(c);
                switch (c.Status)
                {
                    case CamStatusKind.Bound:   BoundCameras.Add(c);   break;
                    case CamStatusKind.Unbound: UnboundCameras.Add(c); break;
                    default:                    OfflineCameras.Add(c); break;
                }
            }
            ConfiguredCount = Cameras.Count;                 // = 偵測數（配置映射 = #21）
            OnlineCount     = Cameras.Count(c => c.Online);
            BoundCount      = BoundCameras.Count;
            UnboundCount    = UnboundCameras.Count;
            OfflineCount    = OfflineCameras.Count;          // 0（無 config 映射，不假造離線台）
            SelectedCamera  = Cameras.FirstOrDefault();
            CamStatus = $"列舉到 {Cameras.Count} 台";
        }
        catch (Exception ex) { CamStatus = $"ERR：{ex.Message}"; }
    }

    /// <summary>點選陣列/清單某台 → 設為選中（MVVM,無 code-behind）。</summary>
    [RelayCommand]
    private void SelectCamera(CameraInfoModel? c)
    {
        if (c is not null) SelectedCamera = c;
    }

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
            var r = await _svc.Connection.Grab.SetCamParamsAsync(CurCamId, ExposureUs, GainRaw, cts.Token);
            if (r is null) { CamStatus = "ERR：Grab 回應失敗"; return; }
            ExposureActualText = $"實際：{r.ExposureUsActual:F1} µs";
            GainActualText     = $"實際：{r.GainRawActual} raw";
            CamStatus = $"已儲存 CCD{CurCamId:00}";
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
            var r = await _svc.Connection.Grab.GetCamParamsAsync(CurCamId, cts.Token);
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
