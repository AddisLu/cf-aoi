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

/// <summary>相機狀態 → 色碼（已綁定綠 / 待綁定琥珀 / 離線灰，對齊 mockup 語意）。接受 CamStatusKind 或其字串。</summary>
public sealed class CamStatusToBrushConverter : IValueConverter
{
    public static readonly CamStatusToBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c)
    {
        var s = v switch
        {
            Models.CamStatusKind.Bound   => "bound",
            Models.CamStatusKind.Unbound => "unbound",
            Models.CamStatusKind.Offline => "offline",
            string str                   => str,
            _                            => "",
        };
        return new SolidColorBrush(s switch
        {
            "bound" or "已綁定"   => Color.Parse("#2ecc71"),
            "unbound" or "待綁定" => Color.Parse("#E2A03B"),
            _                     => Color.Parse("#9AA6B3"),
        });
    }
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>
/// 宣告槽的 live 狀態 → 色碼（Gap #21 join 結果）。沿用相機那組色系再加一個紅色告警：
///   已綁定·就位 綠 / MAC 不符 紅（插錯槽位）/ 離線 灰 / 已宣告·未綁 琥珀。
/// </summary>
public sealed class SlotBindToBrushConverter : IValueConverter
{
    public static readonly SlotBindToBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c) =>
        new SolidColorBrush(v switch
        {
            ViewModels.SlotBindKind.Bound       => Color.Parse("#2ecc71"),   // 綠：就位
            ViewModels.SlotBindKind.MacMismatch => Color.Parse("#d1342f"),   // 紅：插錯槽位
            ViewModels.SlotBindKind.Offline     => Color.Parse("#9AA6B3"),   // 灰：該在卻不在
            _                                   => Color.Parse("#E2A03B"),   // 琥珀：已宣告·未綁
        });
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>字串非空 → true。用於「有告警才顯示該列」的 IsVisible。</summary>
public sealed class NotEmptyToBoolConverter : IValueConverter
{
    public static readonly NotEmptyToBoolConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c)
        => !string.IsNullOrWhiteSpace(v as string);
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>bool 反相（true→false）。用於 IsVisible 切換。</summary>
public sealed class InverseBoolConverter : IValueConverter
{
    public static readonly InverseBoolConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c) => v is not true;
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => v is not true;
}

/// <summary>工作台：槽位狀態 → 左框色（綠=已綁 琥珀=未綁 紅=MAC不符 灰=離線/空）。</summary>
public sealed class SlotKindToBrushConverter : IValueConverter
{
    public static readonly SlotKindToBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c) => v switch
    {
        ViewModels.SlotBindKind.Bound       => new SolidColorBrush(Color.Parse("#16a34a")),
        ViewModels.SlotBindKind.MacMismatch => new SolidColorBrush(Color.Parse("#dc2626")),
        ViewModels.SlotBindKind.Declared    => new SolidColorBrush(Color.Parse("#d97706")),
        _                                   => new SolidColorBrush(Color.Parse("#9ca3af")),
    };
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}

/// <summary>工作台：步驟完成點（true=綠、false=淡灰）。</summary>
public sealed class StepDotBrushConverter : IValueConverter
{
    public static readonly StepDotBrushConverter Instance = new();
    public object Convert(object? v, Type t, object? p, CultureInfo c)
        => new SolidColorBrush(v is true ? Color.Parse("#16a34a") : Color.Parse("#e5e7eb"));
    public object ConvertBack(object? v, Type t, object? p, CultureInfo c) => throw new NotSupportedException();
}
