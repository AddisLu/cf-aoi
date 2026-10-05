using System;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using CfAoiControl.Controllers;
using CfAoiControl.Models;
using CfAoiControl.Services;

namespace CfAoiControl.ViewModels;

/// <summary>
/// 系統狀態（商業化階段 2）：經各 Linux 節點的代理（cfaoi_agent.py，port 8300）遠端管理 Grab / Spark。
/// 線上人員唯一的螢幕是 Windows 上的 Control → 這裡要能看懂狀態、一鍵重啟、收診斷包，不必碰 Linux。
/// 會中斷生產的動作（重啟/切模式/重開機/全部重啟）一律先內嵌確認（同工作台「解除綁定」作法）。
/// </summary>
public sealed partial class NodeAgentsViewModel : ObservableObject
{
    private readonly AppServices _svc;
    private CancellationTokenSource? _pollCts;
    private Func<Task>? _pending;

    public ObservableCollection<AgentNodeItem> Nodes { get; } = new();

    /// <summary>本 Control 的 git 短碼（建置時 SDK 寫入 InformationalVersion "x.y.z+&lt;sha&gt;"）。</summary>
    public string ControlVersion { get; } = ReadControlVersion();

    [ObservableProperty] private string summary = "尚未取得狀態";
    [ObservableProperty] private bool allHealthy;
    [ObservableProperty] private string warning = "";
    [ObservableProperty] private string actionStatus = "";
    [ObservableProperty] private bool isBusy;

    // 內嵌確認列
    [ObservableProperty] private bool awaitingConfirm;
    [ObservableProperty] private string confirmText = "";

    // log 檢視
    [ObservableProperty] private string logTitle = "";
    [ObservableProperty] private string logText = "";
    [ObservableProperty] private bool hasLog;

    public NodeAgentsViewModel(AppServices svc)
    {
        _svc = svc;
        foreach (var a in svc.Config.Agents)
            Nodes.Add(new AgentNodeItem(this, a));
    }

    /// <summary>GUI 啟動後呼叫（selftest/設計時不輪詢）。每 5 秒更新一次。</summary>
    public void StartPolling()
    {
        if (_pollCts is not null || Nodes.Count == 0) return;
        _pollCts = new CancellationTokenSource();
        var ct = _pollCts.Token;
        _ = Task.Run(async () =>
        {
            while (!ct.IsCancellationRequested)
            {
                await RefreshAllAsync(ct);
                try { await Task.Delay(5000, ct); } catch (OperationCanceledException) { }
            }
        }, ct);
    }

    public void StopPolling() { _pollCts?.Cancel(); _pollCts = null; }

    private async Task RefreshAllAsync(CancellationToken ct = default)
    {
        await Task.WhenAll(Nodes.Select(n => RefreshNodeAsync(n, ct)));
        Dispatcher.UIThread.Post(UpdateSummary);
    }

    private static async Task RefreshNodeAsync(AgentNodeItem n, CancellationToken ct)
    {
        try
        {
            var d = await n.Client.StatusAsync(ct);
            var recvEpoch = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            Dispatcher.UIThread.Post(() => n.Apply(d, recvEpoch));
        }
        catch (Exception ex) when (ex is AgentException or OperationCanceledException)
        {
            if (ct.IsCancellationRequested) return;
            Dispatcher.UIThread.Post(() => n.SetOffline(ex.Message));
        }
    }

    private void UpdateSummary()
    {
        int online = Nodes.Count(n => n.Online);
        int bad = Nodes.Count(n => n.Online && !n.ServicesHealthy);
        AllHealthy = online == Nodes.Count && bad == 0;
        Summary = AllHealthy ? $"全部正常（{online}/{Nodes.Count} 節點）"
                : online < Nodes.Count ? $"⚠ {Nodes.Count - online} 個節點連不上"
                : $"⚠ {bad} 個節點有服務未運作";
        // 版本一致性：Control 與各節點的 git 短碼（-dirty = 有未提交修改）
        var vers = Nodes.Where(n => n.Online && n.Version != "").Select(n => $"{n.Name} {n.Version}").ToList();
        bool mismatch = Nodes.Any(n => n.Online && n.Version != "" && !SameVersion(n.Version, ControlVersion));
        var warn = new StringBuilder();
        if (mismatch)
            warn.Append($"版本不一致：Control {ControlVersion}、{string.Join("、", vers)}。請用同一版更新三台。");
        foreach (var n in Nodes.Where(n => n.Online && n.TimeWarn))
            warn.Append((warn.Length > 0 ? "　" : "") + $"{n.Name} 時間差 {n.TimeOffsetText}（>1 秒，結果日期/log 會對不上）。");
        Warning = warn.ToString();
    }

    private static bool SameVersion(string node, string control)
    {
        if (control.Length < 7) return true;                 // Control 沒有版本資訊（非 git 建置）→ 不比
        var nv = node.Split('-')[0];
        return nv.Length >= 7 && control.StartsWith(nv, StringComparison.OrdinalIgnoreCase)
               && !node.EndsWith("-dirty");
    }

    private static string ReadControlVersion()
    {
        var iv = typeof(NodeAgentsViewModel).Assembly
                     .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "";
        var plus = iv.IndexOf('+');
        return plus >= 0 && iv.Length > plus + 7 ? iv.Substring(plus + 1, 7) : "";
    }

    // ── 確認列 ───────────────────────────────────────────────────────────
    internal void Ask(string text, Func<Task> action)
    {
        ConfirmText = text;
        _pending = action;
        AwaitingConfirm = true;
    }

    [RelayCommand]
    private async Task Confirm()
    {
        AwaitingConfirm = false;
        var act = _pending; _pending = null;
        if (act is not null) await RunAsync(act);
    }

    [RelayCommand]
    private void CancelConfirm() { AwaitingConfirm = false; _pending = null; }

    private async Task RunAsync(Func<Task> act)
    {
        IsBusy = true;
        try { await act(); }
        catch (Exception ex)
        {
            ActionStatus = $"❌ {ex.Message}";
            _svc.Log.Error($"系統狀態：{ex.Message}");
        }
        finally
        {
            IsBusy = false;
            await RefreshAllAsync();
        }
    }

    // ── 單一服務 / 節點動作（由 item 的命令呼叫）─────────────────────────
    internal void AskRestart(AgentNodeItem n, AgentServiceItem s) =>
        Ask($"重新啟動 {n.Name} 的「{s.Label}」？進行中的取像/檢測會中斷。", async () =>
        {
            ActionStatus = $"重新啟動 {n.Name} {s.Label}…";
            await n.Client.ServiceAsync(s.Unit, "restart");
            ActionStatus = $"✓ 已重新啟動 {n.Name} {s.Label}";
            _svc.Log.Info($"系統狀態：重新啟動 {n.Name} {s.Unit}");
        });

    internal void AskSwitchTo(AgentNodeItem n, AgentServiceItem s) =>
        Ask($"把 {n.Name} 切到「{s.Label}」？另一個模式會自動停止（生產↔調參互斥）。", async () =>
        {
            ActionStatus = $"切換 {n.Name} → {s.Label}…";
            await n.Client.ServiceAsync(s.Unit, "start");
            ActionStatus = $"✓ {n.Name} 已切到 {s.Label}（開機預設仍是生產模式）";
            _svc.Log.Info($"系統狀態：{n.Name} 切到 {s.Unit}");
        });

    internal void AskReboot(AgentNodeItem n) =>
        Ask($"重新開機 {n.Name}（{n.Hostname}）？約 1–3 分鐘無法使用，開機後服務會自動啟動。", async () =>
        {
            await n.Client.PowerAsync("reboot");
            ActionStatus = $"✓ 已送出 {n.Name} 重新開機";
            _svc.Log.Warn($"系統狀態：{n.Name} 重新開機");
        });

    internal async Task ShowLogsAsync(AgentNodeItem n, AgentServiceItem s)
    {
        LogTitle = $"{n.Name} · {s.Label}（{s.Unit}）最近 300 行";
        LogText = "讀取中…";
        HasLog = true;
        try
        {
            var d = await n.Client.LogsAsync(s.Unit, 300);
            LogText = d?["text"]?.GetValue<string>() is { Length: > 0 } t ? t : "（沒有 log）";
        }
        catch (Exception ex) { LogText = $"❌ {ex.Message}"; }
    }

    [RelayCommand]
    private void CloseLog() { HasLog = false; LogText = ""; }

    [RelayCommand]
    private async Task Refresh()
    {
        ActionStatus = "更新中…";
        await RefreshAllAsync();
        ActionStatus = $"已更新 {DateTime.Now:HH:mm:ss}";
    }

    /// <summary>全部重新啟動：先 IP（收端要先聽著）再 Grab；只重啟目前在跑的服務（調參中就重啟調參）。</summary>
    [RelayCommand]
    private void RestartAll() =>
        Ask("全部重新啟動（先 Spark IP、再 Grab）？進行中的取像/檢測會中斷，約 10–30 秒後恢復。", async () =>
        {
            foreach (var role in new[] { "ip", "grab" })
            foreach (var n in Nodes.Where(x => x.Role == role && x.Online))
            {
                var targets = n.Services.Where(s => s.IsActive).ToList();
                if (targets.Count == 0) targets = n.Services.Where(s => s.Enabled == "enabled").ToList();
                foreach (var s in targets)
                {
                    ActionStatus = $"重新啟動 {n.Name} {s.Label}…";
                    await n.Client.ServiceAsync(s.Unit, "restart");
                }
            }
            ActionStatus = "✓ 全部重新啟動完成；Control 會在幾秒內自動重連（看上方燈號）";
            _svc.Log.Info("系統狀態：全部重新啟動");
        });

    /// <summary>收集診斷包：各節點 tar.gz + Control 自己的 log → OutputDir/diag/&lt;時間&gt;/（給工程師帶出 fab）。</summary>
    [RelayCommand]
    private async Task CollectDiag() => await RunAsync(async () =>
    {
        var dir = Path.Combine(RecipeService.ExpandPath(_svc.Config.Paths.OutputDir), "diag",
                               DateTime.Now.ToString("yyyyMMdd_HHmmss"));
        Directory.CreateDirectory(dir);
        var sb = new StringBuilder();
        foreach (var e in _svc.Log.Entries.ToList())
            sb.AppendLine($"{e.Time:yyyy-MM-dd HH:mm:ss} [{e.Level}] {e.Message}");
        await File.WriteAllTextAsync(Path.Combine(dir, "control_log.txt"), sb.ToString());
        var done = new StringBuilder("control_log.txt");
        foreach (var n in Nodes)
        {
            ActionStatus = $"收集 {n.Name} 診斷包…";
            try
            {
                var d = await n.Client.DiagAsync();
                var name = d?["filename"]?.GetValue<string>() ?? $"{n.Name}.tar.gz";
                await File.WriteAllBytesAsync(Path.Combine(dir, name),
                    Convert.FromBase64String(d?["base64"]?.GetValue<string>() ?? ""));
                done.Append($"、{name}");
            }
            catch (Exception ex) { done.Append($"、{n.Name} 失敗（{ex.Message}）"); }
        }
        ActionStatus = $"✓ 診斷包已存到 {dir}：{done}";
        _svc.Log.Info($"系統狀態：診斷包 → {dir}");
    });
}

/// <summary>一個 Linux 節點（Grab 主機 / Spark）。</summary>
public sealed partial class AgentNodeItem : ObservableObject
{
    private readonly NodeAgentsViewModel _owner;
    public AgentClient Client { get; }
    public string Name { get; }
    public string Role { get; }
    public string Endpoint => $"{Client.Host}:{Client.Port}";
    public bool IsIp => Role == "ip";
    public ObservableCollection<AgentServiceItem> Services { get; } = new();

    [ObservableProperty] private bool online;
    [ObservableProperty] private string hostname = "—";
    [ObservableProperty] private string version = "";
    [ObservableProperty] private string infoText = "連線中…";
    [ObservableProperty] private string timeOffsetText = "";
    [ObservableProperty] private bool timeWarn;
    [ObservableProperty] private string error = "";
    [ObservableProperty] private bool servicesHealthy;

    public AgentNodeItem(NodeAgentsViewModel owner, AgentConfig cfg)
    {
        _owner = owner;
        Name = cfg.Name;
        Role = cfg.Role;
        Client = new AgentClient(cfg.Host, cfg.Port);
    }

    internal void Apply(JsonNode? d, double recvEpoch)
    {
        if (d is null) { SetOffline("代理回應空白"); return; }
        Online = true;
        Error = "";
        Hostname = d["hostname"]?.GetValue<string>() ?? "—";
        Version = d["version"]?.GetValue<string>() ?? "";
        var offset = (d["time_epoch"]?.GetValue<double>() ?? recvEpoch) - recvEpoch;
        TimeOffsetText = $"{offset:+0.00;-0.00}s";
        TimeWarn = Math.Abs(offset) > 1.0;
        var ts = d["timesync"];
        var tsText = ts is null ? "" : (ts["synced"]?.GetValue<bool>() == true ? "已校時" : "⚠ 未校時")
                                       + $"（{ts["source"]?.GetValue<string>()}）";
        var disk = d["disk"];
        var up = TimeSpan.FromSeconds(d["uptime_s"]?.GetValue<double>() ?? 0);
        InfoText = $"{Hostname} · 版本 {(Version == "" ? "?" : Version)} · 時間差 {TimeOffsetText} {tsText} · " +
                   $"磁碟 {disk?["used_pct"]?.GetValue<double>():0}%（{disk?["total_gb"]?.GetValue<double>():0}GB）· " +
                   $"開機 {(int)up.TotalDays}天{up.Hours}時";

        // 服務列：首次建立，之後就地更新（避免整列重建造成畫面閃爍、按鈕失焦）
        if (d["services"] is JsonArray arr)
        {
            foreach (var s in arr)
            {
                var unit = s?["unit"]?.GetValue<string>() ?? "";
                var item = Services.FirstOrDefault(x => x.Unit == unit);
                if (item is null)
                {
                    item = new AgentServiceItem(this, unit, s?["label"]?.GetValue<string>() ?? unit);
                    Services.Add(item);
                }
                item.Apply(s!);
            }
        }
        // 健康：Grab 角色 = cfaoi-grab 在跑；IP 角色 = 生產或調參其一在跑
        ServicesHealthy = Services.Count > 0 && Services.Any(x => x.IsActive)
                          && (Role != "grab" || Services.All(x => x.IsActive));
    }

    internal void SetOffline(string message)
    {
        Online = false;
        ServicesHealthy = false;
        Error = message;
        InfoText = "連不上節點代理（節點關機、網路線、或代理服務未啟動）";
    }

    internal void RequestRestart(AgentServiceItem s) => _owner.AskRestart(this, s);
    internal void RequestSwitch(AgentServiceItem s) => _owner.AskSwitchTo(this, s);
    internal Task RequestLogs(AgentServiceItem s) => _owner.ShowLogsAsync(this, s);

    [RelayCommand]
    private void Reboot() => _owner.AskReboot(this);
}

/// <summary>節點上的一個 cfaoi-* 服務。</summary>
public sealed partial class AgentServiceItem : ObservableObject
{
    private readonly AgentNodeItem _node;
    public string Unit { get; }
    public string Label { get; }
    /// <summary>IP 角色才有「切到此模式」（生產/調參互斥）。</summary>
    public bool CanSwitch => _node.IsIp;

    [ObservableProperty][NotifyPropertyChangedFor(nameof(IsActive))] private string active = "";
    [ObservableProperty] private string enabled = "";
    [ObservableProperty] private string stateText = "";

    public bool IsActive => Active == "active";

    public AgentServiceItem(AgentNodeItem node, string unit, string label)
    {
        _node = node; Unit = unit; Label = label;
    }

    internal void Apply(JsonNode s)
    {
        Active = s["active"]?.GetValue<string>() ?? "unknown";
        Enabled = s["enabled"]?.GetValue<string>() ?? "";
        var restarts = s["restarts"]?.GetValue<int>() ?? 0;
        var state = Active switch
        {
            "active" => "運作中",
            "inactive" => "已停止",
            "failed" => "⚠ 失敗",
            "activating" => "啟動中…",
            "deactivating" => "停止中…",
            _ => Active,
        };
        StateText = $"{state} · {(Enabled == "enabled" ? "開機自啟" : "手動")}" +
                    // IP 生產模式每片結束（Grab 斷 RDMA）本來就會重生一次 → 次數無意義，只顯示 Grab 的
                    (restarts > 0 && !_node.IsIp ? $" · 異常重生 {restarts} 次" : "");
    }

    [RelayCommand] private void Restart() => _node.RequestRestart(this);
    [RelayCommand] private void SwitchTo() => _node.RequestSwitch(this);
    [RelayCommand] private Task ShowLogs() => _node.RequestLogs(this);
}
