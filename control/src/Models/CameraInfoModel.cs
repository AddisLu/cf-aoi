namespace CfAoiControl.Models;

/// <summary>相機狀態:已綁定(有 persistent IP)/ 待綁定(online 但非 persistent,典型 169.254 auto-IP)/ 離線。</summary>
public enum CamStatusKind { Bound, Unbound, Offline }

/// <summary>
/// LIST_CAMERAS 列舉結果(對齊 grab CamInfo)。純資料 + 衍生狀態。
/// online = 出現在列舉;persistent = IsPersistentIpActive()。
/// 「離線」需配置 vs 偵測映射(= Gap #21),本階段列舉到的恆為 online。
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

    public CamStatusKind Status =>
        !Online      ? CamStatusKind.Offline :
        Persistent   ? CamStatusKind.Bound   :
                       CamStatusKind.Unbound;

    public string StatusLabel => Status switch
    {
        CamStatusKind.Bound   => "已綁定",
        CamStatusKind.Unbound => "待綁定",
        _                     => "離線",
    };

    public string CcdLabel => $"CCD{CamId:00}";
    public string IpDisplay => string.IsNullOrEmpty(Ip) ? "—" : Ip;
    public string MacDisplay => string.IsNullOrEmpty(Mac) ? "—" : Mac;
}
