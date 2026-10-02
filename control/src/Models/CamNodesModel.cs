using System;

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

    // 行速率（決定 8-Way 演算法吃到的影像比例尺，見 grab 不變式 11）。
    // LineRateSet = AcquisitionLineRateAbs（顯式設定值）；LineRateResulting = ResultingLineRateAbs（相機依
    // ROI/曝光/頻寬算出的實際上限，唯讀）。同批相機若 LineRateSet 不一致 → 掃描方向量測值/涵蓋範圍跟著跑掉，
    // 且單看 fps 看不出來（2026-09-21 實測：CCD01/02 鎖死 11,001Hz vs CCD03/04 12,195Hz，差 11%）。
    public double LineRateSet       { get; init; }
    public double LineRateResulting { get; init; }

    /// <summary>確認行速率是否符合產線預期（8-way 速度確認）：
    /// 須已顯式設定（&gt;0）、不超過相機實際上限、且落在 expectedHz 的容許誤差內。</summary>
    public bool IsLineRateOk(double expectedHz, double toleranceRatio = 0.02)
        => LineRateSet > 0
           && LineRateSet <= LineRateResulting * 1.001
           && Math.Abs(LineRateSet - expectedHz) / expectedHz <= toleranceRatio;
}
