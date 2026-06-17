using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace CfAoiControl.Controllers;

/// <summary>
/// TCP server ← 上位機（Master Controller）。遷移自 legacy PrjCfAoi/Class/MainProc.cs，移除 MIL/Camera。
///
/// ★ 真實協議（考古自 frmCfAoi/Common.cs/MainProc.cs，取代先前錯誤的 LoadRecipe|RECIPE|PANEL 假設）：
///   - port **8787**、命令前綴 **CF_**、'|' 分隔、'\r\n' 結尾。
///   - 回應走 9 參數：`OK|p1|p2|…|p9` 或 `ERR|…|{errMsg在p9}`。
///   命令：
///   - CF_LOAD_RECIPE|{recipe}|{panelId}|{yyyy-MM-dd-HH-mm-ss}|||||||{detectMode 0=inline/1=offline}
///   - CF_GRAB_START|{timeoutMs}                （範例 40000）
///   - CF_CHECK_ALIGN                            → OK|{camStatus}|{shiftX}|{shiftY}
///   - CF_SET_ALIGN|{result}|{shiftX}|{shiftY}
///   - CF_GET_RESULT                             → OK|{IP0_ResultInfo.xml,IP1_...}|{900,50,...}
///     （回各 IP 的 ResultInfo.xml 路徑 + 缺陷數，逗號分隔，**非 JSON**）
/// 內部把命令轉成對 IP 的 JSON 呼叫（透過 callbacks，ViewModel 不直接碰 TCP）。
/// 不變式：上位機命令名稱/格式不可更改；連線失敗不阻塞啟動。
/// </summary>
public sealed class UpstreamServer : IDisposable
{
    public const string CF_READY        = "CF_READY";
    public const string CF_LOAD_RECIPE  = "CF_LOAD_RECIPE";
    public const string CF_GRAB_START   = "CF_GRAB_START";
    public const string CF_CHECK_ALIGN  = "CF_CHECK_ALIGN";
    public const string CF_SET_ALIGN    = "CF_SET_ALIGN";
    public const string CF_GET_RESULT   = "CF_GET_RESULT";

    public sealed record GetResultPayload(string FilePaths, string DefectCounts);

    // CHECK_ALIGN 回應：ok=false 代表對位失敗（score 過低），由上位機決策（釘點 3）
    public sealed record CheckAlignPayload(bool Ok, double ShiftX, double ShiftY, string Error = "");

    // (recipe, panelId, detectMode) → 成功與否
    public Func<string, string, string, Task<bool>>? OnLoadRecipe { get; set; }
    public Func<string, Task<bool>>? OnGrabStart { get; set; }   // (timeoutMs)
    public Func<Task<GetResultPayload>>? OnGetResult { get; set; }
    // CHECK_ALIGN：裁搜尋 ROI → IP → 回 ShiftX/Y（失敗 ok=false，由上位機決策，釘點 3）
    public Func<Task<CheckAlignPayload>>? OnCheckAlign { get; set; }
    // SET_ALIGN：把上位機傳來的 shiftX/Y 轉給 IP 套回 zones
    public Func<double, double, Task>? OnSetAlign { get; set; }

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
            _listener = new TcpListener(IPAddress.Any, _port);   // 真實上位機 port = 8787
            _listener.Start();
            IsListening = true;
            _log?.Invoke($"[Upstream] 監聽 port {_port}（CF_ 協議）");
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
                var p = line.Split('|');
                var cmd = p[0].Trim().ToUpperInvariant();
                string P(int i) => i < p.Length ? p[i] : "";
                try
                {
                    switch (cmd)
                    {
                        case CF_LOAD_RECIPE:
                        {
                            // p1=recipe, p2=panelId, p3=datetime, p9=detectMode
                            var ok = OnLoadRecipe is null || await OnLoadRecipe(P(1), P(2), P(9));
                            await writer.WriteLineAsync(Resp(ok, errMsg: ok ? "" : "load recipe failed"));
                            break;
                        }
                        case CF_GRAB_START:
                        {
                            var ok = OnGrabStart is null || await OnGrabStart(P(1));
                            await writer.WriteLineAsync(Resp(ok, errMsg: ok ? "" : "grab failed"));
                            break;
                        }
                        case CF_CHECK_ALIGN:
                        {
                            if (OnCheckAlign is null)
                            {
                                // 未接線（Step 1 以前）：回 OK + 0 偏移
                                await writer.WriteLineAsync(Resp(true, p1: "Cs_AlignSet", p2: "0", p3: "0"));
                                break;
                            }
                            var ar = await OnCheckAlign();
                            if (!ar.Ok)
                            {
                                // 釘點 3：失敗回 ERR，讓上位機決策
                                await writer.WriteLineAsync(Resp(false, errMsg: ar.Error));
                            }
                            else
                            {
                                // p1=camStatus, p2=shiftX, p3=shiftY（對齊 legacy）
                                await writer.WriteLineAsync(
                                    Resp(true, p1: "Cs_AlignSet",
                                         p2: ar.ShiftX.ToString("F2"),
                                         p3: ar.ShiftY.ToString("F2")));
                            }
                            break;
                        }
                        case CF_SET_ALIGN:
                        {
                            // p2=shiftX, p3=shiftY（由上位機在 CF_CHECK_ALIGN 回應後決定是否套回）
                            double sx = 0, sy = 0;
                            double.TryParse(P(2), out sx);
                            double.TryParse(P(3), out sy);
                            if (OnSetAlign is not null) await OnSetAlign(sx, sy);
                            await writer.WriteLineAsync(Resp(true));
                            break;
                        }
                        case CF_GET_RESULT:
                        {
                            var r = OnGetResult is null ? new GetResultPayload("", "0") : await OnGetResult();
                            await writer.WriteLineAsync(Resp(true, p1: r.FilePaths, p2: r.DefectCounts));
                            break;
                        }
                        case CF_READY:
                            await writer.WriteLineAsync(Resp(true));
                            break;
                        default:
                            await writer.WriteLineAsync(Resp(false, errMsg: $"unknown cmd: {cmd}"));
                            break;
                    }
                }
                catch (Exception ex) { await writer.WriteLineAsync(Resp(false, errMsg: ex.Message)); }
            }
        }
    }

    // 9 參數回應：OK|p1|…|p8|{p9=errMsg} 或 ERR|…
    private static string Resp(bool ok, string p1 = "", string p2 = "", string p3 = "",
                               string p4 = "", string p5 = "", string p6 = "", string p7 = "",
                               string p8 = "", string errMsg = "")
        => $"{(ok ? "OK" : "ERR")}|{p1}|{p2}|{p3}|{p4}|{p5}|{p6}|{p7}|{p8}|{errMsg}";

    public void Dispose()
    {
        _cts?.Cancel();
        try { _listener?.Stop(); } catch { }
        IsListening = false;
    }
}
