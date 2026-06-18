using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Shapes;
using Avalonia.Input;
using Avalonia.Markup.Xaml;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using CfAoiControl.Models;

namespace CfAoiControl.Controls;

/// <summary>
/// 影像/ROI 共用控制項（從 Step1View 抽出，行為一致）。輸入走 StyledProperty：
///   Source/ImageWidth/ImageHeight/PixelData(影像)、EditZone(要編的 ROI)、AllZones(畫全部 ROI 方框、選中=EditZone 高亮)、
///   Defects/SelectedDefectIndex/ResultVersion(缺陷 overlay)、Caption。輸出狀態：AxisText/ValueText/ZoomText/RegionText。
/// 縮放/平移/框 ROI 把手/數值/量測/缺陷導航 邏輯與原 Step1View code-behind 等價。
/// </summary>
public partial class RoiImageView : UserControl
{
    // ── 輸入 ──
    public static readonly StyledProperty<Bitmap?> SourceProperty =
        AvaloniaProperty.Register<RoiImageView, Bitmap?>(nameof(Source));
    public static readonly StyledProperty<int> ImageWidthProperty =
        AvaloniaProperty.Register<RoiImageView, int>(nameof(ImageWidth));
    public static readonly StyledProperty<int> ImageHeightProperty =
        AvaloniaProperty.Register<RoiImageView, int>(nameof(ImageHeight));
    public static readonly StyledProperty<byte[]?> PixelDataProperty =
        AvaloniaProperty.Register<RoiImageView, byte[]?>(nameof(PixelData));
    public static readonly StyledProperty<ZoneSettingModel?> EditZoneProperty =
        AvaloniaProperty.Register<RoiImageView, ZoneSettingModel?>(nameof(EditZone));
    public static readonly StyledProperty<IEnumerable?> AllZonesProperty =
        AvaloniaProperty.Register<RoiImageView, IEnumerable?>(nameof(AllZones));
    public static readonly StyledProperty<IReadOnlyList<DefectModel>?> DefectsProperty =
        AvaloniaProperty.Register<RoiImageView, IReadOnlyList<DefectModel>?>(nameof(Defects));
    public static readonly StyledProperty<int> SelectedDefectIndexProperty =
        AvaloniaProperty.Register<RoiImageView, int>(nameof(SelectedDefectIndex),
            defaultValue: -1, defaultBindingMode: Avalonia.Data.BindingMode.TwoWay);
    public static readonly StyledProperty<int> ResultVersionProperty =
        AvaloniaProperty.Register<RoiImageView, int>(nameof(ResultVersion));
    public static readonly StyledProperty<string> CaptionProperty =
        AvaloniaProperty.Register<RoiImageView, string>(nameof(Caption), "");

    // ── 輸出（控制項寫，host 綁狀態列）──
    public static readonly StyledProperty<string> AxisTextProperty =
        AvaloniaProperty.Register<RoiImageView, string>(nameof(AxisText), "");
    public static readonly StyledProperty<string> ValueTextProperty =
        AvaloniaProperty.Register<RoiImageView, string>(nameof(ValueText), "");
    public static readonly StyledProperty<string> ZoomTextProperty =
        AvaloniaProperty.Register<RoiImageView, string>(nameof(ZoomText), "");
    public static readonly StyledProperty<string> RegionTextProperty =
        AvaloniaProperty.Register<RoiImageView, string>(nameof(RegionText), "");

    public Bitmap? Source { get => GetValue(SourceProperty); set => SetValue(SourceProperty, value); }
    public int ImageWidth { get => GetValue(ImageWidthProperty); set => SetValue(ImageWidthProperty, value); }
    public int ImageHeight { get => GetValue(ImageHeightProperty); set => SetValue(ImageHeightProperty, value); }
    public byte[]? PixelData { get => GetValue(PixelDataProperty); set => SetValue(PixelDataProperty, value); }
    public ZoneSettingModel? EditZone { get => GetValue(EditZoneProperty); set => SetValue(EditZoneProperty, value); }
    public IEnumerable? AllZones { get => GetValue(AllZonesProperty); set => SetValue(AllZonesProperty, value); }
    public IReadOnlyList<DefectModel>? Defects { get => GetValue(DefectsProperty); set => SetValue(DefectsProperty, value); }
    public int SelectedDefectIndex { get => GetValue(SelectedDefectIndexProperty); set => SetValue(SelectedDefectIndexProperty, value); }
    public int ResultVersion { get => GetValue(ResultVersionProperty); set => SetValue(ResultVersionProperty, value); }
    public string Caption { get => GetValue(CaptionProperty); set => SetValue(CaptionProperty, value); }
    public string AxisText { get => GetValue(AxisTextProperty); set => SetValue(AxisTextProperty, value); }
    public string ValueText { get => GetValue(ValueTextProperty); set => SetValue(ValueTextProperty, value); }
    public string ZoomText { get => GetValue(ZoomTextProperty); set => SetValue(ZoomTextProperty, value); }
    public string RegionText { get => GetValue(RegionTextProperty); set => SetValue(RegionTextProperty, value); }

    /// <summary>內部缺陷選取變更（host 用來捲動縮圖牆）。</summary>
    public event Action<int>? DefectSelected;

    private Border? _viewport;
    private Canvas? _content, _measureOverlay, _roiOverlay;
    private DefectOverlayControl? _defectOverlay;

    private double _scale = 1, _fitScale = 1, _offX, _offY;
    private bool _fitted, _panning, _maybePan, _space, _measureKey;
    private Point _panLast, _pressPos;
    private const double PanThreshold = 4;
    private readonly List<Point> _measurePts = new();

    private bool _roiMode, _roiDragging;
    private Point _roiStart, _roiCur;
    private ZoneSettingModel? _roiZone;
    private int _roiHandle = -1;
    private const double HandleHit = 10, HandleSize = 9;

    public RoiImageView()
    {
        AvaloniaXamlLoader.Load(this);
        _viewport = this.FindControl<Border>("Viewport");
        _content = this.FindControl<Canvas>("ContentRoot");
        _measureOverlay = this.FindControl<Canvas>("MeasureOverlay");
        _roiOverlay = this.FindControl<Canvas>("RoiOverlay");
        _defectOverlay = this.FindControl<DefectOverlayControl>("DefectOverlay");
        if (this.FindControl<Button>("BtnFit") is { } f) f.Click += (_, _) => Fit();
        if (this.FindControl<Button>("BtnRefresh") is { } r) r.Click += (_, _) => RefreshOverlay();
        if (this.FindControl<Button>("BtnRoi") is { } br) br.Click += (_, _) => ToggleRoiMode();
        if (this.FindControl<Button>("BtnRoiClear") is { } bc) bc.Click += (_, _) => ClearRoi();

        if (_viewport != null)
        {
            _viewport.PointerWheelChanged += OnWheel;
            _viewport.PointerPressed += OnPressed;
            _viewport.PointerMoved += OnMoved;
            _viewport.PointerReleased += OnReleased;
            _viewport.KeyDown += OnKeyDown;
            _viewport.KeyUp += OnKeyUp;
            _viewport.PointerEntered += (_, _) => _viewport?.Focus();
            _viewport.SizeChanged += (_, _) => { if (!_fitted) Fit(); };
        }
        HookRoiZone();
    }

    protected override void OnPropertyChanged(AvaloniaPropertyChangedEventArgs e)
    {
        base.OnPropertyChanged(e);
        if (e.Property == SourceProperty && Source != null)
            Dispatcher.UIThread.Post(() => { _measurePts.Clear(); Fit(); });
        else if (e.Property == EditZoneProperty)
            HookRoiZone();
        else if (e.Property == ResultVersionProperty)
            Dispatcher.UIThread.Post(() =>
            {
                _defectOverlay?.SetDefects(Defects);
                if (_defectOverlay != null) { _defectOverlay.SelectedIndex = -1; _defectOverlay.StrokeScale = _scale; }
            });
        else if (e.Property == DefectsProperty)
            Dispatcher.UIThread.Post(RefreshOverlay);
        else if (e.Property == SelectedDefectIndexProperty && _defectOverlay != null)
            _defectOverlay.SelectedIndex = SelectedDefectIndex;
    }

    /// <summary>從目前 Defects 重建大圖圓圈標示（開窗自動重建 + 「刷新標示」鈕）。</summary>
    public void RefreshOverlay()
    {
        if (_defectOverlay is null) return;
        _defectOverlay.SetDefects(Defects);
        _defectOverlay.SelectedIndex = SelectedDefectIndex;
        _defectOverlay.StrokeScale = _scale;
    }

    // ===== 變換核心 =====
    private Matrix CurrentMatrix() => new(_scale, 0, 0, _scale, _offX, _offY);
    private Point ScreenToContent(Point s) => CurrentMatrix().TryInvert(out var inv) ? inv.Transform(s) : s;
    private Point ContentToScreen(Point c) => CurrentMatrix().Transform(c);

    private void ApplyTransform()
    {
        if (_content != null) _content.RenderTransform = new MatrixTransform(CurrentMatrix());
        if (_defectOverlay != null) _defectOverlay.StrokeScale = _scale;
        ZoomText = $"| Zoom : {_scale:F2}x";
        RedrawMeasure();
        RedrawRoi();
    }

    public void Fit()
    {
        if (_viewport is null) return;
        double vw = _viewport.Bounds.Width, vh = _viewport.Bounds.Height;
        double iw = Math.Max(1, ImageWidth), ih = Math.Max(1, ImageHeight);
        if (vw < 2 || vh < 2) return;
        _fitScale = Math.Min(vw / iw, vh / ih);
        _scale = _fitScale;
        _offX = (vw - iw * _scale) / 2;
        _offY = (vh - ih * _scale) / 2;
        _fitted = true;
        ApplyTransform();
    }

    private void OnWheel(object? sender, PointerWheelEventArgs e)
    {
        if (Source is null) return;
        var s = e.GetPosition(_viewport);
        var c = ScreenToContent(s);
        double factor = e.Delta.Y > 0 ? 1.15 : 1 / 1.15;
        double min = Math.Min(0.1, _fitScale), max = 10.0;
        double ns = Math.Clamp(_scale * factor, min, max);
        _offX = s.X - c.X * ns;
        _offY = s.Y - c.Y * ns;
        _scale = ns;
        ApplyTransform();
        e.Handled = true;
    }

    private void OnPressed(object? sender, PointerPressedEventArgs e)
    {
        _viewport?.Focus();
        var pt = e.GetCurrentPoint(_viewport);
        var s = pt.Position;

        if (_measureKey && pt.Properties.IsLeftButtonPressed)
        {
            if (_measurePts.Count >= 2) _measurePts.Clear();
            _measurePts.Add(ScreenToContent(s));
            if (_measurePts.Count == 2) UpdateMeasureReadout();
            RedrawMeasure();
            e.Handled = true;
            return;
        }
        if (pt.Properties.IsMiddleButtonPressed || (_space && pt.Properties.IsLeftButtonPressed))
        {
            _panning = true; _panLast = s;
            e.Pointer.Capture(_viewport);
            e.Handled = true;
            return;
        }
        if (_roiMode && pt.Properties.IsLeftButtonPressed)
        {
            int h = HitTestRoiHandle(s);
            if (h >= 0) { _roiHandle = h; e.Pointer.Capture(_viewport); e.Handled = true; return; }
            _roiDragging = true; _roiStart = ScreenToContent(s); _roiCur = _roiStart;
            e.Pointer.Capture(_viewport); RedrawRoi(); e.Handled = true;
            return;
        }
        if (pt.Properties.IsLeftButtonPressed)
        {
            _maybePan = true; _pressPos = s; _panLast = s;
            e.Pointer.Capture(_viewport); e.Handled = true;
        }
    }

    private int HitTestDefect(Point c)
    {
        var ds = Defects;
        if (ds is null) return -1;
        for (int i = 0; i < ds.Count; i++)
        {
            var d = ds[i];
            if (c.X >= d.XMin && c.X <= d.XMax && c.Y >= d.YMin && c.Y <= d.YMax) return i;
        }
        return -1;
    }

    private void OnMoved(object? sender, PointerEventArgs e)
    {
        var s = e.GetPosition(_viewport);
        if (_maybePan && !_panning)
        {
            double mdx = s.X - _pressPos.X, mdy = s.Y - _pressPos.Y;
            if (mdx * mdx + mdy * mdy >= PanThreshold * PanThreshold) { _panning = true; _panLast = s; }
        }
        if (_panning)
        {
            _offX += s.X - _panLast.X; _offY += s.Y - _panLast.Y; _panLast = s;
            ApplyTransform();
        }
        else if (_roiDragging)
        {
            _roiCur = ScreenToContent(s);
            ShowRoiReadout((int)Math.Round(Math.Min(_roiStart.X, _roiCur.X)), (int)Math.Round(Math.Min(_roiStart.Y, _roiCur.Y)),
                           (int)Math.Round(Math.Max(_roiStart.X, _roiCur.X)), (int)Math.Round(Math.Max(_roiStart.Y, _roiCur.Y)));
            RedrawRoi();
        }
        else if (_roiHandle >= 0) UpdateRoiHandle(ScreenToContent(s));
        UpdateAxisValue(s);
    }

    private void OnReleased(object? sender, PointerReleasedEventArgs e)
    {
        if (_panning) { _panning = false; _maybePan = false; e.Pointer.Capture(null); }
        else if (_roiDragging) { _roiDragging = false; e.Pointer.Capture(null); CommitRoi(); }
        else if (_roiHandle >= 0) { _roiHandle = -1; e.Pointer.Capture(null); }
        else if (_maybePan)
        {
            _maybePan = false; e.Pointer.Capture(null);
            int hit = HitTestDefect(ScreenToContent(_pressPos));
            if (hit >= 0) SelectDefect(hit, center: false);
        }
    }

    private void OnKeyDown(object? sender, KeyEventArgs e)
    {
        var ds = Defects;
        if (e.Key == Key.Space) _space = true;
        else if (e.Key == Key.M) _measureKey = true;
        else if (e.Key == Key.F) Fit();
        else if (e.Key == Key.Right && ds is { Count: > 0 })
        { SelectDefect(Math.Min((SelectedDefectIndex < 0 ? -1 : SelectedDefectIndex) + 1, ds.Count - 1), center: true); e.Handled = true; }
        else if (e.Key == Key.Left && ds is { Count: > 0 })
        { SelectDefect(Math.Max((SelectedDefectIndex < 0 ? 1 : SelectedDefectIndex) - 1, 0), center: true); e.Handled = true; }
    }

    private void OnKeyUp(object? sender, KeyEventArgs e)
    {
        if (e.Key == Key.Space) _space = false;
        else if (e.Key == Key.M) _measureKey = false;
    }

    /// <summary>選取某缺陷：高亮 overlay + 設 SelectedDefectIndex(→VM) + 可選置中；raise 事件供 host 捲縮圖。</summary>
    public void SelectDefect(int idx, bool center)
    {
        var ds = Defects;
        if (ds is null || idx < 0 || idx >= ds.Count) return;
        SelectedDefectIndex = idx;
        if (_defectOverlay != null) _defectOverlay.SelectedIndex = idx;
        if (center) CenterOnDefect(ds[idx]);
        DefectSelected?.Invoke(idx);
    }

    private void CenterOnDefect(DefectModel d)
    {
        if (_viewport is null) return;
        double vw = _viewport.Bounds.Width, vh = _viewport.Bounds.Height;
        if (vw < 2 || vh < 2) return;
        _scale = Math.Clamp(5.0, Math.Min(0.1, _fitScale), 10.0);
        _offX = vw / 2 - d.GlobalPosX * _scale;
        _offY = vh / 2 - d.GlobalPosY * _scale;
        ApplyTransform();
    }

    private void UpdateAxisValue(Point screen)
    {
        var c = ScreenToContent(screen);
        int ix = (int)Math.Floor(c.X), iy = (int)Math.Floor(c.Y);
        if (PixelData is { } px && ix >= 0 && iy >= 0 && ix < ImageWidth && iy < ImageHeight)
        {
            AxisText = $"| Axis : ({ix},{iy})";
            ValueText = $"| Value : {px[iy * ImageWidth + ix]}";
        }
        else { AxisText = "| Axis : -"; ValueText = "| Value : -"; }
    }

    private void UpdateMeasureReadout()
    {
        if (_measurePts.Count < 2) return;
        double dx = Math.Abs(_measurePts[1].X - _measurePts[0].X);
        double dy = Math.Abs(_measurePts[1].Y - _measurePts[0].Y);
        RegionText = $"| dx={dx:F0} dy={dy:F0} d={Math.Sqrt(dx * dx + dy * dy):F1}";
    }

    private void RedrawMeasure()
    {
        if (_measureOverlay is null) return;
        _measureOverlay.Children.Clear();
        if (_measurePts.Count == 0) return;
        var brush = new SolidColorBrush(Color.Parse("#FF3030"));
        Point? prev = null;
        foreach (var cpt in _measurePts)
        {
            var sp = ContentToScreen(cpt);
            var dot = new Ellipse { Width = 10, Height = 10, Stroke = brush, StrokeThickness = 2, Fill = Brushes.Transparent };
            Canvas.SetLeft(dot, sp.X - 5); Canvas.SetTop(dot, sp.Y - 5);
            _measureOverlay.Children.Add(dot);
            if (prev is { } p) _measureOverlay.Children.Add(new Line { StartPoint = p, EndPoint = sp, Stroke = brush, StrokeThickness = 1.5 });
            prev = sp;
        }
    }

    // ===== ROI =====
    private void ToggleRoiMode()
    {
        _roiMode = !_roiMode;
        if (this.FindControl<Button>("BtnRoi") is { } b)
            b.Background = _roiMode ? new SolidColorBrush(Color.Parse("#9CCC65")) : null;
        RegionText = _roiMode ? "| ROI 框選中：拖矩形；放大後拖把手或用下方數值精修" : "";
        RedrawRoi();
    }

    private void ClearRoi()
    {
        if (EditZone is { } z) { z.StartX = -1; z.StartY = -1; z.EndX = -1; z.EndY = -1; }
        RegionText = "| ROI 全幅(-1)";
        RedrawRoi();
    }

    private void HookRoiZone()
    {
        if (_roiZone != null) _roiZone.PropertyChanged -= OnRoiZoneChanged;
        _roiZone = EditZone;
        if (_roiZone != null) _roiZone.PropertyChanged += OnRoiZoneChanged;
        RedrawRoi();
    }

    private void OnRoiZoneChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is "StartX" or "StartY" or "EndX" or "EndY")
            Dispatcher.UIThread.Post(RedrawRoi);
    }

    private void CommitRoi()
    {
        if (EditZone is not { } z) return;
        int iw = Math.Max(1, ImageWidth), ih = Math.Max(1, ImageHeight);
        int x0 = (int)Math.Round(Math.Min(_roiStart.X, _roiCur.X));
        int y0 = (int)Math.Round(Math.Min(_roiStart.Y, _roiCur.Y));
        int x1 = (int)Math.Round(Math.Max(_roiStart.X, _roiCur.X));
        int y1 = (int)Math.Round(Math.Max(_roiStart.Y, _roiCur.Y));
        x0 = Math.Clamp(x0, 0, iw); x1 = Math.Clamp(x1, 0, iw);
        y0 = Math.Clamp(y0, 0, ih); y1 = Math.Clamp(y1, 0, ih);
        if (x1 - x0 < 2 || y1 - y0 < 2) { RedrawRoi(); return; }
        z.StartX = x0; z.StartY = y0; z.EndX = x1; z.EndY = y1;
        ShowRoiReadout(x0, y0, x1, y1);
        RedrawRoi();
    }

    private void ShowRoiReadout(int x0, int y0, int x1, int y1)
        => RegionText = $"| ROI ({x0},{y0})-({x1},{y1})  {x1 - x0}×{y1 - y0}";

    private static bool ZoneValid(ZoneSettingModel? z)
        => z is not null && z.StartX >= 0 && z.StartY >= 0 && z.EndX > z.StartX && z.EndY > z.StartY;

    private void RedrawRoi()
    {
        if (_roiOverlay is null) return;
        _roiOverlay.Children.Clear();
        var edit = EditZone;
        // 先畫其他 ROI（灰），再畫選中 ROI（藍）+ 把手 → 多 ROI 對照定位（AllZones 為空則僅 EditZone）
        if (AllZones is { } all)
            foreach (var o in all)
                if (o is ZoneSettingModel z && !ReferenceEquals(z, edit) && ZoneValid(z))
                    DrawRoiRect(z.StartX, z.StartY, z.EndX, z.EndY, "#607080");
        if (ZoneValid(edit))
            DrawRoiRect(edit!.StartX, edit.StartY, edit.EndX, edit.EndY, "#00B0FF");
        if (_roiDragging)
            DrawRoiRect(Math.Min(_roiStart.X, _roiCur.X), Math.Min(_roiStart.Y, _roiCur.Y),
                        Math.Max(_roiStart.X, _roiCur.X), Math.Max(_roiStart.Y, _roiCur.Y), "#FFC400");
        else if (ZoneValid(edit) && _roiMode)
            foreach (var p in RoiHandlePoints(edit!)) DrawHandle(p);
    }

    private Point[] RoiHandlePoints(ZoneSettingModel z)
    {
        var tl = ContentToScreen(new Point(z.StartX, z.StartY));
        var br = ContentToScreen(new Point(z.EndX, z.EndY));
        double mx = (tl.X + br.X) / 2, my = (tl.Y + br.Y) / 2;
        return new[]
        {
            new Point(tl.X, tl.Y), new Point(mx, tl.Y), new Point(br.X, tl.Y), new Point(br.X, my),
            new Point(br.X, br.Y), new Point(mx, br.Y), new Point(tl.X, br.Y), new Point(tl.X, my),
        };
    }

    private int HitTestRoiHandle(Point screen)
    {
        if (!ZoneValid(EditZone)) return -1;
        var pts = RoiHandlePoints(EditZone!);
        for (int i = 0; i < pts.Length; i++)
        {
            double dx = screen.X - pts[i].X, dy = screen.Y - pts[i].Y;
            if (dx * dx + dy * dy <= HandleHit * HandleHit) return i;
        }
        return -1;
    }

    private void UpdateRoiHandle(Point c)
    {
        if (EditZone is not { } z) return;
        int iw = Math.Max(1, ImageWidth), ih = Math.Max(1, ImageHeight);
        int cx = (int)Math.Round(Math.Clamp(c.X, 0, iw)), cy = (int)Math.Round(Math.Clamp(c.Y, 0, ih));
        bool left = _roiHandle is 0 or 6 or 7, right = _roiHandle is 2 or 3 or 4;
        bool top = _roiHandle is 0 or 1 or 2, bottom = _roiHandle is 4 or 5 or 6;
        if (left) z.StartX = Math.Min(cx, z.EndX - 2);
        if (right) z.EndX = Math.Max(cx, z.StartX + 2);
        if (top) z.StartY = Math.Min(cy, z.EndY - 2);
        if (bottom) z.EndY = Math.Max(cy, z.StartY + 2);
        ShowRoiReadout(z.StartX, z.StartY, z.EndX, z.EndY);
    }

    private void DrawHandle(Point screen)
    {
        var sq = new Rectangle
        {
            Width = HandleSize, Height = HandleSize, Fill = Brushes.White,
            Stroke = new SolidColorBrush(Color.Parse("#00B0FF")), StrokeThickness = 1.5,
        };
        Canvas.SetLeft(sq, screen.X - HandleSize / 2);
        Canvas.SetTop(sq, screen.Y - HandleSize / 2);
        _roiOverlay!.Children.Add(sq);
    }

    private void DrawRoiRect(double x0, double y0, double x1, double y1, string color)
    {
        var p0 = ContentToScreen(new Point(x0, y0));
        var p1 = ContentToScreen(new Point(x1, y1));
        var rect = new Rectangle
        {
            Width = Math.Abs(p1.X - p0.X), Height = Math.Abs(p1.Y - p0.Y),
            Stroke = new SolidColorBrush(Color.Parse(color)), StrokeThickness = 2, Fill = Brushes.Transparent,
        };
        Canvas.SetLeft(rect, Math.Min(p0.X, p1.X));
        Canvas.SetTop(rect, Math.Min(p0.Y, p1.Y));
        _roiOverlay!.Children.Add(rect);
    }
}
