namespace CfAoiControl.Models;

/// <summary>GigE 機器層參數快照（open() 設的東西,供 UI 顯示「看得到」）。對齊 grab MachineParams。</summary>
public sealed class CamNodesModel
{
    public string PixelFormat     { get; init; } = "";
    public string ExposureAuto    { get; init; } = "";
    public string GainAuto        { get; init; } = "";
    public string TriggerMode     { get; init; } = "";
    public string TriggerSelector { get; init; } = "";
    public string TriggerSource   { get; init; } = "";
    public long   Width           { get; init; }
    public long   Height          { get; init; }
    public long   PacketSize      { get; init; }
    public long   Scpd            { get; init; }
}
