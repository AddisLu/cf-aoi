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
///
/// ── 座標系統（讀本檔前必懂）──────────────────────────────────────────────
/// 全檔只有兩種座標，變數命名一律 s=screen、c=content：
///   • **content 座標** = 影像像素座標（0..ImageWidth-1 / 0..ImageHeight-1）。ROI(StartX/EndY…)、
///     缺陷(GlobalPosX/Y)、量測點都存這個座標系 —— 與縮放無關，直接就是配方裡的值。
///   • **screen 座標** = Viewport 內的裝置無關像素（滑鼠事件給的就是這個）。
/// 兩者只差一個仿射變換 `CurrentMatrix()`＝縮放 `_scale` + 平移 `(_offX,_offY)`（無旋轉/傾斜）：
///     screen = content × _scale + (_offX, _offY)          ← ContentToScreen
///     content = (screen − (_offX,_offY)) / _scale          ← ScreenToContent（實作走反矩陣）
/// 這個矩陣同時**套在 ContentRoot 的 RenderTransform 上**（見 ApplyTransform），所以影像與其內的
/// DefectOverlay 由 Avalonia 自動變換；而 RoiOverlay/MeasureOverlay 是**畫在未變換的圖層**，
/// 故它們每次都要自己呼叫 ContentToScreen 換算後重畫（RedrawRoi/RedrawMeasure）——
/// 這樣做的好處是 ROI 邊框與把手線寬不會被縮放拉粗。
///
/// ── 滑鼠模式狀態機（三模式互斥，優先序見 OnPressed）───────────────────────
///   量測(按住 M) ＞ 平移(中鍵 或 空白鍵+左鍵) ＞ 框 ROI(_roiMode 開 且 左鍵) ＞ 點選缺陷(純左鍵)
/// 同一時間最多一個模式在進行中，各自有旗標：_measureKey / _panning(_maybePan 為門檻前的候選)
/// / _roiDragging(拉新框) 或 _roiHandle>=0(拖既有把手)。「純左鍵」預設進 _maybePan：
/// 移動超過 PanThreshold 才升級為平移，否則放開時視為「點選缺陷」——一個手勢兼顧拖曳與點選。
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

    // 視埠變換狀態（= CurrentMatrix 的全部參數）：
    //   _scale    目前縮放倍率（1 = 一個影像像素畫成一個螢幕像素）
    //   _fitScale 「整張塞滿視埠」時的倍率，由 Fit() 算；當作縮小下限的參考值
    //   _offX/_offY 平移量（screen 座標；影像左上角被畫在螢幕的哪裡）
    //   _fitted   是否已做過至少一次 Fit——用於「視埠尺寸變了但使用者還沒自己縮放過就自動重 Fit」
    private double _scale = 1, _fitScale = 1, _offX, _offY;
    // 模式旗標（互斥語意見類別註解）：_panning 平移中／_maybePan 左鍵按下但還沒超過位移門檻（可能只是點選）／
    // _space 空白鍵按住（左鍵改當平移）／_measureKey M 鍵按住（左鍵改當量測取點）
    private bool _fitted, _panning, _maybePan, _space, _measureKey;
    private Point _panLast, _pressPos;
    // 左鍵按下後位移超過此距離(screen px) 才算「拖曳平移」，否則放開時算「點選缺陷」——避免手抖就選不到缺陷
    private const double PanThreshold = 4;
    // 量 Pitch 的兩個取樣點（content 座標）；滿 2 點即算 dx/dy/距離，第 3 次點擊重新開始
    private readonly List<Point> _measurePts = new();

    // ROI 編輯狀態：_roiMode 由「框 ROI」鈕切換（關閉時左鍵仍是平移/點選缺陷，不會誤改配方）；
    // _roiDragging 拉全新矩形中，_roiStart/_roiCur 為兩對角（content 座標）
    private bool _roiMode, _roiDragging;
    private Point _roiStart, _roiCur;
    // 目前已訂閱 PropertyChanged 的 zone（= 上一個 EditZone）。留著才能在切換 ROI 時正確退訂，
    // 否則舊 zone 的事件會一直重畫本控制項（記憶體與重繪雙重浪費）。
    private ZoneSettingModel? _roiZone;
    // 正在拖曳的把手索引 0..7（對應關係見 RoiHandlePoints）；-1 = 沒有在拖把手
    private int _roiHandle = -1;
    // HandleHit = 命中半徑(screen px，比視覺大一點好抓)；HandleSize = 把手方塊邊長
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

    /// <summary>StyledProperty 變更分派。多處用 <c>Dispatcher.UIThread.Post</c> 延到下一輪，
    /// 是因為屬性到達時子控制項的版面/綁定可能還沒就緒（Bounds 為 0 會讓 Fit 算出錯誤倍率）。
    /// ResultVersion 是 VM 每次分析後 +1 的「重畫信號」——缺陷清單物件可能同參照，只靠 Defects
    /// 的變更通知不一定會觸發，故另設一個必變的計數器。</summary>
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
    // 只有縮放 + 平移（無旋轉/傾斜）：screen = content*_scale + (_offX,_offY)。
    // Avalonia Matrix 建構子順序為 (m11,m12,m21,m22,m31,m32) → 對角放 _scale、第三列放平移。
    private Matrix CurrentMatrix() => new(_scale, 0, 0, _scale, _offX, _offY);
    // 反矩陣換算：滑鼠 screen 座標 → 影像像素座標。TryInvert 只在 _scale=0 這種退化情況失敗，
    // 此時原樣回傳（不丟例外）—— 讀值會不準但不會讓整個滑鼠事件炸掉。
    private Point ScreenToContent(Point s) => CurrentMatrix().TryInvert(out var inv) ? inv.Transform(s) : s;
    private Point ContentToScreen(Point c) => CurrentMatrix().Transform(c);

    /// <summary>把目前 _scale/_offX/_offY 套到畫面：ContentRoot 走 RenderTransform（影像+缺陷圈自動跟著），
    /// 未變換的 ROI/量測圖層則整層重畫（線寬因此不隨縮放變粗，見類別註解）。</summary>
    private void ApplyTransform()
    {
        if (_content != null) _content.RenderTransform = new MatrixTransform(CurrentMatrix());
        // 缺陷圈畫在已變換圖層 → 要把倍率告訴它，內部才能反算出「螢幕上維持固定粗細」的線寬
        if (_defectOverlay != null) _defectOverlay.StrokeScale = _scale;
        ZoomText = $"| Zoom : {_scale:F2}x";
        RedrawMeasure();
        RedrawRoi();
    }

    /// <summary>整張塞滿視埠並置中：取寬高比例的較小者當倍率（不裁切），再把剩餘空白平均分到兩側。
    /// 視埠尺寸為 0（版面還沒量測完）時直接放棄，等 SizeChanged 再進來一次。</summary>
    public void Fit()
    {
        if (_viewport is null) return;
        double vw = _viewport.Bounds.Width, vh = _viewport.Bounds.Height;
        double iw = Math.Max(1, ImageWidth), ih = Math.Max(1, ImageHeight);
        if (vw < 2 || vh < 2) return;
        _fitScale = Math.Min(vw / iw, vh / ih);   // 取小者 = 長邊剛好塞滿、短邊留白（contain 而非 cover）
        _scale = _fitScale;
        _offX = (vw - iw * _scale) / 2;           // 置中：剩餘空白平分左右/上下
        _offY = (vh - ih * _scale) / 2;
        _fitted = true;
        ApplyTransform();
    }

    /// <summary>滾輪縮放，**以游標為錨點**（游標下的那個影像像素縮放前後停在原地）：
    /// 先記下游標對應的 content 點 c，改倍率後反推平移量使 ContentToScreen(c) 仍等於 s。
    /// 沒有這個錨定，放大時目標會往畫面外跑，8192×5000 的圖幾乎不可能對到想看的位置。</summary>
    private void OnWheel(object? sender, PointerWheelEventArgs e)
    {
        if (Source is null) return;
        var s = e.GetPosition(_viewport);
        var c = ScreenToContent(s);               // 錨點（content 座標，縮放後不變）
        double factor = e.Delta.Y > 0 ? 1.15 : 1 / 1.15;   // 每格 ±15%，正反向互為倒數 → 來回滾可回到原倍率
        // 縮小下限取 min(0.1, _fitScale)：超寬幅線掃圖的 _fitScale 可能遠小於 0.1，
        // 若硬性夾在 0.1 會導致「Fit 後還能再放大回不去」的怪現象。
        double min = Math.Min(0.1, _fitScale), max = 10.0;
        double ns = Math.Clamp(_scale * factor, min, max);
        _offX = s.X - c.X * ns;                   // 由 screen = content*ns + off 反解 off
        _offY = s.Y - c.Y * ns;
        _scale = ns;
        ApplyTransform();
        e.Handled = true;
    }

    /// <summary>模式仲裁入口——**由上而下的判斷順序即優先序**（量測 ＞ 平移 ＞ 框 ROI ＞ 點選缺陷），
    /// 命中即 return，確保三模式互斥。先 Focus 視埠，否則空白/M/方向鍵這些 KeyDown 收不到。</summary>
    private void OnPressed(object? sender, PointerPressedEventArgs e)
    {
        _viewport?.Focus();
        var pt = e.GetCurrentPoint(_viewport);
        var s = pt.Position;

        // ① 量測（按住 M）：最高優先，滿 2 點就算 dx/dy/距離；第 3 次點擊重開一組
        if (_measureKey && pt.Properties.IsLeftButtonPressed)
        {
            if (_measurePts.Count >= 2) _measurePts.Clear();
            _measurePts.Add(ScreenToContent(s));
            if (_measurePts.Count == 2) UpdateMeasureReadout();
            RedrawMeasure();
            e.Handled = true;
            return;
        }
        // ② 明示平移（中鍵，或空白鍵+左鍵）：立刻進入拖曳，不必等位移門檻
        if (pt.Properties.IsMiddleButtonPressed || (_space && pt.Properties.IsLeftButtonPressed))
        {
            _panning = true; _panLast = s;
            e.Pointer.Capture(_viewport);   // 抓住游標 → 拖出視埠外仍收得到 Moved/Released，不會卡在拖曳中
            e.Handled = true;
            return;
        }
        // ③ 框 ROI（僅當「框 ROI」鈕已開）：先試把手（精修既有框），沒中才是拉一個全新矩形
        if (_roiMode && pt.Properties.IsLeftButtonPressed)
        {
            int h = HitTestRoiHandle(s);
            if (h >= 0) { _roiHandle = h; e.Pointer.Capture(_viewport); e.Handled = true; return; }
            _roiDragging = true; _roiStart = ScreenToContent(s); _roiCur = _roiStart;
            e.Pointer.Capture(_viewport); RedrawRoi(); e.Handled = true;
            return;
        }
        // ④ 純左鍵：暫不決定——先記為 _maybePan，等 Moved 超過門檻才升級平移，
        //    否則 Released 時視為「點選缺陷」（見 OnReleased）。一個手勢同時支援拖曳與點選。
        if (pt.Properties.IsLeftButtonPressed)
        {
            _maybePan = true; _pressPos = s; _panLast = s;
            e.Pointer.Capture(_viewport); e.Handled = true;
        }
    }

    /// <summary>命中測試：content 座標落在哪個缺陷的外接盒內（回索引，無則 -1）。
    /// 由前往後找 → 重疊時選到清單較前者；缺陷數千顆時仍是 O(n)，但只在放開滑鼠時跑一次。</summary>

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

    /// <summary>拖曳分派：先判斷 _maybePan 是否升級為平移，再依當前模式更新。
    /// 無論哪個模式最後都會更新 Axis/Value 讀數（游標所在像素座標與灰階值）。</summary>
    private void OnMoved(object? sender, PointerEventArgs e)
    {
        var s = e.GetPosition(_viewport);
        // 位移門檻判定（比較平方值省開根號）：超過才從「可能是點選」升級為「確定是拖曳平移」
        if (_maybePan && !_panning)
        {
            double mdx = s.X - _pressPos.X, mdy = s.Y - _pressPos.Y;
            if (mdx * mdx + mdy * mdy >= PanThreshold * PanThreshold) { _panning = true; _panLast = s; }
        }
        if (_panning)
        {
            // 平移直接累加 screen 位移到 _offX/_offY（與 _scale 無關 → 手感恆為 1:1 跟手）
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

    /// <summary>結束手勢並歸還游標捕捉。順序與 OnPressed 對稱：
    /// 拖曳過的三種模式各自收尾；最後一支 _maybePan（= 從按下到放開都沒超過門檻）才解讀為「點選缺陷」。</summary>
    private void OnReleased(object? sender, PointerReleasedEventArgs e)
    {
        if (_panning) { _panning = false; _maybePan = false; e.Pointer.Capture(null); }
        else if (_roiDragging) { _roiDragging = false; e.Pointer.Capture(null); CommitRoi(); }   // 放開才寫回 zone
        else if (_roiHandle >= 0) { _roiHandle = -1; e.Pointer.Capture(null); }                  // 把手拖曳是即時寫回，此處僅收旗標
        else if (_maybePan)
        {
            // 沒移動過 → 視為點選：用「按下時」的位置做命中測試（非放開位置，容忍最後 1~2px 抖動）
            _maybePan = false; e.Pointer.Capture(null);
            int hit = HitTestDefect(ScreenToContent(_pressPos));
            if (hit >= 0) SelectDefect(hit, center: false);   // center:false = 點選不搬動視角，使用者正看著它
        }
    }

    /// <summary>鍵盤：Space/M 是**模式修飾鍵**（按住期間左鍵改變意義，見類別註解的優先序）；
    /// F=Fit；←/→ 在缺陷清單間導航並置中。修飾鍵在 OnKeyUp 復原，故不會卡在某模式。</summary>
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

    /// <summary>把某缺陷擺到視埠正中央並放大到可辨識倍率（縮圖牆點選 / ←→ 導航用）。
    /// 平移量由「螢幕中心 = 缺陷 content 座標經變換後的位置」反解而得。</summary>
    private void CenterOnDefect(DefectModel d)
    {
        if (_viewport is null) return;
        double vw = _viewport.Bounds.Width, vh = _viewport.Bounds.Height;
        if (vw < 2 || vh < 2) return;
        // 導航固定跳到 5x（小缺陷在 Fit 倍率下只有 1px，不放大等於沒跳）。
        // ⚠️ 已知限制（docs/code_review_20260802.md K20）：Clamp 的 value 是常數 5.0、下限恆 ≤0.1，
        // 故夾制無實際作用＝恆為 5.0；若日後想改成「不低於 fit 倍率」需重寫此行。
        _scale = Math.Clamp(5.0, Math.Min(0.1, _fitScale), 10.0);
        _offX = vw / 2 - d.GlobalPosX * _scale;
        _offY = vh / 2 - d.GlobalPosY * _scale;
        ApplyTransform();
    }

    /// <summary>游標所在像素的座標與灰階值讀數。Floor（非 Round）才能讓「一個像素方格」對應唯一整數座標。
    /// PixelData 為 null = 遠端影像模式（全解析度像素留在 IP 機、Mac 只有預覽）→ 誠實顯示 "-"，不猜值。</summary>
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

    /// <summary>換綁 EditZone：**先退訂舊 zone 再訂新 zone**（漏退訂會累積事件，切 ROI 越多重畫越多次）。
    /// 訂閱目的：ROI 座標也可能從別處改（下方數值 NumericUpDown、ZoneParamEditor 批次套用、
    /// 配方重載）——收到通知才能把框重畫到新位置，達成「數值↔影像雙向同步」。</summary>
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

    /// <summary>把拖出來的矩形寫回 EditZone（放開滑鼠才做，拖曳過程只畫預覽框）。
    /// 用 Min/Max 正規化兩對角 → 不論從哪個方向拉都得到 Start&lt;End；小於 2px 視為誤觸不寫入
    /// （否則手滑一下就把整個 ROI 縮成一點，比不寫入更難救）。</summary>
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

    /// <summary>ROI 是否「畫得出來」：-1 = 全幅（legacy 慣例，不畫框）、翻面/零面積也不畫。</summary>
    private static bool ZoneValid(ZoneSettingModel? z)
        => z is not null && z.StartX >= 0 && z.StartY >= 0 && z.EndX > z.StartX && z.EndY > z.StartY;

    /// <summary>重畫 ROI 圖層（整層 Clear 再建，數量最多數十個、成本可忽略）。
    /// 疊放順序刻意：其他 ROI(灰) → 選中 ROI(藍) → 拖曳預覽(黃) / 把手，確保正在編的那個永遠在最上層看得見。</summary>
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

    /// <summary>
    /// 產生 8 個把手的 **screen 座標**，索引順序為「左上起、順時針」：
    /// <code>
    ///   0 ── 1 ── 2        0=左上  1=上中  2=右上
    ///   │         │        3=右中          7=左中
    ///   7         3        4=右下  5=下中  6=左下
    ///   │         │
    ///   6 ── 5 ── 4
    /// </code>
    /// 這個順序是 <see cref="UpdateRoiHandle"/> 判斷「要改哪幾條邊」的依據，改動順序等同改動拖曳語意。
    /// 回傳 screen 座標而非 content：把手是畫在未變換圖層，且命中半徑要用螢幕距離才符合手感。
    /// </summary>
    private Point[] RoiHandlePoints(ZoneSettingModel z)
    {
        var tl = ContentToScreen(new Point(z.StartX, z.StartY));
        var br = ContentToScreen(new Point(z.EndX, z.EndY));
        double mx = (tl.X + br.X) / 2, my = (tl.Y + br.Y) / 2;   // 邊中點
        return new[]
        {
            new Point(tl.X, tl.Y), new Point(mx, tl.Y), new Point(br.X, tl.Y), new Point(br.X, my),
            new Point(br.X, br.Y), new Point(mx, br.Y), new Point(tl.X, br.Y), new Point(tl.X, my),
        };
    }

    /// <summary>游標是否落在某把手的命中半徑內（回索引 0..7，無則 -1）。用平方距離比較省開根號。</summary>

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

    /// <summary>
    /// 拖曳把手即時改寫 EditZone 的邊界（content 座標，直接就是配方值 → 屬性變更會讓 27/34 列表單同步）。
    /// 「這個把手要動哪幾條邊」由索引集合決定（對照 <see cref="RoiHandlePoints"/> 的方位圖）：
    ///   left  = {0,6,7}（左上/左下/左中）  right  = {2,3,4}（右上/右中/右下）
    ///   top   = {0,1,2}（左上/上中/右上）  bottom = {4,5,6}（右下/下中/左下）
    /// 角把手同時落在兩個集合 → 一次改兩條邊；邊中點把手只落在一個集合 → 只改該邊。
    /// 兩道保護：① Clamp 到影像範圍，不讓 ROI 跑出畫面外；
    ///           ② Min/Max 保留至少 2px 寬高，避免拖過頭把矩形翻面（StartX>EndX 會讓 IP 端切出負尺寸）。
    /// </summary>
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
