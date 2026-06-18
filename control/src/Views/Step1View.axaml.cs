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
    private bool _maybePan;          // 左鍵已按下、尚未決定是「點選缺陷」還是「平移」
    private Point _pressPos;         // 按下時的螢幕座標（判拖曳閾值 + 點選命中）
    private const double PanThreshold = 4;   // px：移動超過視為平移，否則視為點選
    private bool _space, _measureKey;
    private readonly List<Point> _measurePts = new();   // content 座標

    // #8 視覺 ROI 框選
    private Canvas? _roiOverlay;
    private Button? _btnRoi;
    private bool _roiMode, _roiDragging;
    private Point _roiStart, _roiCur;                   // content 座標
    private Models.ZoneSettingModel? _roiZone;          // 目前監聽的 PrimaryZone

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
        _roiOverlay = this.FindControl<Canvas>("RoiOverlay");
        _btnRoi = this.FindControl<Button>("BtnRoi");
        if (_btnRoi != null) _btnRoi.Click += (_, _) => ToggleRoiMode();
        var btnRoiClear = this.FindControl<Button>("BtnRoiClear");
        if (btnRoiClear != null) btnRoiClear.Click += (_, _) => ClearRoi();

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
            vm.RefreshOverlayRequested -= RebuildOverlay;
            vm.RefreshOverlayRequested += RebuildOverlay;
            vm.Store.RecipeReloaded -= HookRoiZone;
            vm.Store.RecipeReloaded += HookRoiZone;
        }
        HookRoiZone();   // 綁目前 PrimaryZone 的變更 → 重畫 ROI
        // 重新開啟視窗時 VM 仍持有缺陷資料 → 從記憶體重建大圖標示（不需重跑 Test）
        Dispatcher.UIThread.Post(RebuildOverlay);
    }

    // 從 VM 當前缺陷清單重建大圖圓圈標示（開窗自動重建 + 手動「刷新標示」鈕）
    private void RebuildOverlay()
    {
        if (_defectOverlay is null || Vm is null) return;
        _defectOverlay.SetDefects(Vm.NavDefects);
        _defectOverlay.SelectedIndex = Vm.SelectedDefectIndex;
        _defectOverlay.StrokeScale = _scale;
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
        RedrawRoi();
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
        // #8 ROI 框選模式：左鍵拖矩形
        if (_roiMode && pt.Properties.IsLeftButtonPressed)
        {
            _roiDragging = true;
            _roiStart = ScreenToContent(s);
            _roiCur = _roiStart;
            e.Pointer.Capture(_viewport);
            RedrawRoi();
            e.Handled = true;
            return;
        }
        // 一般模式左鍵：先記錄，暫不決定。OnMoved 超過閾值→平移；OnReleased 未拖動→點選缺陷。
        // → 觸控板單指拖 / 滑鼠左鍵拖皆可平移，免鍵盤免中鍵；輕點仍選缺陷。
        if (pt.Properties.IsLeftButtonPressed)
        {
            _maybePan = true;
            _pressPos = s;
            _panLast = s;
            e.Pointer.Capture(_viewport);
            e.Handled = true;
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
        // 左鍵按住後移動超過閾值 → 由「待定」升級為平移（從當前點起算，避免跳動）
        if (_maybePan && !_panning)
        {
            double mdx = s.X - _pressPos.X, mdy = s.Y - _pressPos.Y;
            if (mdx * mdx + mdy * mdy >= PanThreshold * PanThreshold) { _panning = true; _panLast = s; }
        }
        if (_panning)
        {
            _offX += s.X - _panLast.X;
            _offY += s.Y - _panLast.Y;
            _panLast = s;
            ApplyTransform();
        }
        else if (_roiDragging)
        {
            _roiCur = ScreenToContent(s);
            RedrawRoi();
        }
        UpdateAxisValue(s);
    }

    private void OnReleased(object? sender, PointerReleasedEventArgs e)
    {
        if (_panning) { _panning = false; _maybePan = false; e.Pointer.Capture(null); }
        else if (_roiDragging) { _roiDragging = false; e.Pointer.Capture(null); CommitRoi(); }
        else if (_maybePan)   // 按下後幾乎沒動 = 點選 → 命中缺陷則選取
        {
            _maybePan = false;
            e.Pointer.Capture(null);
            int hit = HitTestDefect(ScreenToContent(_pressPos));
            if (hit >= 0) NavTo(hit, center: false);
        }
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

    // ===== #8 視覺 ROI 框選（在影像上拖矩形 → 寫回 RecipeStore.PrimaryZone）=====
    private void ToggleRoiMode()
    {
        _roiMode = !_roiMode;
        if (_btnRoi != null)
            _btnRoi.Background = _roiMode ? new SolidColorBrush(Color.Parse("#9CCC65")) : null;
        if (Vm != null) Vm.RegionText = _roiMode ? "| ROI 框選中：拖矩形" : "";
    }

    private void ClearRoi()
    {
        if (Vm?.Store.PrimaryZone is { } z) { z.StartX = -1; z.StartY = -1; z.EndX = -1; z.EndY = -1; }
        if (Vm != null) Vm.RegionText = "| ROI 全幅(-1)";
        RedrawRoi();
    }

    // 綁目前 PrimaryZone 的 PropertyChanged → 數值改動(含 ZoneParamEditor 編輯)即重畫 ROI
    private void HookRoiZone()
    {
        if (_roiZone != null) _roiZone.PropertyChanged -= OnRoiZoneChanged;
        _roiZone = Vm?.Store.PrimaryZone;
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
        if (Vm is not { } vm || vm.Store.PrimaryZone is not { } z) return;
        int iw = Math.Max(1, vm.ImageWidth), ih = Math.Max(1, vm.ImageHeight);
        int x0 = (int)Math.Round(Math.Min(_roiStart.X, _roiCur.X));
        int y0 = (int)Math.Round(Math.Min(_roiStart.Y, _roiCur.Y));
        int x1 = (int)Math.Round(Math.Max(_roiStart.X, _roiCur.X));
        int y1 = (int)Math.Round(Math.Max(_roiStart.Y, _roiCur.Y));
        x0 = Math.Clamp(x0, 0, iw); x1 = Math.Clamp(x1, 0, iw);
        y0 = Math.Clamp(y0, 0, ih); y1 = Math.Clamp(y1, 0, ih);
        if (x1 - x0 < 2 || y1 - y0 < 2) { RedrawRoi(); return; }   // 太小 → 忽略
        z.StartX = x0; z.StartY = y0; z.EndX = x1; z.EndY = y1;     // 寫回共用配方(單一資料來源)
        if (Vm != null) Vm.RegionText = $"| ROI ({x0},{y0})-({x1},{y1})";
        RedrawRoi();
    }

    private void RedrawRoi()
    {
        if (_roiOverlay is null) return;
        _roiOverlay.Children.Clear();
        if (Vm?.Store.PrimaryZone is { } z && z.StartX >= 0 && z.StartY >= 0 && z.EndX > z.StartX && z.EndY > z.StartY)
            DrawRoiRect(z.StartX, z.StartY, z.EndX, z.EndY, "#00B0FF");   // 已設 ROI = 藍框
        if (_roiDragging)
            DrawRoiRect(Math.Min(_roiStart.X, _roiCur.X), Math.Min(_roiStart.Y, _roiCur.Y),
                        Math.Max(_roiStart.X, _roiCur.X), Math.Max(_roiStart.Y, _roiCur.Y), "#FFC400"); // 拖曳中 = 黃框
    }

    private void DrawRoiRect(double x0, double y0, double x1, double y1, string color)
    {
        var p0 = ContentToScreen(new Point(x0, y0));
        var p1 = ContentToScreen(new Point(x1, y1));
        var rect = new Rectangle
        {
            Width = Math.Abs(p1.X - p0.X),
            Height = Math.Abs(p1.Y - p0.Y),
            Stroke = new SolidColorBrush(Color.Parse(color)),
            StrokeThickness = 2,
            Fill = Brushes.Transparent,
        };
        Canvas.SetLeft(rect, Math.Min(p0.X, p1.X));
        Canvas.SetTop(rect, Math.Min(p0.Y, p1.Y));
        _roiOverlay!.Children.Add(rect);
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
