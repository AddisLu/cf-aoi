using System;
using System.Threading;
using System.Threading.Tasks;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CfAoiControl.Controllers;

/// <summary>
/// 連線狀態 + 主動心跳偵測 + 自動重連（不變式 3：連線失敗不阻塞啟動）。
/// 每 ~2.5s 對 IP/Grab 送 CHECK_HEALTH（5s timeout）：成功 → 綠燈。
/// **連續 2 次失敗**才判定斷線（單次抖動不翻燈、不記 log、不關 socket）→ 紅燈 + log「連線中斷」
/// 並關閉 socket 讓下一回合重連；連回來 → 綠燈 + log「已重新連線」。
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

    // 心跳容忍度（2026-07-30 依實測調整）。取捨：
    //   逾時太短/單次即斷 → 網路抖動就假斷線、log 洗版、連線反覆重建（實測手機熱點下每 4 秒一次）。
    //   逾時太長/門檻太高 → 真的斷線時偵測變慢。
    // 現值：5s 逾時 × 連續 2 次失敗 → 最慢約 12.5s 判定斷線，對狀態燈而言可接受。
    private static readonly TimeSpan HeartbeatTimeout = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan ConnectTimeout   = TimeSpan.FromSeconds(4);
    private const int MaxConsecutiveFails = 2;

    private async Task HeartbeatLoop(string name, IHeartbeatClient client,
                                     Func<NodeConfig?> nodeOf, Action<bool> setStatus, CancellationToken ct)
    {
        bool prev = false, everUp = false;
        int consecutiveFails = 0;
        while (!ct.IsCancellationRequested)
        {
            bool ok = false;
            var node = nodeOf();
            if (node is not null)
            {
                if (client.IsBusy)
                {
                    ok = true;                              // 命令進行中 → 視為存活，跳過本回合心跳
                    consecutiveFails = 0;
                }
                else
                {
                    bool healthy = false;
                    try
                    {
                        if (!client.IsConnected)
                        {
                            using var cc = new CancellationTokenSource(ConnectTimeout);
                            await client.ConnectAsync(node.Host, node.Port, cc.Token);
                        }
                        using var hb = new CancellationTokenSource(HeartbeatTimeout);
                        var resp = await client.CheckHealthAsync(hb.Token);
                        healthy = resp?["status"]?.GetValue<string>() == "OK";
                    }
                    catch { healthy = false; }

                    // ⚠️ 單次逾時**不可**直接斷線（2026-07-30 實測教訓）：
                    // 當時全系統跑在手機熱點上，到 Spark 有 12.5% 封包遺失、RTT 尖峰 987ms，
                    // 舊版 2s 逾時 + 單次失敗即 Disconnect() → 每 4 秒斷一次、log 洗版，
                    // 但 IP 程式其實一直好好的。網路抖動或 GC 停頓不該扯斷連線。
                    if (healthy) { consecutiveFails = 0; ok = true; }
                    else
                    {
                        ++consecutiveFails;
                        if (consecutiveFails >= MaxConsecutiveFails)
                        {
                            client.Disconnect();            // 關閉壞掉的 socket → 下回合重連
                            ok = false;
                        }
                        else
                        {
                            ok = prev;                      // 未達門檻 → 維持原狀態，不翻燈、不記 log
                        }
                    }
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
