using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Shapes;
using Avalonia.Input;
using Avalonia.Markup.Xaml;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using CfAoiControl.Controls;
using CfAoiControl.ViewModels;

namespace CfAoiControl.Views;

public partial class Step1View : UserControl
{
    private Border? _viewport;
    private Canvas? _content;
    private Canvas? _measureOverlay;
    private DefectOverlayControl? _defectOverlay;
    private ListBox? _thumbList;
    private bool _navigating;   // 防止 thumb↔index 互設造成迴圈

    // 變換狀態：screen = scale*content + offset
    private double _scale = 1, _fitScale = 1, _offX, _offY;
    private bool _fitted;
    private bool _panning;
    private Point _panLast;
    private bool _space, _measureKey;
    private readonly List<Point> _measurePts = new();   // content 座標

    public Step1View()
    {
        AvaloniaXamlLoader.Load(this);
        _viewport = this.FindControl<Border>("Viewport");
        _content = this.FindControl<Canvas>("ContentRoot");
        _measureOverlay = this.FindControl<Canvas>("MeasureOverlay");
        _defectOverlay = this.FindControl<DefectOverlayControl>("DefectOverlay");
        _thumbList = this.FindControl<ListBox>("ThumbList");
        if (_thumbList != null) _thumbList.SelectionChanged += OnThumbSelectionChanged;
        var fit = this.FindControl<Button>("BtnFit");
        if (fit != null) fit.Click += (_, _) => Fit();

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

        DataContextChanged += OnDataContextChanged;
    }

    private Step1ViewModel? Vm => DataContext as Step1ViewModel;

    private void OnDataContextChanged(object? sender, EventArgs e)
    {
        WireFilePicker();
        if (Vm is { } vm)
        {
            vm.PropertyChanged -= OnVmPropertyChanged;
            vm.PropertyChanged += OnVmPropertyChanged;
        }
    }

    // 載入新圖 → 重新 Fit、清量測；分析完成（ResultVersion）→ 重綁 overlay 的缺陷清單
    private void OnVmPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(Step1ViewModel.SourceBitmap) && Vm?.SourceBitmap != null)
            Dispatcher.UIThread.Post(() => { _measurePts.Clear(); Fit(); });
        else if (e.PropertyName == nameof(Step1ViewModel.ResultVersion))
            Dispatcher.UIThread.Post(() =>
            {
                _defectOverlay?.SetDefects(Vm?.NavDefects);
                if (_defectOverlay != null) { _defectOverlay.SelectedIndex = -1; _defectOverlay.StrokeScale = _scale; }
            });
    }

    // ===== 變換核心（用 Matrix 反矩陣換算座標，縮放/平移後皆精確）=====
    private Matrix CurrentMatrix() => new(_scale, 0, 0, _scale, _offX, _offY);
    private Point ScreenToContent(Point s) => CurrentMatrix().TryInvert(out var inv) ? inv.Transform(s) : s;
    private Point ContentToScreen(Point c) => CurrentMatrix().Transform(c);

    private void ApplyTransform()
    {
        if (_content != null) _content.RenderTransform = new MatrixTransform(CurrentMatrix());
        if (_defectOverlay != null) _defectOverlay.StrokeScale = _scale;   // 框線維持約略固定螢幕粗細
        if (Vm != null) Vm.ZoomText = $"| Zoom : {_scale:F2}x";
        RedrawMeasure();
    }

    private void Fit()
    {
        if (_viewport is null || Vm is null) return;
        double vw = _viewport.Bounds.Width, vh = _viewport.Bounds.Height;
        double iw = Math.Max(1, Vm.ImageWidth), ih = Math.Max(1, Vm.ImageHeight);
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
        if (Vm?.SourceBitmap is null) return;
        var s = e.GetPosition(_viewport);
        var c = ScreenToContent(s);
        double factor = e.Delta.Y > 0 ? 1.15 : 1 / 1.15;
        double min = Math.Min(0.1, _fitScale), max = 10.0;
        double ns = Math.Clamp(_scale * factor, min, max);
        // 保持游標下的 content 點不動：offset = screen - content*newScale
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

        // 量測模式（按住 M）：左鍵點兩點
        if (_measureKey && pt.Properties.IsLeftButtonPressed)
        {
            if (_measurePts.Count >= 2) _measurePts.Clear();
            _measurePts.Add(ScreenToContent(s));
            if (_measurePts.Count == 2) UpdateMeasureReadout();
            RedrawMeasure();
            e.Handled = true;
            return;
        }
        // 平移：中鍵 或 空白+左鍵
        if (pt.Properties.IsMiddleButtonPressed || (_space && pt.Properties.IsLeftButtonPressed))
        {
            _panning = true; _panLast = s;
            e.Pointer.Capture(_viewport);
            e.Handled = true;
            return;
        }
        // 點大圖缺陷框 → 選取（不平移大圖，捲動縮圖牆 + 高亮）
        if (pt.Properties.IsLeftButtonPressed)
        {
            int hit = HitTestDefect(ScreenToContent(s));
            if (hit >= 0) NavTo(hit, center: false);
        }
    }

    // 找出含此 content 點的缺陷索引（線性掃描，點擊頻率低可接受）
    private int HitTestDefect(Point c)
    {
        if (Vm is not { } vm) return -1;
        for (int i = 0; i < vm.NavDefects.Count; i++)
        {
            var d = vm.NavDefects[i];
            if (c.X >= d.XMin && c.X <= d.XMax && c.Y >= d.YMin && c.Y <= d.YMax) return i;
        }
        return -1;
    }

    private void OnMoved(object? sender, PointerEventArgs e)
    {
        var s = e.GetPosition(_viewport);
        if (_panning)
        {
            _offX += s.X - _panLast.X;
            _offY += s.Y - _panLast.Y;
            _panLast = s;
            ApplyTransform();
        }
        UpdateAxisValue(s);
    }

    private void OnReleased(object? sender, PointerReleasedEventArgs e)
    {
        if (_panning) { _panning = false; e.Pointer.Capture(null); }
    }

    private void OnKeyDown(object? sender, KeyEventArgs e)
    {
        if (e.Key == Key.Space) _space = true;
        else if (e.Key == Key.M) _measureKey = true;
        else if (e.Key == Key.F) Fit();
        else if (e.Key == Key.Right && Vm is { NavDefects.Count: > 0 } vmr)
        { NavTo(Math.Min((vmr.SelectedDefectIndex < 0 ? -1 : vmr.SelectedDefectIndex) + 1, vmr.NavDefects.Count - 1), center: true); e.Handled = true; }
        else if (e.Key == Key.Left && Vm is { NavDefects.Count: > 0 } vml)
        { NavTo(Math.Max((vml.SelectedDefectIndex < 0 ? 1 : vml.SelectedDefectIndex) - 1, 0), center: true); e.Handled = true; }
    }

    private void OnKeyUp(object? sender, KeyEventArgs e)
    {
        if (e.Key == Key.Space) _space = false;
        else if (e.Key == Key.M) _measureKey = false;
    }

    // ===== 缺陷導航 =====
    private void OnThumbSelectionChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (_navigating) return;
        if (_thumbList?.SelectedItem is DefectThumb t) NavTo(t.Index, center: true);
    }

    // 設定當前缺陷：高亮 overlay + 更新索引/狀態；center=true 則大圖跳轉置中(~5x)，並捲動縮圖
    private void NavTo(int idx, bool center)
    {
        if (Vm is not { } vm || idx < 0 || idx >= vm.NavDefects.Count) return;
        vm.SelectedDefectIndex = idx;
        if (_defectOverlay != null) _defectOverlay.SelectedIndex = idx;

        if (center) CenterOnDefect(vm.NavDefects[idx]);
        ScrollThumbTo(idx);
    }

    private void CenterOnDefect(Models.DefectModel d)
    {
        if (_viewport is null) return;
        double vw = _viewport.Bounds.Width, vh = _viewport.Bounds.Height;
        if (vw < 2 || vh < 2) return;
        _scale = Math.Clamp(5.0, Math.Min(0.1, _fitScale), 10.0);
        double cx = d.GlobalPosX, cy = d.GlobalPosY;
        _offX = vw / 2 - cx * _scale;
        _offY = vh / 2 - cy * _scale;
        ApplyTransform();
    }

    private void ScrollThumbTo(int idx)
    {
        if (_thumbList is null || Vm is not { } vm) return;
        if (idx < vm.Thumbs.Count)
        {
            _navigating = true;
            _thumbList.SelectedItem = vm.Thumbs[idx];
            _thumbList.ScrollIntoView(vm.Thumbs[idx]);
            _navigating = false;
        }
    }

    // ===== StatusBar：Axis（原始像素座標）+ Value（灰階值）=====
    private void UpdateAxisValue(Point screen)
    {
        if (Vm is not { } vm) return;
        var c = ScreenToContent(screen);
        int ix = (int)Math.Floor(c.X), iy = (int)Math.Floor(c.Y);
        if (vm.PixelData is { } px && ix >= 0 && iy >= 0 && ix < vm.ImageWidth && iy < vm.ImageHeight)
        {
            vm.AxisText = $"| Axis : ({ix},{iy})";
            vm.ValueText = $"| Value : {px[iy * vm.ImageWidth + ix]}";
        }
        else { vm.AxisText = "| Axis : -"; vm.ValueText = "| Value : -"; }
    }

    // ===== 量測：Region 格分開顯示 dx / dy / 歐氏 =====
    private void UpdateMeasureReadout()
    {
        if (Vm is null || _measurePts.Count < 2) return;
        double dx = Math.Abs(_measurePts[1].X - _measurePts[0].X);
        double dy = Math.Abs(_measurePts[1].Y - _measurePts[0].Y);
        double d = Math.Sqrt(dx * dx + dy * dy);
        Vm.RegionText = $"| dx={dx:F0} dy={dy:F0} d={d:F1}";
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
            if (prev is { } p)
                _measureOverlay.Children.Add(new Line { StartPoint = p, EndPoint = sp, Stroke = brush, StrokeThickness = 1.5 });
            prev = sp;
        }
    }

    // 檔案選取（StorageProvider，需 TopLevel）注入 ViewModel，保持 VM 平台無關。
    private void WireFilePicker()
    {
        if (Vm is not { } vm) return;
        vm.FilePicker = async () =>
        {
            var top = TopLevel.GetTopLevel(this);
            if (top is null) return null;
            var files = await top.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
            {
                Title = "選擇影像", AllowMultiple = false,
                FileTypeFilter = new[]
                {
                    new FilePickerFileType("影像") { Patterns = new[] { "*.tif", "*.tiff", "*.png", "*.bmp", "*.jpg" } }
                },
            });
            return files.Count > 0 ? files[0].TryGetLocalPath() : null;
        };
    }
}
