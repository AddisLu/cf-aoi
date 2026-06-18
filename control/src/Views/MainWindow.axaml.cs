using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Markup.Xaml.Styling;

namespace CfAoiControl.Views;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();

        // A/B 外觀切換：抽換 Application.Resources 第一個 MergedDictionary（ThemeConsole/ThemeLab）。
        // 所有 View 以 DynamicResource 取 token → 即時換膚。
        if (this.FindControl<Button>("BtnVariantA") is { } a) a.Click += (_, _) => SetVariant(console: true);
        if (this.FindControl<Button>("BtnVariantB") is { } b) b.Click += (_, _) => SetVariant(console: false);
    }

    private void SetVariant(bool console)
    {
        var app = Application.Current;
        if (app is null) return;

        var uri = new Uri(console
            ? "avares://CfAoiControl/Styles/ThemeConsole.axaml"
            : "avares://CfAoiControl/Styles/ThemeLab.axaml");
        var theme = new ResourceInclude(uri) { Source = uri };

        var md = app.Resources.MergedDictionaries;
        if (md.Count > 0) md[0] = theme; else md.Add(theme);

        // 視窗背景非 token 控制項 → 直接更新；分段鈕 active 狀態
        if (this.FindControl<Button>("BtnVariantA") is { } a) a.Classes.Set("active", console);
        if (this.FindControl<Button>("BtnVariantB") is { } b) b.Classes.Set("active", !console);
    }
}
