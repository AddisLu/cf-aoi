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
public sealed class IpClient : IDisposable, IHeartbeatClient
{
    private readonly SemaphoreSlim _lock = new(1, 1);
    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private StreamReaderLite? _reader;
    private int _seq;

    public string Host { get; private set; } = "127.0.0.1";
    public int Port { get; private set; } = 8200;
    public bool IsConnected => _tcp?.Connected == true;
    public bool IsBusy => _lock.CurrentCount == 0;        // 有命令持有鎖 → 進行中
    public void Disconnect() => CloseInternal();

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

    /// <summary>列出 IP output 目錄中（指定日期的）缺陷結果資料夾。date 為 "yyyy-MM-dd"（空=全部）。</summary>
    public Task<JsonNode?> ListDefectFoldersAsync(string date, CancellationToken ct = default)
        => SendCommandAsync("LIST_DEFECT_FOLDERS", new JsonObject { ["date"] = date }, ct);

    /// <summary>命令 IP 就地把選中 panel 的缺陷檔歸類到 output/{outputSubdir}（by_id → 依前綴建子夾）。</summary>
    public Task<JsonNode?> SortDefectsAsync(string date, string outputSubdir, bool byId,
                                            System.Collections.Generic.IEnumerable<string> folders,
                                            CancellationToken ct = default)
    {
        var arr = new JsonArray();
        foreach (var f in folders) arr.Add(f);
        return SendCommandAsync("SORT_DEFECTS",
            new JsonObject
            {
                ["date"] = date,
                ["output_subdir"] = outputSubdir,
                ["by_id_folder"] = byId,
                ["selected_folders"] = arr,
            }, ct);
    }

    /// <summary>列出一塊 panel 夾的所有缺陷小圖 metadata（座標/型別/Size/目前分類）。</summary>
    public Task<JsonNode?> ListDefectPatchesAsync(string date, string folderName, CancellationToken ct = default)
        => SendCommandAsync("LIST_DEFECT_PATCHES",
            new JsonObject { ["date"] = date, ["folder_name"] = folderName }, ct);

    /// <summary>批次取回多張小圖的 PNG bytes（base64）。一次 ~50 張避免逐張往返。</summary>
    public Task<JsonNode?> GetDefectPatchesBatchAsync(string date, string folderName,
                                                      System.Collections.Generic.IEnumerable<string> patchIds,
                                                      CancellationToken ct = default)
    {
        var arr = new JsonArray();
        foreach (var id in patchIds) arr.Add(id);
        return SendCommandAsync("GET_DEFECT_PATCHES_BATCH",
            new JsonObject { ["date"] = date, ["folder_name"] = folderName, ["patch_ids"] = arr }, ct);
    }

    /// <summary>人工分類結果送 IP：依 TrueDefect/Particle 歸檔到子夾 + 存 classification.json。</summary>
    public Task<JsonNode?> SaveDefectClassificationAsync(string date, string folderName,
            System.Collections.Generic.IEnumerable<(string patchId, string klass)> classifications,
            CancellationToken ct = default)
    {
        var arr = new JsonArray();
        foreach (var (pid, k) in classifications)
            arr.Add(new JsonObject { ["patch_id"] = pid, ["class"] = k });
        return SendCommandAsync("SAVE_DEFECT_CLASSIFICATION",
            new JsonObject { ["date"] = date, ["folder_name"] = folderName, ["classifications"] = arr }, ct);
    }

    /// <summary>送一張 Mono8 影像（命令行 + 緊接 raw payload）。</summary>
    public async Task<JsonNode?> SendImageForReviewAsync(
        string panelId, int camId, int width, int height, int frameSeq,
        byte[] payload, bool last = true, bool debug = false, CancellationToken ct = default)
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
                    ["debug"] = debug,   // true → IP 存全部缺陷小圖（供 DefectSort）；預設只存結果+overlay
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

/// <summary>逐 byte 安全讀「一行（\n 結尾）」，不吃掉後續 binary（本連線伺服器只回 JSON 行）。
/// 累積 raw bytes 到換行後「整行以 UTF-8 解碼」——不可逐 byte 轉 char，否則中文等多位元組 UTF-8 會亂碼。</summary>
internal sealed class StreamReaderLite
{
    private readonly NetworkStream _s;
    public StreamReaderLite(NetworkStream s) => _s = s;

    public async Task<string?> ReadLineAsync(CancellationToken ct)
    {
        var buf = new System.Collections.Generic.List<byte>(256);
        var one = new byte[1];
        while (true)
        {
            int n = await _s.ReadAsync(one, 0, 1, ct);
            if (n <= 0) return buf.Count == 0 ? null : Decode(buf);
            byte b = one[0];
            if (b == (byte)'\n') return Decode(buf);
            if (b != (byte)'\r') buf.Add(b);
        }
    }

    private static string Decode(System.Collections.Generic.List<byte> buf)
        => Encoding.UTF8.GetString(buf.ToArray());   // 整行 UTF-8 解碼（支援中文多位元組）
}
