using System;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace CfAoiControl.Controllers;

/// <summary>
/// TCP → Grab（預設 port 8100），newline-delimited JSON。本階段（Step 1 offline）不取像，
/// 先提供連線/健康檢查骨架，Step 2+ 再擴充取像命令。
/// </summary>
public sealed class GrabClient : IDisposable
{
    private readonly SemaphoreSlim _lock = new(1, 1);
    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private StreamReaderLite? _reader;
    private int _seq;

    public string Host { get; private set; } = "127.0.0.1";
    public int Port { get; private set; } = 8100;
    public bool IsConnected => _tcp?.Connected == true;

    public async Task ConnectAsync(string host, int port, CancellationToken ct = default)
    {
        await _lock.WaitAsync(ct);
        try
        {
            Close();
            Host = host; Port = port;
            _tcp = new TcpClient { NoDelay = true };
            await _tcp.ConnectAsync(host, port, ct);
            _stream = _tcp.GetStream();
            _reader = new StreamReaderLite(_stream);
        }
        finally { _lock.Release(); }
    }

    public Task<JsonNode?> CheckHealthAsync(CancellationToken ct = default)
        => SendCommandAsync("CHECK_HEALTH", null, ct);

    public async Task<JsonNode?> SendCommandAsync(string cmd, JsonObject? prms, CancellationToken ct = default)
    {
        await _lock.WaitAsync(ct);
        try
        {
            if (_stream is null || _reader is null) throw new InvalidOperationException("GrabClient 未連線");
            var seq = Interlocked.Increment(ref _seq);
            var obj = new JsonObject { ["cmd"] = cmd, ["seq"] = seq, ["params"] = prms ?? new JsonObject() };
            await _stream.WriteAsync(Encoding.UTF8.GetBytes(obj.ToJsonString() + "\n"), ct);
            await _stream.FlushAsync(ct);
            var resp = await _reader.ReadLineAsync(ct);
            return resp is null ? null : JsonNode.Parse(resp);
        }
        finally { _lock.Release(); }
    }

    private void Close()
    {
        try { _stream?.Dispose(); } catch { }
        try { _tcp?.Dispose(); } catch { }
        _stream = null; _reader = null; _tcp = null;
    }

    public void Dispose() { Close(); _lock.Dispose(); }
}
