using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace CfAoiControl.Controllers;

/// <summary>
/// TCP server ← 上位機。遷移自 legacy PrjCfAoi/Class/MainProc.cs，移除 MIL/Camera，
/// 內部把命令轉成對 IP 的 JSON 呼叫（透過 callbacks，ViewModel 不直接碰 TCP）。
///
/// ⚠️ TODO — 上位機真實協議缺口（接真實上位機前必改）：
///   本實作用的是 docs/CLAUDE.md 的「簡化介面」：port 8000、命令 LoadRecipe|RECIPE|PANEL /
///   GrabStart|PANEL / GetResult，回應 OK\r\n。
///   但考古確認 legacy 實際是：port **8787**、命令前綴 **CF_**（CF_LOAD_RECIPE/CF_GRAB_START/
///   CF_CHECK_ALIGN/CF_SET_ALIGN/CF_GET_RESULT）、'|' 分隔 9 參數、回應 OK|p1..|p9 / ERR，
///   CF_GET_RESULT 回 ResultInfo.xml 路徑+缺陷數（非 JSON）。
///   → 串接真實上位機時，須改成 CF_ 前綴 / 9 參數 / 8787。本檔已在 docs/CLAUDE.md §5 與
///     control/CLAUDE.md 不變式區留有對應 TODO。
/// 不變式：上位機命令名稱（LoadRecipe/GrabStart/GetResult）不可改。連線失敗不阻塞啟動。
/// </summary>
public sealed class UpstreamServer : IDisposable
{
    public Func<string, string, Task<bool>>? OnLoadRecipe { get; set; }  // (recipe, panel)
    public Func<string, Task<bool>>? OnGrabStart { get; set; }           // (panel)
    public Func<Task<string>>? OnGetResult { get; set; }                 // → json/payload

    private TcpListener? _listener;
    private CancellationTokenSource? _cts;
    private readonly int _port;
    private readonly Action<string>? _log;

    public bool IsListening { get; private set; }

    public UpstreamServer(int port, Action<string>? log = null) { _port = port; _log = log; }

    public void Start()
    {
        _cts = new CancellationTokenSource();
        _ = Task.Run(() => AcceptLoopAsync(_cts.Token));
    }

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        try
        {
            _listener = new TcpListener(IPAddress.Any, _port);
            _listener.Start();
            IsListening = true;
            _log?.Invoke($"[Upstream] 監聽 port {_port}");
        }
        catch (Exception ex) { _log?.Invoke($"[Upstream] 無法監聽 {_port}: {ex.Message}"); return; }

        while (!ct.IsCancellationRequested)
        {
            TcpClient client;
            try { client = await _listener.AcceptTcpClientAsync(ct); }
            catch { break; }
            _ = Task.Run(() => HandleClientAsync(client, ct), ct);
        }
    }

    private async Task HandleClientAsync(TcpClient client, CancellationToken ct)
    {
        using (client)
        await using (var stream = client.GetStream())
        using (var reader = new StreamReader(stream, Encoding.UTF8))
        {
            var writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\r\n" };
            string? line;
            while (!ct.IsCancellationRequested && (line = await reader.ReadLineAsync(ct)) is not null)
            {
                var parts = line.Split('|');
                var cmd = parts[0].Trim();
                try
                {
                    switch (cmd)
                    {
                        case "LoadRecipe":
                        {
                            var recipe = parts.Length > 1 ? parts[1] : "";
                            var panel = parts.Length > 2 ? parts[2] : "";
                            var ok = OnLoadRecipe is null || await OnLoadRecipe(recipe, panel);
                            await writer.WriteLineAsync(ok ? "OK" : "ERR");
                            break;
                        }
                        case "GrabStart":
                        {
                            var panel = parts.Length > 1 ? parts[1] : "";
                            var ok = OnGrabStart is null || await OnGrabStart(panel);
                            await writer.WriteLineAsync(ok ? "OK" : "ERR");
                            break;
                        }
                        case "GetResult":
                        {
                            var payload = OnGetResult is null ? "{}" : await OnGetResult();
                            await writer.WriteLineAsync($"OK|{payload}");
                            break;
                        }
                        default:
                            await writer.WriteLineAsync($"ERR|unknown cmd: {cmd}");
                            break;
                    }
                }
                catch (Exception ex) { await writer.WriteLineAsync($"ERR|{ex.Message}"); }
            }
        }
    }

    public void Dispose()
    {
        _cts?.Cancel();
        try { _listener?.Stop(); } catch { }
        IsListening = false;
    }
}
