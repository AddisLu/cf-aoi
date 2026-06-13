using System;
using System.Threading;
using System.Threading.Tasks;
using CfAoiControl.Models;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CfAoiControl.Controllers;

/// <summary>
/// 連線狀態 + 靜默自動重連（不變式 3：連線失敗不阻塞啟動）。
/// ViewModel 綁定 IsIpConnected/IsGrabConnected/IsUpstreamConnected 顯示狀態燈。
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

    public void SetUpstreamConnected(bool v) => IsUpstreamConnected = v;

    /// <summary>啟動背景重連迴圈（不 await；啟動不被連線狀態阻塞）。</summary>
    public void Start(SystemConfigModel cfg)
    {
        _cfg = cfg;
        _cts = new CancellationTokenSource();
        _ = Task.Run(() => ReconnectLoopAsync(_cts.Token));
    }

    private async Task ReconnectLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            // IP
            if (!Ip.IsConnected && _cfg.ActiveIp is { } node)
            {
                try { await Ip.ConnectAsync(node.Host, node.Port, ct); IsIpConnected = true; }
                catch { IsIpConnected = false; }
            }
            else IsIpConnected = Ip.IsConnected;

            // Grab（Step 1 可能無 → 靜默）
            if (!Grab.IsConnected && _cfg.Nodes.TryGetValue("GrabA", out var g))
            {
                try { await Grab.ConnectAsync(g.Host, g.Port, ct); IsGrabConnected = true; }
                catch { IsGrabConnected = false; }
            }
            else IsGrabConnected = Grab.IsConnected;

            try { await Task.Delay(3000, ct); } catch { break; }
        }
    }

    public void Dispose()
    {
        _cts?.Cancel();
        Ip.Dispose();
        Grab.Dispose();
    }
}
