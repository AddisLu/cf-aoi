using System;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using CfAoiControl.Models;

namespace CfAoiControl.Controllers;

/// <summary>
/// TCP → Grab（預設 port 8100），newline-delimited JSON。本階段（Step 1 offline）不取像，
/// 先提供連線/健康檢查骨架，Step 2+ 再擴充取像命令。
/// </summary>
public sealed class GrabClient : IDisposable, IHeartbeatClient
{
    private readonly SemaphoreSlim _lock = new(1, 1);
    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private StreamReaderLite? _reader;
    private int _seq;

    public string Host { get; private set; } = "127.0.0.1";
    public int Port { get; private set; } = 8100;
    public bool IsConnected => _tcp?.Connected == true;
    public bool IsBusy => _lock.CurrentCount == 0;
    public void Disconnect() => Close();

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

    // Gap #2：設定相機曝光/增益；回傳 read-back actual 值。
    public async Task<CamParamsResult?> SetCamParamsAsync(
        int camId, double exposureUs, int gainRaw, CancellationToken ct = default)
    {
        var prms = new JsonObject
        {
            ["cam_id"]      = camId,
            ["exposure_us"] = exposureUs,
            ["gain_raw"]    = gainRaw,
        };
        var resp = await SendCommandAsync("SET_CAM_PARAMS", prms, ct);
        if (resp?["status"]?.GetValue<string>() != "OK") return null;
        return new CamParamsResult
        {
            ExposureUs       = resp["exposure_us"]?.GetValue<double>()        ?? exposureUs,
            GainRaw          = resp["gain_raw"]?.GetValue<int>()              ?? gainRaw,
            ExposureUsActual = resp["exposure_us_actual"]?.GetValue<double>() ?? exposureUs,
            GainRawActual    = resp["gain_raw_actual"]?.GetValue<int>()       ?? gainRaw,
        };
    }

    // Gap #2：讀取相機目前曝光/增益（相機未開時回傳 cam_config.json 值）。
    public async Task<CamParamsResult?> GetCamParamsAsync(
        int camId = 0, CancellationToken ct = default)
    {
        var prms = new JsonObject { ["cam_id"] = camId };
        var resp = await SendCommandAsync("GET_CAM_PARAMS", prms, ct);
        if (resp?["status"]?.GetValue<string>() != "OK") return null;
        var exp  = resp["exposure_us"]?.GetValue<double>() ?? 0;
        var gain = resp["gain_raw"]?.GetValue<int>()       ?? 256;
        return new CamParamsResult
        {
            ExposureUs       = exp,
            GainRaw          = gain,
            ExposureUsActual = exp,
            GainRawActual    = gain,
        };
    }

    // 調參效果確認：TUNE_MEAN（開相機免 RDMA → 設曝光/增益 → 抓 1 幀回 mean gray）。
    public async Task<CamParamsResult?> TuneMeanAsync(
        int camId, double exposureUs, int gainRaw, CancellationToken ct = default)
    {
        var prms = new JsonObject
        {
            ["cam_id"]      = camId,
            ["exposure_us"] = exposureUs,
            ["gain_raw"]    = gainRaw,
        };
        var resp = await SendCommandAsync("TUNE_MEAN", prms, ct);
        if (resp?["status"]?.GetValue<string>() != "OK") return null;
        return new CamParamsResult
        {
            ExposureUs       = exposureUs,
            GainRaw          = gainRaw,
            ExposureUsActual = resp["exposure_us_actual"]?.GetValue<double>() ?? exposureUs,
            GainRawActual    = resp["gain_raw_actual"]?.GetValue<int>()       ?? gainRaw,
            MeanGray         = resp["mean_gray"]?.GetValue<double>()          ?? -1,
        };
    }

    // GET_CAM_NODES：讀回 GigE 機器層參數（PixelFormat/Auto/Trigger/ROI/封包），供 UI 顯示。
    public async Task<CamNodesModel?> GetCamNodesAsync(CancellationToken ct = default)
    {
        var resp = await SendCommandAsync("GET_CAM_NODES", null, ct);
        if (resp?["status"]?.GetValue<string>() != "OK") return null;
        var n = resp["nodes"];
        if (n is null) return null;
        return new CamNodesModel
        {
            PixelFormat     = n["pixel_format"]?.GetValue<string>()     ?? "",
            ExposureAuto    = n["exposure_auto"]?.GetValue<string>()    ?? "",
            GainAuto        = n["gain_auto"]?.GetValue<string>()        ?? "",
            TriggerMode     = n["trigger_mode"]?.GetValue<string>()     ?? "",
            TriggerSelector = n["trigger_selector"]?.GetValue<string>() ?? "",
            TriggerSource   = n["trigger_source"]?.GetValue<string>()   ?? "",
            Width           = n["width"]?.GetValue<long>()      ?? 0,
            Height          = n["height"]?.GetValue<long>()     ?? 0,
            PacketSize      = n["packet_size"]?.GetValue<long>() ?? 0,
            Scpd            = n["scpd"]?.GetValue<long>()        ?? 0,
        };
    }

    // 相機陣列總覽：LIST_CAMERAS（唯讀列舉）。回傳每台 {cam_id,mac,model,ip,online,persistent,...}。
    public async Task<System.Collections.Generic.List<CameraInfoModel>?> ListCamerasAsync(CancellationToken ct = default)
    {
        var resp = await SendCommandAsync("LIST_CAMERAS", null, ct);
        if (resp?["status"]?.GetValue<string>() != "OK") return null;
        var list = new System.Collections.Generic.List<CameraInfoModel>();
        if (resp["cameras"] is JsonArray arr)
        {
            foreach (var n in arr)
            {
                if (n is null) continue;
                list.Add(new CameraInfoModel
                {
                    CamId       = n["cam_id"]?.GetValue<int>()        ?? 0,
                    Mac         = n["mac"]?.GetValue<string>()        ?? "",
                    Model       = n["model"]?.GetValue<string>()      ?? "",
                    Serial      = n["serial"]?.GetValue<string>()     ?? "",
                    Ip          = n["ip"]?.GetValue<string>()         ?? "",
                    Online      = n["online"]?.GetValue<bool>()       ?? true,
                    Persistent  = n["persistent"]?.GetValue<bool>()   ?? false,
                    IpConfig    = n["ip_config"]?.GetValue<string>()  ?? "",
                    DeviceClass = n["device_class"]?.GetValue<string>() ?? "",
                });
            }
        }
        return list;
    }

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
