using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace CfAoiControl.Controllers;

/// <summary>
/// TCP → 節點代理（tools/node_agent/cfaoi_agent.py，port 8300；商業化階段 2）。
/// 協定同 Grab/IP：一行一個 JSON {cmd, seq, params} → {seq, status, data, error}。
///
/// 與 GrabClient/IpClient 不同：**每個命令開一條新連線**。代理命令很少（狀態每幾秒一次、
/// 重啟/收 log 是人按的），短連線不留殘線，節點重開機後也不必處理半開連線。
/// 讀回應：整行 bytes 收齊後一次 UTF-8 解碼（control 不變式 10；診斷包 base64 可達數十 MB，用 64KB 緩衝讀）。
/// </summary>
public sealed class AgentClient
{
    public string Host { get; }
    public int Port { get; }
    private int _seq;

    public AgentClient(string host, int port) { Host = host; Port = port; }

    /// <summary>送一個命令並回傳 data 節點；status≠OK 時丟 AgentException（訊息 = 代理回的 error）。</summary>
    public async Task<JsonNode?> CallAsync(string cmd, JsonObject? prms, TimeSpan timeout, CancellationToken ct = default)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(timeout);
        using var tcp = new TcpClient { NoDelay = true };
        try
        {
            await tcp.ConnectAsync(Host, Port, cts.Token);
            var stream = tcp.GetStream();
            var seq = Interlocked.Increment(ref _seq);
            var req = new JsonObject { ["cmd"] = cmd, ["seq"] = seq, ["params"] = prms ?? new JsonObject() };
            await stream.WriteAsync(Encoding.UTF8.GetBytes(req.ToJsonString() + "\n"), cts.Token);
            var line = await ReadLineAsync(stream, cts.Token)
                       ?? throw new AgentException($"{cmd}：代理未回應即斷線");
            var resp = JsonNode.Parse(line);
            if (resp?["status"]?.GetValue<string>() != "OK")
                throw new AgentException(resp?["error"]?.GetValue<string>() ?? $"{cmd} 失敗");
            return resp?["data"];
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new AgentException($"{cmd} 逾時（{timeout.TotalSeconds:0}s，{Host}:{Port}）");
        }
        catch (SocketException ex)
        {
            throw new AgentException($"連不上代理 {Host}:{Port}（{ex.SocketErrorCode}）");
        }
    }

    private static async Task<string?> ReadLineAsync(NetworkStream s, CancellationToken ct)
    {
        using var ms = new MemoryStream();
        var buf = new byte[64 * 1024];
        while (true)
        {
            int n = await s.ReadAsync(buf, ct);
            if (n == 0) return ms.Length == 0 ? null : Encoding.UTF8.GetString(ms.ToArray());
            int nl = Array.IndexOf(buf, (byte)'\n', 0, n);
            if (nl >= 0)
            {
                ms.Write(buf, 0, nl);
                return Encoding.UTF8.GetString(ms.ToArray());
            }
            ms.Write(buf, 0, n);
        }
    }

    // ── 命令（對應 cfaoi_agent.py COMMANDS）────────────────────────────────
    public Task<JsonNode?> StatusAsync(CancellationToken ct = default)
        => CallAsync("STATUS", null, TimeSpan.FromSeconds(5), ct);

    public Task<JsonNode?> ServiceAsync(string unit, string action, CancellationToken ct = default)
        => CallAsync("SERVICE", new JsonObject { ["unit"] = unit, ["action"] = action }, TimeSpan.FromSeconds(70), ct);

    public Task<JsonNode?> LogsAsync(string unit, int lines, CancellationToken ct = default)
        => CallAsync("LOGS", new JsonObject { ["unit"] = unit, ["lines"] = lines }, TimeSpan.FromSeconds(25), ct);

    public Task<JsonNode?> DiagAsync(CancellationToken ct = default)
        => CallAsync("DIAG", null, TimeSpan.FromSeconds(120), ct);

    public Task<JsonNode?> PowerAsync(string action, CancellationToken ct = default)
        => CallAsync("POWER", new JsonObject { ["action"] = action, ["confirm"] = true }, TimeSpan.FromSeconds(10), ct);
}

public sealed class AgentException : Exception
{
    public AgentException(string message) : base(message) { }
}
