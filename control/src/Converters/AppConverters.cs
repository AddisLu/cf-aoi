using System;
using System.Globalization;
using Avalonia.Data.Converters;
using Avalonia.Media;
using CfAoiControl.Services;

namespace CfAoiControl.Converters;

/// <summary>bool → 綠/紅（連線狀態燈）。</summary>
public sealed class BoolToGreenRedConverter : IValueConverter
{
    public static readonly BoolToGreenRedConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c)
        => new SolidColorBrush(v is true ? Color.Parse("#2ecc71") : Color.Parse("#e74c3c"));
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>LogLevel → 文字顏色（A5：紅錯/藍警/黑訊息）。</summary>
public sealed class LogLevelToBrushConverter : IValueConverter
{
    public static readonly LogLevelToBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c) => v switch
    {
        LogLevel.Error   => new SolidColorBrush(Colors.Red),
        LogLevel.Warning => new SolidColorBrush(Colors.Blue),
        _                => new SolidColorBrush(Colors.Black),
    };
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>Sort 可執行 → Orange #FFA500，否則 DarkGray #A9A9A9（對應 legacy btnRun 狀態色）。</summary>
public sealed class CanSortToBrushConverter : IValueConverter
{
    public static readonly CanSortToBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c)
        => new SolidColorBrush(Color.Parse(v is true ? "#FFA500" : "#A9A9A9"));
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>達檢測上限 → 紅字，否則黑字（DefectCnt 警示）。</summary>
public sealed class AtCapToBrushConverter : IValueConverter
{
    public static readonly AtCapToBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c)
        => new SolidColorBrush(v is true ? Colors.Red : Colors.Black);
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>缺陷 Type → 顏色（亮=紅、暗=藍）。</summary>
public sealed class DefectTypeToBrushConverter : IValueConverter
{
    public static readonly DefectTypeToBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c)
        => new SolidColorBrush((v as string)?.Contains("Bright") == true ? Colors.Red : Colors.Blue);
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>分類 → 邊框色（TrueDefect 紅 / Particle 綠 / 未分類 灰）。</summary>
public sealed class ClassToBorderBrushConverter : IValueConverter
{
    public static readonly ClassToBorderBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c) => (v as string) switch
    {
        "TrueDefect" => new SolidColorBrush(Color.Parse("#FF0000")),
        "Particle"   => new SolidColorBrush(Color.Parse("#00C000")),
        _            => new SolidColorBrush(Color.Parse("#A0A0A0")),
    };
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>bool 反相（true→false）。用於 IsVisible 切換。</summary>
public sealed class InverseBoolConverter : IValueConverter
{
    public static readonly InverseBoolConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c) => v is not true;
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => v is not true;
}
