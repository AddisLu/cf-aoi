using System.Collections.Generic;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;
using CfAoiControl.Models;

namespace CfAoiControl.Controls;

/// <summary>
/// 在大圖上畫所有缺陷框（亮=紅、暗=藍、選中=綠）。單一控制項一次 Render 全部框，
/// 數千框也順暢；放在 ContentRoot 內，套同一 MatrixTransform → 隨縮放/平移。
/// StrokeScale = 當前縮放倍率，用來讓框線維持約略固定的螢幕粗細。
/// </summary>
public sealed class DefectOverlayControl : Control
{
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

        double t = System.Math.Max(0.5, 2.0 / _strokeScale);    // 約 2px 螢幕粗細
        double ts = System.Math.Max(1.0, 4.0 / _strokeScale);   // 選中框較粗
        var brightPen = new Pen(Brushes.Red, t);
        var darkPen = new Pen(Brushes.Blue, t);
        var selPen = new Pen(Brushes.Lime, ts);

        for (int i = 0; i < _defects.Count; i++)
        {
            var d = _defects[i];
            var rect = new Rect(d.XMin, d.YMin, d.BoxW, d.BoxH);
            var pen = i == _selectedIndex ? selPen : (d.IsBright ? brightPen : darkPen);
            ctx.DrawRectangle(null, pen, rect);   // 只畫框不填充
        }
    }
}
