using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using CfAoiControl.ViewModels;

namespace CfAoiControl.Views;

public partial class MainWindow : Window
{
    private Window? _algoWin;

    public MainWindow()
    {
        InitializeComponent();
        // Ctrl+F：切換進階按鈕顯示（沿用舊版慣例）
        KeyDown += (_, e) =>
        {
            if (e.Key == Key.F && e.KeyModifiers.HasFlag(KeyModifiers.Control)
                && DataContext is MainWindowViewModel vm)
                vm.ToggleAdvancedCommand.Execute(null);
        };
        var btn = this.FindControl<Button>("BtnOpenAlgoTest");
        if (btn != null) btn.Click += OpenAlgoTest;
        var btn2 = this.FindControl<Button>("BtnOpenParamEditor");
        if (btn2 != null) btn2.Click += OpenParamEditor;
    }

    private Window? _paramWin;
    // 開「配方編輯」(對應 legacy frmIpParamEditor)，獨立視窗 1280×797
    private void OpenParamEditor(object? sender, RoutedEventArgs e)
    {
        if (DataContext is not MainWindowViewModel vm) return;
        if (_paramWin is { IsVisible: true } w) { w.Activate(); return; }
        _paramWin = new Window
        {
            Title = "Ip Params Editor",
            Width = 1280, Height = 797,
            FontFamily = new Avalonia.Media.FontFamily("Arial"),
            Content = new ZoneParamEditorView { DataContext = vm.ZoneEditor },
        };
        _paramWin.Closed += (_, _) => _paramWin = null;
        _paramWin.Show(this);
    }

    // 開「離線分析工具」(對應 legacy frmAlgorithmTestTools)，獨立視窗 1064×681
    private void OpenAlgoTest(object? sender, RoutedEventArgs e)
    {
        if (DataContext is not MainWindowViewModel vm) return;
        if (_algoWin is { IsVisible: true } w) { w.Activate(); return; }
        _algoWin = new Window
        {
            Title = "Algorithm Test Tools",
            Width = 1064, Height = 681,
            FontFamily = new Avalonia.Media.FontFamily("Arial"),
            Content = new Step1View { DataContext = vm.Step1 },
        };
        _algoWin.Closed += (_, _) => _algoWin = null;
        _algoWin.Show(this);
    }
}
