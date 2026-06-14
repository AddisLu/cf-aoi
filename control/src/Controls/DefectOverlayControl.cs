using System.Collections.Generic;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;
using CfAoiControl.Models;

namespace CfAoiControl.Controls;

/// <summary>
/// 在大圖上畫所有缺陷的「中空圓圈」（亮=紅、暗=藍、選中=綠）。單一控制項一次 Render 全部，
/// 數千個也順暢；放在 ContentRoot 內，套同一 MatrixTransform → 圓心/半徑用 content 座標隨縮放/平移。
/// StrokeScale = 當前縮放倍率：① 線寬維持約略固定螢幕粗細 ② 換算最小螢幕半徑。
/// 半徑 = max( sqrt(Size/π)*AreaFactor , 最小螢幕半徑/scale )：比缺陷本體大一圈、小缺陷也看得到。
/// </summary>
public sealed class DefectOverlayControl : Control
{
    private const double AreaFactor = 1.8;     // 圓比缺陷面積等效半徑放大的倍數
    private const double MinScreenRadius = 8;  // 最小螢幕半徑(px)，避免小缺陷圓圈太小看不到

    private IReadOnlyList<DefectModel>? _defects;
    private int _selectedIndex = -1;
    private double _strokeScale = 1;

    public void SetDefects(IReadOnlyList<DefectModel>? defects)
    {
        _defects = defects;
        InvalidateVisual();
    }

    public int SelectedIndex
    {
        get => _selectedIndex;
        set { if (_selectedIndex != value) { _selectedIndex = value; InvalidateVisual(); } }
    }

    public double StrokeScale
    {
        get => _strokeScale;
        set { if (_strokeScale != value) { _strokeScale = value <= 0 ? 1 : value; InvalidateVisual(); } }
    }

    public override void Render(DrawingContext ctx)
    {
        if (_defects is null || _defects.Count == 0) return;

        double t = System.Math.Max(0.5, 2.0 / _strokeScale);    // 約 2px 螢幕粗細（線寬不隨縮放暴增）
        double ts = System.Math.Max(1.0, 3.0 / _strokeScale);   // 選中圈較粗
        var brightPen = new Pen(Brushes.Red, t);
        var darkPen = new Pen(Brushes.Blue, t);
        var selPen = new Pen(Brushes.Lime, ts);

        double minContentR = MinScreenRadius / _strokeScale;    // 螢幕 8px 換算成 content 半徑

        for (int i = 0; i < _defects.Count; i++)
        {
            var d = _defects[i];
            double areaR = System.Math.Sqrt(System.Math.Max(1, d.Size) / System.Math.PI) * AreaFactor;
            double r = System.Math.Max(areaR, minContentR);
            var center = new Point(d.GlobalPosX, d.GlobalPosY);
            var pen = i == _selectedIndex ? selPen : (d.IsBright ? brightPen : darkPen);
            ctx.DrawEllipse(null, pen, center, r, r);   // 中空圓（不填充）→ 不遮缺陷本體
        }
    }
}
