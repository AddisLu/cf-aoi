using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using CfAoiControl.Controllers;
using CfAoiControl.Models;
using Xunit;

namespace CfAoiControl.Tests;

/// <summary>
/// 8-way 速度確認：行速率（AcquisitionLineRateAbs/ResultingLineRateAbs）決定 8-Way 演算法吃到的
/// 影像比例尺（見 grab/CLAUDE.md 不變式 11）。歷史真實事故：CCD01/02 被 UserSet 鎖在 11,001.1Hz、
/// CCD03/04 跑 12,195.1Hz，同批相機吞吐/幾何差 11%，且單看連線/fps 完全看不出來。
/// 本測試驗證 Control 端現在能逐台查詢、解析並判定行速率是否符合產線預期。
/// </summary>
public class SpeedVerificationTests
{
    // ---- 純邏輯：門檻判定（不需連線）----

    [Fact]
    public void LineRate_AtProductionTarget_IsOk()
    {
        var nodes = new CamNodesModel { LineRateSet = 12000.0, LineRateResulting = 12195.122 };
        Assert.True(nodes.IsLineRateOk(expectedHz: 12000));
    }

    [Fact]
    public void LineRate_LockedAtStaleUserSetValue_IsNotOk()
    {
        // 真實事故值：CCD01/02 出廠 UserSet 鎖在 11,001.1Hz，與 96mm/s÷8µm/line 的 12,000Hz 預期差 11%。
        var nodes = new CamNodesModel { LineRateSet = 11001.1, LineRateResulting = 12195.122 };
        Assert.False(nodes.IsLineRateOk(expectedHz: 12000));
    }

    [Fact]
    public void LineRate_NeverExplicitlySet_IsNotOk()
    {
        // grab 未設定節點時 line_rate_set 預設 0（見 GrabClient.GetCamNodesAsync）→ 不可誤判為「正常」。
        var nodes = new CamNodesModel { LineRateSet = 0, LineRateResulting = 12195.122 };
        Assert.False(nodes.IsLineRateOk(expectedHz: 12000));
    }

    [Fact]
    public void LineRate_ExceedsCameraCeiling_IsNotOk()
    {
        // 設定值不可能超過相機算出的實際上限；若發生，代表資料不一致，不應判為正常。
        var nodes = new CamNodesModel { LineRateSet = 13000.0, LineRateResulting = 12195.122 };
        Assert.False(nodes.IsLineRateOk(expectedHz: 12000));
    }

    // ---- 端到端：GrabClient 透過 TCP 依 cam_id 查詢、解析 line_rate_set/resulting ----

    [Fact]
    public async Task GetCamNodesAsync_RoutesByCamId_AndParsesLineRateFields()
    {
        using var server = new FakeGrabServer();
        await server.StartAsync();

        using var grab = new GrabClient();
        await grab.ConnectAsync("127.0.0.1", server.Port);

        var cam0 = await grab.GetCamNodesAsync(0);
        var cam1 = await grab.GetCamNodesAsync(1);

        Assert.NotNull(cam0);
        Assert.NotNull(cam1);
        Assert.Equal(12000.0, cam0!.LineRateSet);
        Assert.Equal(11001.1, cam1!.LineRateSet);
        Assert.Equal(12195.122, cam0.LineRateResulting);
        Assert.Equal(12195.122, cam1.LineRateResulting);
    }

    [Fact]
    public async Task CheckLineRateAsync_DistinguishesHealthyFromDivergedCamera()
    {
        using var server = new FakeGrabServer();
        await server.StartAsync();

        using var grab = new GrabClient();
        await grab.ConnectAsync("127.0.0.1", server.Port);

        Assert.True(await grab.CheckLineRateAsync(0, expectedHz: 12000));
        Assert.False(await grab.CheckLineRateAsync(1, expectedHz: 12000));
    }

    /// <summary>假 grab server：cam0 回產線預期值(12,000Hz)、cam1 回歷史分歧值(11,001.1Hz)，
    /// 兩者 line_rate_resulting 皆為相機實測上限 12,195.122Hz。依 GET_CAM_NODES params.cam_id 路由
    /// （對齊 grab/src/control_server.cpp 的 cam_id 路由協議）。</summary>
    private sealed class FakeGrabServer : System.IDisposable
    {
        private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
        public int Port => ((IPEndPoint)_listener.LocalEndpoint).Port;

        public Task StartAsync()
        {
            _listener.Start();
            _ = Task.Run(async () =>
            {
                using var cli = await _listener.AcceptTcpClientAsync();
                using var ns = cli.GetStream();
                var rd = new System.IO.StreamReader(ns, Encoding.UTF8);
                while (await rd.ReadLineAsync() is { } line && line.Length > 0)
                {
                    var req = JsonNode.Parse(line)!;
                    var seq = (int?)req["seq"] ?? 0;
                    string resp;
                    if (req["cmd"]!.GetValue<string>() == "GET_CAM_NODES")
                    {
                        int camId = req["params"]?["cam_id"]?.GetValue<int>() ?? 0;
                        double lineRate = camId == 1 ? 11001.1 : 12000.0;
                        resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"cam_id\":{camId},\"nodes\":{{" +
                               "\"pixel_format\":\"Mono8\",\"exposure_auto\":\"Off\",\"gain_auto\":\"Off\"," +
                               "\"trigger_mode\":\"Off\",\"trigger_selector\":\"LineStart\",\"trigger_source\":\"Line3\"," +
                               "\"width\":8192,\"height\":2500,\"packet_size\":9000,\"scpd\":0," +
                               $"\"line_rate_set\":{lineRate},\"line_rate_resulting\":12195.122}}}}";
                    }
                    else resp = $"{{\"seq\":{seq},\"status\":\"OK\"}}";
                    await ns.WriteAsync(Encoding.UTF8.GetBytes(resp + "\n"));
                    await ns.FlushAsync();
                }
            });
            return Task.CompletedTask;
        }

        public void Dispose() => _listener.Stop();
    }
}
