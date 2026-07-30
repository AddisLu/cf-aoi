namespace CfAoiControl.Models;

/// <summary>
/// 相機狀態。⚠️ 2026-07-30 語意修正：
///   舊版用 persistent(有固定 IP) 推斷「已綁定」——那是「IP 設定方式」，
///   與「綁到哪個 CCD 槽位」是兩件完全不同的事。
/// 現在改用 grab 的 bound 欄位（= 是否列於 cam_map.json，Gap #21）：
///   Bound   = 已綁定某個 CCD 槽位（cam_map.json 有這台的 MAC）
///   Unbound = 偵測到但未列於 cam_map → **不得指派到任何槽**（約束②）
///   Offline = 未出現在列舉
/// </summary>
public enum CamStatusKind { Bound, Unbound, Offline }

/// <summary>
/// LIST_CAMERAS 列舉結果(對齊 grab CamInfo)。純資料 + 衍生狀態。
/// online = 出現在列舉；persistent = IsPersistentIpActive()（只是 IP 設定方式，非綁定）。
/// bound/ccd_id = grab 依 cam_map.json 判定的槽位綁定（Gap #21）；舊版 grab 不回這兩欄 → 預設未綁。
/// </summary>
public sealed class CameraInfoModel
{
    public int    CamId       { get; init; }
    public string Mac         { get; init; } = "";
    public string Model       { get; init; } = "";
    public string Serial      { get; init; } = "";
    public string Ip          { get; init; } = "";
    public bool   Online      { get; init; } = true;
    public bool   Persistent  { get; init; }
    public string IpConfig    { get; init; } = "";
    public string DeviceClass { get; init; } = "";

    // Gap #21：grab cam_map.json 的綁定結果。舊版 grab 無此欄位 → CcdId="" / Bound=false（退回舊行為）。
    public string CcdId       { get; init; } = "";
    public bool   Bound       { get; init; }

    public CamStatusKind Status =>
        !Online ? CamStatusKind.Offline :
        Bound   ? CamStatusKind.Bound   :
                  CamStatusKind.Unbound;

    public string StatusLabel => Status switch
    {
        CamStatusKind.Bound   => "已綁定",
        CamStatusKind.Unbound => "待綁定",
        _                     => "離線",
    };

    /// <summary>綁定到的 CCD 槽位名；未綁定時退回以 cam_id 推的顯示名（僅供辨識，非綁定憑據）。</summary>
    public string CcdLabel => string.IsNullOrEmpty(CcdId) ? $"CCD{CamId:00}" : CcdId;

    /// <summary>綁定來源說明（UI 明細用），避免使用者把「待綁定」誤解為相機故障。</summary>
    public string BindSourceText => Bound
        ? $"cam_map.json → {CcdId}"
        : "未列於 cam_map.json（不指派槽位）";

    /// <summary>IP 設定方式。與綁定無關，獨立顯示避免與「已綁定」混淆。</summary>
    public string IpModeText => Persistent ? "固定 IP" : "自動 IP";

    public string IpDisplay => string.IsNullOrEmpty(Ip) ? "—" : Ip;
    public string MacDisplay => string.IsNullOrEmpty(Mac) ? "—" : Mac;
}
