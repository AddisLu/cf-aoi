namespace CfAoiControl.Models;

/// <summary>
/// Gap #2：SET/GET_CAM_PARAMS 回傳結果。
/// ExposureUs/GainRaw = requested；ExposureUsActual/GainRawActual = read-back actual（相機實際套用）。
/// </summary>
public class CamParamsResult
{
    public double ExposureUs       { get; init; }
    public int    GainRaw          { get; init; }
    public double ExposureUsActual { get; init; }
    public int    GainRawActual    { get; init; }
}
