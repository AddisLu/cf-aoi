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

/// <summary>缺陷 Type → 顏色（亮=紅、暗=藍）。</summary>
public sealed class DefectTypeToBrushConverter : IValueConverter
{
    public static readonly DefectTypeToBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c)
        => new SolidColorBrush((v as string)?.Contains("Bright") == true ? Colors.Red : Colors.Blue);
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}
