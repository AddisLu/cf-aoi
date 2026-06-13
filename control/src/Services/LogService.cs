using System;
using System.Collections.ObjectModel;

namespace CfAoiControl.Services;

public enum LogLevel { Info, Warning, Error }

public sealed record LogEntry(DateTime Time, LogLevel Level, string Message)
{
    public string Display => $"{Time:HH:mm:ss} {Message}";
}

/// <summary>
/// 系統 log（A5 慣例：紅=錯誤、藍=警告、黃底系統訊息）。UI 綁 Entries。
/// </summary>
public sealed class LogService
{
    public ObservableCollection<LogEntry> Entries { get; } = new();
    public event Action<LogEntry>? Logged;

    public void Info(string m) => Add(LogLevel.Info, m);
    public void Warn(string m) => Add(LogLevel.Warning, m);
    public void Error(string m) => Add(LogLevel.Error, m);

    private void Add(LogLevel level, string m)
    {
        var e = new LogEntry(DateTime.Now, level, m);
        // UI thread marshaling 交給呼叫端/Dispatcher；此處僅儲存 + 通知
        Entries.Add(e);
        if (Entries.Count > 1000) Entries.RemoveAt(0);
        Logged?.Invoke(e);
    }
}
