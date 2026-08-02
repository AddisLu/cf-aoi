using System;
using System.Collections.Generic;
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

    // ── 多 CCD 陣列：宣告拓樸 × 偵測相機的 join（Gap #21）─────────────────────
    // 約束②（宣告≠綁定）依然成立，但意義已變：
    //   grab 端 cam_map.json 落地後，LIST_CAMERAS 會回 bound/ccd_id = **真實綁定憑據**，
    //   據此 join 不是「假 merge」。底線不變：bound==false 的相機**永不**指派到任何槽。
    public ArrayTopologyModel Topology { get; private set; } = new();
    /// <summary>依運算單元分群的宣告槽（運算單元帶）。</summary>
    public ObservableCollection<ComputeUnitGroup> ComputeUnits { get; } = new();
    [ObservableProperty] private int declaredSlotCount;
    [ObservableProperty] private string topologyStatus = "";
    [ObservableProperty] private CcdSlotModel? selectedSlot;
    /// <summary>MAC 不符（相機插錯槽位）的告警文字；無告警時為空字串。</summary>
    [ObservableProperty] private string bindingWarning = "";

    private void LoadTopology() => ApplyTopology(ArrayTopologyModel.Load());

    /// <summary>套用拓樸 → 依 compute_unit 分群建運算單元帶（selftest 也走此入口餵 fixture）。</summary>
    public void ApplyTopology(ArrayTopologyModel topo)
    {
        Topology = topo;
        ComputeUnits.Clear();
        foreach (var u in topo.ComputeUnits)
        {
            var g = new ComputeUnitGroup { Unit = u };
            foreach (var s in topo.Slots.Where(s => s.ComputeUnit == u.Id)) g.Slots.Add(new SlotBinding(s));
            ComputeUnits.Add(g);
        }
        // 防呆：宣告了 compute_unit 但 compute_units[] 沒列到的槽 → 歸「(未指派)」群，不靜默吞掉
        var orphan = topo.Slots.Where(s => topo.ComputeUnits.All(u => u.Id != s.ComputeUnit)).ToList();
        if (orphan.Count > 0)
        {
            var g = new ComputeUnitGroup { Unit = new ComputeUnitModel { Id = "(未指派運算單元)" } };
            foreach (var s in orphan) g.Slots.Add(new SlotBinding(s));
            ComputeUnits.Add(g);
        }
        DeclaredSlotCount = topo.Slots.Count;
        RefreshUnitConnectivity();
        RecomputeBindings();   // 拓樸換了 → 重算 join（此時可能還沒列舉相機，全部未綁）
    }

    /// <summary>
    /// 宣告槽 × 偵測相機的 join（Gap #21）。ApplyTopology 與 LoadCameras 之後都必須呼叫。
    /// 規則：
    ///   1. 只有 <c>Bound==true</c> 的相機才可能對到槽（未綁的絕不指派 → 約束②底線）。
    ///   2. 對應鍵是相機的 <c>CcdId</c>（來自 grab cam_map.json），不是 cam_id。
    ///   3. expected_mac 有填值時做交叉檢查：不符 = 相機插錯槽位 → 告警（不阻擋，只標紅）。
    ///   4. 「離線」只算「expected_mac 有值但沒相機對到」的槽 —— 該就位卻沒出現，指向明確；
    ///      expected_mac 為 null 的槽只是「還沒宣告要哪台」，不算離線（開發期不吵）。
    /// </summary>
    private void RecomputeBindings()
    {
        var bound = Cameras.Where(c => c.Bound && !string.IsNullOrEmpty(c.CcdId)).ToList();
        int boundSlots = 0, offlineSlots = 0;
        var mismatches = new List<string>();

        foreach (var g in ComputeUnits)
            foreach (var sb in g.Slots)
            {
                var cam = bound.FirstOrDefault(c => c.CcdId == sb.Slot.CcdId);
                sb.Camera = cam;

                if (cam is not null)
                {
                    boundSlots++;
                    // 交叉檢查：宣告的 expected_mac vs 實際就位相機的 MAC（正規化後比對）
                    if (sb.Slot.HasExpectedMac && !MacUtil.Same(sb.Slot.ExpectedMac, cam.Mac))
                        mismatches.Add($"{sb.Slot.CcdId}（預期 {sb.Slot.ExpectedMacDisplay} / 實際 {cam.MacDisplay}）");
                }
                else if (sb.Slot.HasExpectedMac)
                {
                    offlineSlots++;   // 該就位卻沒出現
                }
                sb.Refresh();
            }

        BoundCount   = boundSlots;
        OfflineCount = offlineSlots;
        UnboundCount = Cameras.Count(c => !c.Bound);

        BindingWarning = mismatches.Count == 0
            ? ""
            : $"⚠ MAC 不符（相機可能插錯槽位）：{string.Join("、", mismatches)}";

        TopologyStatus = DeclaredSlotCount == 0
            ? "未載入拓樸"
            : $"宣告 {DeclaredSlotCount} 槽 / {Topology.ComputeUnits.Count} 運算單元 · 已就位 {boundSlots}";
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

    // View 傳進來的是 SlotBinding（chip 的 DataContext）；下游 SingleCcdSetupViewModel.LoadSlot
    // 吃的仍是純宣告的 CcdSlotModel，故此處解包 .Slot（維持宣告模型純淨）。
    [RelayCommand] private void SelectSlot(object? s)
    {
        SelectedSlot = s switch
        {
            SlotBinding sb  => sb.Slot,
            CcdSlotModel cs => cs,     // 相容：直接傳宣告槽也接受（selftest 用）
            _               => SelectedSlot,
        };
    }

    // ── 連線設定 tab ──────────────────────────────────────────────
    [ObservableProperty] private string ipHost = "";
    [ObservableProperty] private int ipPort;
    [ObservableProperty] private string recipeDir = "";
    [ObservableProperty] private string outputDir = "";
    [ObservableProperty] private string imageDir = "";
    [ObservableProperty] private int upstreamPort;
    [ObservableProperty] private string testResult = "";

    /// <summary>測試 IP 連線。
    /// ⚠️ 已知限制（docs/code_review_20260802.md K7）：直接把**共用的** IpClient 連到輸入的位址，
    /// 測試成功後心跳/後續命令都會沿用該位址（appsettings 的 ActiveIp 未變），且本頁欄位改了不會存檔。</summary>
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
        OnPropertyChanged(nameof(CanBind));
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
            OnlineCount    = Cameras.Count(c => c.Online);
            SelectedCamera = Cameras.FirstOrDefault();

            // 已綁定/待綁定/離線三個 KPI 由 join 算（見 RecomputeBindings 的規則說明）：
            //   已綁定 = 有相機就位的**槽數**（非「有固定 IP 的相機數」——那是舊版的語意錯誤）
            //   待綁定 = 偵測到但不在 cam_map.json 的**相機數**
            //   離線   = expected_mac 有值但沒相機對到的**槽數**
            RecomputeBindings();

            int unboundCams = Cameras.Count(c => !c.Bound);
            DetectedCamerasNote = unboundCams == 0
                ? $"偵測到 {Cameras.Count} 台 · 全數已綁定到宣告槽"
                : $"偵測到 {Cameras.Count} 台 · 其中 {unboundCams} 台未列於 cam_map.json（不指派槽位）";
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

    // ── Gap #21 綁定動作：把選中的相機綁到選中的宣告槽 ────────────────────────
    [ObservableProperty] private string bindActionStatus = "";

    /// <summary>可否執行綁定：需連線 + 選了相機 + 選了槽。</summary>
    public bool CanBind => IsGrabConnected && SelectedCamera is not null && SelectedSlot is not null;

    partial void OnSelectedSlotChanged(CcdSlotModel? value) => OnPropertyChanged(nameof(CanBind));

    /// <summary>
    /// cam_id 取該槽在拓樸中的索引：拓樸固定時它唯一且穩定，且對 CCD00..CCD36 / IP0..IP36
    /// 的既有慣例剛好等於 CCD 編號。**不可**用列舉順序（那正是 #21 要修掉的東西）。
    /// </summary>
    private int CamIdForSlot(CcdSlotModel slot)
    {
        int idx = Topology.Slots.FindIndex(s => s.CcdId == slot.CcdId);
        return idx >= 0 ? idx : 0;
    }

    /// <summary>
    /// 送出**完整**映射表（現有綁定 + 本次異動）。送完整表而非增量：
    /// 增量語意在兩端各自維護狀態時極易分歧，一次覆蓋全表則「畫面所見 = 檔案內容」。
    /// </summary>
    private async Task<bool> PushMapAsync(List<(string Mac, int CamId, string CcdId)> entries, string what)
    {
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            var resp = await _svc.Connection.Grab.SetCamMapAsync(entries, cts.Token);
            if (resp?["status"]?.GetValue<string>() != "OK")
            {
                BindActionStatus = $"❌ {what}失敗：{resp?["error"]?.GetValue<string>() ?? "無回應"}";
                return false;
            }
            BindActionStatus = $"✓ {what}成功（{resp["written"]?.GetValue<int>() ?? entries.Count} 筆）";
            await LoadCameras();   // 重新列舉 → join 依 Grab 回的新 bound/ccd_id 刷新（不自行假設結果）
            return true;
        }
        catch (Exception ex) { BindActionStatus = $"❌ {what}失敗：{ex.Message}"; return false; }
    }

    /// <summary>把選中的相機綁到選中的宣告槽。</summary>
    [RelayCommand]
    private async Task BindSelected()
    {
        var cam = SelectedCamera; var slot = SelectedSlot;
        if (cam is null || slot is null) { BindActionStatus = "請先選一台相機與一個槽"; return; }
        if (string.IsNullOrWhiteSpace(cam.Mac)) { BindActionStatus = "❌ 此相機無 MAC，無法綁定"; return; }

        // 該槽已被「別台」佔用 → 拒絕（不默默搶槽；要換請先解除原本那台）
        var occupied = Cameras.FirstOrDefault(c => c.Bound && c.CcdId == slot.CcdId);
        if (occupied is not null && !MacUtil.Same(occupied.Mac, cam.Mac))
        {
            BindActionStatus = $"❌ {slot.CcdId} 已綁 {occupied.Model} SN={occupied.Serial}，請先解除";
            return;
        }

        // 完整表 = 其他已綁的維持原樣 + 本台改綁到目標槽
        var entries = Cameras
            .Where(c => c.Bound && !MacUtil.Same(c.Mac, cam.Mac) && !string.IsNullOrEmpty(c.CcdId))
            .Select(c => (c.Mac, c.CamId, c.CcdId))
            .ToList();
        entries.Add((cam.Mac, CamIdForSlot(slot), slot.CcdId));
        await PushMapAsync(entries, $"綁定 {slot.CcdId}");
    }

    /// <summary>解除選中相機的綁定（從映射表移除該筆）。</summary>
    [RelayCommand]
    private async Task UnbindSelected()
    {
        var cam = SelectedCamera;
        if (cam is null || !cam.Bound) { BindActionStatus = "請先選一台「已綁定」的相機"; return; }
        var entries = Cameras
            .Where(c => c.Bound && !MacUtil.Same(c.Mac, cam.Mac) && !string.IsNullOrEmpty(c.CcdId))
            .Select(c => (c.Mac, c.CamId, c.CcdId))
            .ToList();
        if (entries.Count == 0)
        {
            // Grab 端不接受空表（空表語意等同停用映射，應直接刪檔而非用此路徑）
            BindActionStatus = "❌ 這是最後一筆綁定；若要停用映射請直接刪除 grab 的 cam_map.json";
            return;
        }
        await PushMapAsync(entries, $"解除 {cam.CcdId}");
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

/// <summary>宣告槽的 live 狀態（宣告 × 偵測的 join 結果，Gap #21）。四種互斥狀態。</summary>
public enum SlotBindKind
{
    Declared,     // 已宣告 · 未綁（expected_mac 為 null，還沒說要哪台）— 黃
    Bound,        // 已綁定 · 就位（有相機且 MAC 相符/未指定）— 綠
    MacMismatch,  // 已就位但 MAC 與 expected_mac 不符 → 可能插錯槽位 — 紅告警
    Offline,      // expected_mac 有值但沒相機對到 → 該就位卻沒出現 — 灰
}

/// <summary>
/// 一個宣告槽 + join 到的實體相機。**宣告資料仍在 Slot 裡**（CcdSlotModel 保持純宣告），
/// 本型別只承載 runtime 的 join 結果，符合約束②「宣告與偵測分離」。
/// </summary>
public sealed partial class SlotBinding : ObservableObject
{
    public SlotBinding(CcdSlotModel slot) { Slot = slot; }

    public CcdSlotModel Slot { get; }
    /// <summary>就位的相機；null = 沒有相機綁到這個槽。由 RecomputeBindings 指派。</summary>
    public CameraInfoModel? Camera { get; set; }

    public string CcdId => Slot.CcdId;

    public SlotBindKind Kind =>
        Camera is null
            ? (Slot.HasExpectedMac ? SlotBindKind.Offline : SlotBindKind.Declared)
            : (Slot.HasExpectedMac && !MacUtil.Same(Slot.ExpectedMac, Camera.Mac)
                ? SlotBindKind.MacMismatch
                : SlotBindKind.Bound);

    public string StatusLabel => Kind switch
    {
        SlotBindKind.Bound       => "已綁定 · 就位",
        SlotBindKind.MacMismatch => "⚠ MAC 不符",
        SlotBindKind.Offline     => "離線（該就位卻沒出現）",
        _                        => "已宣告 · 未綁",
    };

    /// <summary>chip 的 ToolTip：一眼看出這個槽現在是誰、為什麼是這個狀態。</summary>
    public string Tooltip => Kind switch
    {
        SlotBindKind.Bound       => $"{CcdId}：{Camera!.Model} SN={Camera.Serial}\nMAC {Camera.MacDisplay} · {Camera.IpDisplay}",
        SlotBindKind.MacMismatch => $"{CcdId}：MAC 不符\n預期 {Slot.ExpectedMacDisplay}\n實際 {Camera!.MacDisplay}（{Camera.Model} SN={Camera.Serial}）",
        SlotBindKind.Offline     => $"{CcdId}：預期 {Slot.ExpectedMacDisplay} 應就位，但未偵測到",
        _                        => $"{CcdId}：尚未指定相機（expected_mac = TBD）",
    };

    /// <summary>Camera 指派後由 VM 呼叫，通知所有衍生屬性刷新（Camera 非 ObservableProperty）。</summary>
    public void Refresh()
    {
        OnPropertyChanged(nameof(Camera));
        OnPropertyChanged(nameof(Kind));
        OnPropertyChanged(nameof(StatusLabel));
        OnPropertyChanged(nameof(Tooltip));
    }
}

/// <summary>運算單元帶的一張卡：連線(真) + 處理 N 顆 CCD(真) + 負載%(估算容量投影)。</summary>
public sealed partial class ComputeUnitGroup : ObservableObject
{
    public ComputeUnitModel Unit { get; init; } = new();
    public ObservableCollection<SlotBinding> Slots { get; } = new();
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
