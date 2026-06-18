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

        LoadTopology();   // 塊1：開機載入機台層宣告槽（config/array_topology.json，缺則回退 .example）
    }

    // ── 塊1：多 CCD 陣列「宣告」拓樸（topology 驅動，與下方「偵測到的相機」分開；綁定=#21）──
    // 約束②：宣告槽來自 array_topology.json，全部「已宣告·未綁」，未綁前不得標線上；
    //         絕不把 LIST_CAMERAS 列舉到的相機 merge 進任何槽（填槽=綁定=#21，本輪不做）。
    public ArrayTopologyModel Topology { get; private set; } = new();
    /// <summary>依運算單元分群的宣告槽（運算單元帶）。</summary>
    public ObservableCollection<ComputeUnitGroup> ComputeUnits { get; } = new();
    [ObservableProperty] private int declaredSlotCount;
    [ObservableProperty] private string topologyStatus = "";
    [ObservableProperty] private CcdSlotModel? selectedSlot;

    private void LoadTopology() => ApplyTopology(ArrayTopologyModel.Load());

    /// <summary>套用拓樸 → 依 compute_unit 分群建運算單元帶（selftest 也走此入口餵 fixture）。</summary>
    public void ApplyTopology(ArrayTopologyModel topo)
    {
        Topology = topo;
        ComputeUnits.Clear();
        foreach (var u in topo.ComputeUnits)
        {
            var g = new ComputeUnitGroup { Unit = u };
            foreach (var s in topo.Slots.Where(s => s.ComputeUnit == u.Id)) g.Slots.Add(s);
            ComputeUnits.Add(g);
        }
        // 防呆：宣告了 compute_unit 但 compute_units[] 沒列到的槽 → 歸「(未指派)」群，不靜默吞掉
        var orphan = topo.Slots.Where(s => topo.ComputeUnits.All(u => u.Id != s.ComputeUnit)).ToList();
        if (orphan.Count > 0)
        {
            var g = new ComputeUnitGroup { Unit = new ComputeUnitModel { Id = "(未指派運算單元)" } };
            foreach (var s in orphan) g.Slots.Add(s);
            ComputeUnits.Add(g);
        }
        DeclaredSlotCount = topo.Slots.Count;
        TopologyStatus = $"宣告 {DeclaredSlotCount} 槽 / {topo.ComputeUnits.Count} 運算單元 · 全部未綁（綁定動作 = #21）";
        RefreshUnitConnectivity();
    }

    // 連線(真)：Control 現只連單一 ActiveIpNode → 以該 node + IsIpConnected 套 UnitConnected 規則。
    // 結構上未假設永遠單台：未來多台 active 各自連線時，改成查 per-node 連線狀態即可（規則函式不變）。
    private void RefreshUnitConnectivity()
    {
        var connectedNode = _svc.Config.ActiveIpNode;
        bool ipUp = _svc.Connection.IsIpConnected;
        foreach (var g in ComputeUnits)
            g.IsConnected = ComputeUnitGroup.UnitConnected(g.Unit.Node, connectedNode, ipUp);
    }

    [RelayCommand] private void SelectSlot(CcdSlotModel? s) { if (s is not null) SelectedSlot = s; }

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

    // KPI：配置 = 拓樸「宣告」CCD 數（= DeclaredSlotCount，config 數，非 runtime 偵測）。
    // 連線/已綁定/待綁定/離線 = 「偵測到的相機」之狀態（runtime；未偵測前皆 0）。
    // 約束②：宣告的 37 槽不在這 4 個偵測 KPI 重複計（用上方黃點呈現），不假裝宣告槽=偵測狀態。
    [ObservableProperty] private int onlineCount;
    [ObservableProperty] private int boundCount;
    [ObservableProperty] private int unboundCount;
    [ObservableProperty] private int offlineCount;
    // 偵測側說明：強調與「宣告槽」分開、未對映（約束②，對映=綁定=#21）。
    [ObservableProperty] private string detectedCamerasNote = "尚未列舉（與上方宣告槽分開；對映到槽位 = #21）";

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
            OnlineCount     = Cameras.Count(c => c.Online);
            BoundCount      = BoundCameras.Count;
            UnboundCount    = UnboundCameras.Count;
            OfflineCount    = OfflineCameras.Count;          // 0（無 config 映射，不假造離線台）
            SelectedCamera  = Cameras.FirstOrDefault();
            // 約束②：列舉到的相機獨立呈現，未 merge 進任何宣告槽（對映=綁定=#21）。
            DetectedCamerasNote = $"偵測到 {Cameras.Count} 台 · 尚未對映到宣告槽（綁定 = #21）";
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

    // 調參效果確認（mean gray）：證明影像真的隨曝光/增益變，非只看回讀值。
    [ObservableProperty] private string meanGrayText = "mean gray：—（按「套用並驗證」抓幀）";
    private double _prevMean = -1;

    // GigE 機器層參數（open() 設的 PixelFormat/Auto/Trigger/ROI/封包）→ 讓使用者看得到。
    [ObservableProperty] private string machineParamsText = "（按「讀取機器層參數」顯示 PixelFormat/Trigger/ROI…）";

    [RelayCommand]
    private async Task ReadCamNodes()
    {
        CamStatus = "讀取機器層參數中…";
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(6));
            var n = await _svc.Connection.Grab.GetCamNodesAsync(cts.Token);
            if (n is null) { CamStatus = "ERR：讀取失敗（取像中?）"; return; }
            MachineParamsText =
                $"PixelFormat={n.PixelFormat}　ExposureAuto={n.ExposureAuto}　GainAuto={n.GainAuto}\n" +
                $"TriggerMode={n.TriggerMode}（Selector={n.TriggerSelector} / Source={n.TriggerSource}）\n" +
                $"ROI={n.Width}×{n.Height}　PacketSize={n.PacketSize}　GevSCPD={n.Scpd}";
            CamStatus = "已讀取機器層參數";
        }
        catch (Exception ex) { CamStatus = $"ERR：{ex.Message}"; }
    }

    [RelayCommand]
    private async Task VerifyCamParams()
    {
        CamStatus = "套用並抓幀中…";
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            var r = await _svc.Connection.Grab.TuneMeanAsync(CurCamId, ExposureUs, GainRaw, cts.Token);
            if (r is null) { CamStatus = "ERR：Grab 回應失敗（取像中無法調參預覽?）"; return; }
            ExposureActualText = $"實際：{r.ExposureUsActual:F1} µs";
            GainActualText     = $"實際：{r.GainRawActual} raw";
            MeanGrayText = _prevMean >= 0
                ? $"mean gray：{r.MeanGray:F1}（前次 {_prevMean:F1}，Δ {r.MeanGray - _prevMean:+0.0;-0.0}）"
                : $"mean gray：{r.MeanGray:F1}";
            _prevMean = r.MeanGray;
            CamStatus = $"已套用並抓幀 CCD{CurCamId:00}";
        }
        catch (Exception ex) { CamStatus = $"ERR：{ex.Message}"; }
    }

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
        else if (e.PropertyName == nameof(ConnectionManager.IsIpConnected))
            RefreshUnitConnectivity();   // 連線變化即時刷新各運算單元卡的燈（不影響負載估算）
    }
}

/// <summary>運算單元帶的一張卡：連線(真) + 處理 N 顆 CCD(真) + 負載%(估算容量投影)。</summary>
public sealed partial class ComputeUnitGroup : ObservableObject
{
    public ComputeUnitModel Unit { get; init; } = new();
    public ObservableCollection<CcdSlotModel> Slots { get; } = new();
    public string Title => Unit.Id;
    public string SubTitle => string.IsNullOrEmpty(Unit.Node) ? Unit.Role : $"node={Unit.Node} · {Unit.Role}";

    // ① 處理 N（真）：= 該 unit 分到的宣告槽數（從拓樸算，不寫死）。
    public int SlotCount => Slots.Count;
    public string SlotCountText => $"處理 {SlotCount} 顆 CCD";

    // ② 連線（真）：由 VM 依連線規則設定；未連顯灰、不假綠；連線變化即時刷新。
    [ObservableProperty] private bool isConnected;

    /// <summary>
    /// 連線規則（純函式，可測；不假設永遠單台）：本單元 node == 該連線 node 且該連線已連 → 連線。
    /// 現況 Control 只連單一 ActiveIpNode，故 VM 傳 active 進來；未來多台 active 時改為 per-node 查詢即可。
    /// </summary>
    public static bool UnitConnected(string unitNode, string connectedNode, bool nodeUp)
        => !string.IsNullOrEmpty(unitNode) && unitNode == connectedNode && nodeUp;

    // ③ 負載%（估算容量投影，與連線無關）：§2 投影——7.4ms/張實測、每 CCD ~30 張(=1110/37)、30s 節拍；
    //    37 CCD 吞吐未實機跑滿。代表「滿載時約多少」，非當下即時量測。
    public const double MsPerImage = 7.4;     // 實測
    public const int    ImagesPerCcd = 30;    // 投影 = 1110/37
    public const double TactMs = 30000;       // 30s 節拍
    public double EstPanelMs => SlotCount * ImagesPerCcd * MsPerImage;
    public int EstLoadPct   => (int)Math.Round(EstPanelMs / TactMs * 100);
    public int EstMarginPct => Math.Max(0, 100 - EstLoadPct);
    public string LoadText   => $"負載 ~{EstLoadPct}% · 餘裕 ~{EstMarginPct}%（估算）";
    public string LoadDetail =>
        $"估算容量投影（非即時量測）：{SlotCount}×{ImagesPerCcd}張×{MsPerImage}ms ≈ {EstPanelMs / 1000:F1}s / {TactMs / 1000:F0}s 節拍。7.4ms/張實測，37 CCD 吞吐未實機跑滿。";
}
