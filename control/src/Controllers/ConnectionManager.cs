using System;
using System.Threading;
using System.Threading.Tasks;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CfAoiControl.Controllers;

/// <summary>
/// 連線狀態 + 主動心跳偵測 + 自動重連（不變式 3：連線失敗不阻塞啟動）。
/// 每 ~2.5s 對 IP/Grab 送 CHECK_HEALTH（2s timeout）：成功 → 綠燈；失敗/timeout/例外 → 紅燈 +
/// log「連線中斷」，並關閉 socket 讓下一回合重連；連回來 → 綠燈 + log「已重新連線」。
/// 心跳/重連全在背景 Task，不阻塞 UI。上位機為 inbound（Control 為 server），狀態由 UpstreamServer 設定。
/// </summary>
public sealed partial class ConnectionManager : ObservableObject, IDisposable
{
    public IpClient Ip { get; } = new();
    public GrabClient Grab { get; } = new();

    [ObservableProperty] private bool isIpConnected;
    [ObservableProperty] private bool isGrabConnected;
    [ObservableProperty] private bool isUpstreamConnected;

    private CancellationTokenSource? _cts;
    private SystemConfigModel _cfg = new();
    private LogService? _log;

    public void SetUpstreamConnected(bool v) => IsUpstreamConnected = v;

    /// <summary>啟動背景心跳/重連迴圈（不 await；啟動不被連線狀態阻塞）。</summary>
    public void Start(SystemConfigModel cfg, LogService? log = null)
    {
        _cfg = cfg; _log = log;
        _cts = new CancellationTokenSource();
        var ct = _cts.Token;
        // IP（必接）
        _ = Task.Run(() => HeartbeatLoop("IP", Ip, () => _cfg.ActiveIp, v => IsIpConnected = v, ct));
        // Grab（Step 1 可能無 → 靜默重試；架構一致）
        _ = Task.Run(() => HeartbeatLoop("Grab", Grab,
            () => _cfg.Nodes.TryGetValue("GrabA", out var g) ? g : null, v => IsGrabConnected = v, ct));
    }

    private async Task HeartbeatLoop(string name, IHeartbeatClient client,
                                     Func<NodeConfig?> nodeOf, Action<bool> setStatus, CancellationToken ct)
    {
        bool prev = false, everUp = false;
        while (!ct.IsCancellationRequested)
        {
            bool ok = false;
            var node = nodeOf();
            if (node is not null)
            {
                if (client.IsBusy)
                {
                    ok = true;                              // 命令進行中 → 視為存活，跳過本回合心跳
                }
                else
                {
                    try
                    {
                        if (!client.IsConnected)
                        {
                            using var cc = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                            await client.ConnectAsync(node.Host, node.Port, cc.Token);
                        }
                        using var hb = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                        var resp = await client.CheckHealthAsync(hb.Token);
                        ok = resp?["status"]?.GetValue<string>() == "OK";
                    }
                    catch { ok = false; }
                    if (!ok) client.Disconnect();           // 關閉壞掉的 socket → 下回合重連
                }
            }

            // 只在狀態變化時記 log，避免洗版
            if (ok && !prev) { _log?.Info($"{name} {(everUp ? "已重新連線" : "已連線")} ({client.Host}:{client.Port})"); everUp = true; }
            else if (!ok && prev) { _log?.Error($"{name} 連線中斷（{client.Host}:{client.Port}）"); }

            prev = ok;
            setStatus(ok);

            try { await Task.Delay(2500, ct); } catch { break; }
        }
    }

    public void Dispose()
    {
        _cts?.Cancel();
        Ip.Dispose();
        Grab.Dispose();
    }
}
