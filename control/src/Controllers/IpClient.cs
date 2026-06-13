using System;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace CfAoiControl.Controllers;

/// <summary>
/// TCP → IP（預設 port 8200），newline-delimited JSON 命令。協議對齊 ip/src/control_server.cpp：
///   送：{"cmd":..,"seq":..,"params":{..}}\n      收：{"seq":..,"status":"OK"|"ERR",...}\n
///   SEND_IMAGE_FOR_REVIEW：命令行(\n) 後緊接 payload_bytes 個 raw bytes（Mono8）。
/// 注意：IP 收圖後僅回 OK，偵測結果寫到 IP 的 --output（由 OfflineReviewService 讀回 JSON）。
/// </summary>
public sealed class IpClient : IDisposable
{
    private readonly SemaphoreSlim _lock = new(1, 1);
    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private StreamReaderLite? _reader;
    private int _seq;

    public string Host { get; private set; } = "127.0.0.1";
    public int Port { get; private set; } = 8200;
    public bool IsConnected => _tcp?.Connected == true;

    public async Task ConnectAsync(string host, int port, CancellationToken ct = default)
    {
        await _lock.WaitAsync(ct);
        try
        {
            CloseInternal();
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

    public Task<JsonNode?> GetStatusAsync(CancellationToken ct = default)
        => SendCommandAsync("GET_STATUS", null, ct);

    // recipeXml 為配方 XML 內容（跨機器免共用檔案系統）；recipePathOrName 為同機路徑/名稱備用。
    public Task<JsonNode?> LoadRecipeAsync(string recipePathOrName, string panelId,
                                           string? recipeXml = null, CancellationToken ct = default)
        => SendCommandAsync("LOAD_RECIPE",
            new JsonObject
            {
                ["recipe"] = recipePathOrName,
                ["recipe_xml"] = recipeXml ?? "",
                ["panel_id"] = panelId,
            }, ct);

    public Task<JsonNode?> SendImageStreamBeginAsync(string panelId, CancellationToken ct = default)
        => SendCommandAsync("SEND_IMAGE_STREAM_BEGIN", new JsonObject { ["panel_id"] = panelId }, ct);

    /// <summary>送一張 Mono8 影像（命令行 + 緊接 raw payload）。</summary>
    public async Task<JsonNode?> SendImageForReviewAsync(
        string panelId, int camId, int width, int height, int frameSeq,
        byte[] payload, bool last = true, CancellationToken ct = default)
    {
        if (payload.Length != (long)width * height)
            throw new ArgumentException("payload 長度必須等於 width*height (Mono8)");

        await _lock.WaitAsync(ct);
        try
        {
            EnsureOpen();
            var seq = Interlocked.Increment(ref _seq);
            var cmd = new JsonObject
            {
                ["cmd"] = "SEND_IMAGE_FOR_REVIEW",
                ["seq"] = seq,
                ["params"] = new JsonObject
                {
                    ["panel_id"] = panelId, ["cam_id"] = camId,
                    ["width"] = width, ["height"] = height,
                    ["frame_seq"] = frameSeq, ["payload_bytes"] = payload.Length,
                    ["last"] = last,
                },
            };
            var line = cmd.ToJsonString() + "\n";
            await _stream!.WriteAsync(Encoding.UTF8.GetBytes(line), ct);
            await _stream.WriteAsync(payload, ct);          // 緊接 binary payload
            await _stream.FlushAsync(ct);
            var resp = await _reader!.ReadLineAsync(ct);
            return resp is null ? null : JsonNode.Parse(resp);
        }
        finally { _lock.Release(); }
    }

    private async Task<JsonNode?> SendCommandAsync(string cmd, JsonObject? prms, CancellationToken ct)
    {
        await _lock.WaitAsync(ct);
        try
        {
            EnsureOpen();
            var seq = Interlocked.Increment(ref _seq);
            var obj = new JsonObject { ["cmd"] = cmd, ["seq"] = seq, ["params"] = prms ?? new JsonObject() };
            var line = obj.ToJsonString() + "\n";
            await _stream!.WriteAsync(Encoding.UTF8.GetBytes(line), ct);
            await _stream.FlushAsync(ct);
            var resp = await _reader!.ReadLineAsync(ct);
            return resp is null ? null : JsonNode.Parse(resp);
        }
        finally { _lock.Release(); }
    }

    private void EnsureOpen()
    {
        if (_stream is null || _reader is null || _tcp?.Connected != true)
            throw new InvalidOperationException("IpClient 未連線");
    }

    private void CloseInternal()
    {
        try { _stream?.Dispose(); } catch { }
        try { _tcp?.Dispose(); } catch { }
        _stream = null; _reader = null; _tcp = null;
    }

    public void Dispose() { CloseInternal(); _lock.Dispose(); }
}

/// <summary>逐 byte 安全讀「一行（\n 結尾）」，不吃掉後續 binary（本連線伺服器只回 JSON 行）。</summary>
internal sealed class StreamReaderLite
{
    private readonly NetworkStream _s;
    public StreamReaderLite(NetworkStream s) => _s = s;

    public async Task<string?> ReadLineAsync(CancellationToken ct)
    {
        var sb = new StringBuilder();
        var one = new byte[1];
        while (true)
        {
            int n = await _s.ReadAsync(one, 0, 1, ct);
            if (n <= 0) return sb.Length == 0 ? null : sb.ToString();
            char c = (char)one[0];
            if (c == '\n') return sb.ToString();
            if (c != '\r') sb.Append(c);
        }
    }
}
