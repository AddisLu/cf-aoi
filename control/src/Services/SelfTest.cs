using System;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Xml.Serialization;
using CfAoiControl.Controllers;
using CfAoiControl.Models;

namespace CfAoiControl.Services;

/// <summary>
/// 無頭（headless）跨程式對齊驗證，對應計畫 Verification 2/3/4。CI smoke test 用。
/// 用法：
///   --selftest parse  &lt;ipJson&gt; &lt;ipXml&gt;        驗證 IP 的 JSON/XML 都能解析且值一致（step 3）
///   --selftest recipe &lt;recipeName&gt;             產生 RecipeInfo.xml 供 IP 載入（step 2 前半）
///   --selftest send   &lt;image&gt; &lt;recipeName&gt;     offline-tcp：連 IP 送圖讀回結果（step 4）
/// </summary>
public static class SelfTest
{
    public static async Task<int> RunAsync(string[] args)
    {
        var log = new LogService();
        log.Logged += e => Console.WriteLine($"  [{e.Level}] {e.Message}");
        var cfg = ConfigLoader.Load();

        var sub = args.SkipWhile(a => a != "--selftest").Skip(1).FirstOrDefault();
        var rest = args.SkipWhile(a => a != "--selftest").Skip(2).ToArray();

        try
        {
            switch (sub)
            {
                case "parse":  return ParseTest(rest);
                case "recipe": return await RecipeTest(rest, cfg, log);
                case "send":   return await SendTest(rest, cfg, log);
                case "fft":    return FftTest(rest);
                case "store":  return await StoreTest(rest);
                case "heartbeat": return await HeartbeatTest(rest);
                default:
                    Console.WriteLine("用法: --selftest parse|recipe|send|fft|store ...");
                    return 2;
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"✗ SelfTest 例外: {ex.Message}");
            return 1;
        }
    }

    // ---- 連線心跳偵測（綠↔紅 + 自動重連）----
    private static async Task<int> HeartbeatTest(string[] a)
    {
        int secs = a.Length > 0 && int.TryParse(a[0], out var s) ? s : 30;
        string host = a.Length > 1 ? a[1] : "127.0.0.1";
        var svc = AppServices.Build();
        if (svc.Config.Nodes.TryGetValue("IpOffline", out var n)) n.Host = host;   // 本機測試用
        svc.Log.Logged += e => Console.WriteLine($"   LOG[{e.Level}] {e.Message}");
        svc.Connection.Start(svc.Config, svc.Log);
        for (int i = 0; i < secs; i++)
        {
            Console.WriteLine($"  t={i,2}s IsIpConnected={svc.Connection.IsIpConnected}");
            await Task.Delay(1000);
        }
        return 0;
    }

    // ---- 配方單一資料來源（共用實例 + 存檔）----
    private static async Task<int> StoreTest(string[] a)
    {
        var name = a.Length > 0 ? a[0] : "SYNC_TEST";
        var svc = AppServices.Build();
        await svc.RecipeStore.SelectAsync(name);

        var step1 = new ViewModels.Step1ViewModel(svc);
        var zone = new ViewModels.ZoneParamEditorViewModel(svc);
        var z = svc.RecipeStore.PrimaryZone!;
        z.PitchX = 33; z.PitchY = 17; z.BrightThreshold = 1.55; z.DarkThreshold = 0.55;

        bool shared = ReferenceEquals(step1.Store.PrimaryZone, z)
                      && ReferenceEquals(zone.Store.PrimaryZone, z)
                      && zone.Rois.Count > 0 && ReferenceEquals(zone.Rois[0].Zone, z);
        Console.WriteLine($"  改 Store.PrimaryZone.PitchX=33 → step1={step1.Store.PrimaryZone?.PitchX}, zoneRoi0={zone.Rois.FirstOrDefault()?.Zone.PitchX}");

        await svc.RecipeStore.SaveAsync();
        var path = svc.Recipes.RecipeXmlPath(name);
        var xml = System.IO.File.ReadAllText(path);
        bool xmlOk = xml.Contains("<PitchX>33</PitchX>") && xml.Contains("<PitchY>17</PitchY>")
                     && xml.Contains("<BrightThreshold>1.55</BrightThreshold>");
        Console.WriteLine($"  XML: {path}");
        Console.WriteLine(shared ? "✓ 三處共用同一 ZoneSettingModel 實例" : "✗ 非同一實例");
        Console.WriteLine(xmlOk ? "✓ 存檔 XML 含 PitchX=33/PitchY=17/BrightThreshold=1.55" : "✗ XML 值不符");
        return shared && xmlOk ? 0 : 1;
    }

    // ---- FFT Pitch 估算（驗證純 managed FFT 邏輯）----
    private static int FftTest(string[] a)
    {
        if (a.Length < 1) { Console.WriteLine("fft 需要 <image>"); return 2; }
        using var img = SixLabors.ImageSharp.Image.Load<SixLabors.ImageSharp.PixelFormats.L8>(a[0]);
        int w = img.Width, h = img.Height;
        var px = new byte[w * h];
        img.CopyPixelDataTo(px);
        var r = PitchEstimator.Estimate(px, w, h);
        Console.WriteLine($"  影像 {w}x{h}");
        Console.WriteLine($"  PitchX≈{r.PitchX}（{r.ConfX}, SNR={r.SnrX:F1}, ok={r.OkX}）");
        Console.WriteLine($"  PitchY≈{r.PitchY}（{r.ConfY}, SNR={r.SnrY:F1}, ok={r.OkY}）");
        bool sane = r.PitchX is > 4 and < 150 && r.PitchY is > 4 and < 150;
        Console.WriteLine(sane ? "✓ FFT 估算落在合理範圍" : "✗ FFT 估算超出合理範圍");
        return sane ? 0 : 1;
    }

    // ---- step 3：IP JSON 與 XML 都能解析且值一致 ----
    private static int ParseTest(string[] a)
    {
        if (a.Length < 2) { Console.WriteLine("parse 需要 <json> <xml>"); return 2; }
        var json = OfflineReviewService.ParseJson(File.ReadAllText(a[0]));
        Console.WriteLine($"  JSON: DefectCnt={json.DefectCnt} rois={json.RoiInfoList.Count} allDefects={json.AllDefects.Count()}");
        var d0j = json.AllDefects.FirstOrDefault();
        if (d0j != null) Console.WriteLine($"  JSON defect[0]: Type={d0j.Type} GC=({d0j.GcX},{d0j.GcY}) Size={d0j.Size} {d0j.Width}x{d0j.Height} Global=({d0j.GlobalPosX},{d0j.GlobalPosY}) GL_Mean={d0j.GlMean}");

        var ser = new XmlSerializer(typeof(DefectResultModel));
        using var fs = File.OpenRead(a[1]);
        var xml = (DefectResultModel)ser.Deserialize(fs)!;
        Console.WriteLine($"  XML : DefectCnt={xml.DefectCnt} rois={xml.RoiInfoList.Count} allDefects={xml.AllDefects.Count()}");
        var d0x = xml.AllDefects.FirstOrDefault();
        if (d0x != null) Console.WriteLine($"  XML  defect[0]: Type={d0x.Type} GC=({d0x.GcX},{d0x.GcY}) Size={d0x.Size} {d0x.Width}x{d0x.Height}");

        bool ok = json.DefectCnt == xml.DefectCnt
                  && json.AllDefects.Count() == xml.AllDefects.Count()
                  && d0j != null && d0x != null
                  && d0j.GcX == d0x.GcX && d0j.GcY == d0x.GcY
                  && d0j.Size == d0x.Size && d0j.Type == d0x.Type;
        Console.WriteLine(ok ? "✓ step3 JSON/XML 解析一致" : "✗ step3 JSON 與 XML 不一致");
        return ok ? 0 : 1;
    }

    // ---- step 2 前半：產生 Control 配方 XML 供 IP 載入 ----
    private static async Task<int> RecipeTest(string[] a, SystemConfigModel cfg, LogService log)
    {
        if (a.Length < 1) { Console.WriteLine("recipe 需要 <recipeName>"); return 2; }
        var svc = new RecipeService(cfg, log);
        var r = await svc.EnsureRecipeExistsAsync(a[0]);
        Console.WriteLine($"✓ 配方寫出: {r.RecipeXmlPath} (autoGen={r.IsAutoGenerated})");
        Console.WriteLine($"  → 用此指令確認 IP 可載入(且為 DIV)：");
        Console.WriteLine($"    cfaoi_ip --mode offline-file --recipe '{r.RecipeXmlPath}' --input <img>");
        return 0;
    }

    // ---- step 4：offline-tcp 串接 ----
    private static async Task<int> SendTest(string[] a, SystemConfigModel cfg, LogService log)
    {
        if (a.Length < 2) { Console.WriteLine("send 需要 <image> <recipeName>"); return 2; }
        var node = cfg.ActiveIp ?? new NodeConfig { Host = "127.0.0.1", Port = 8200 };
        using var ip = new IpClient();
        await ip.ConnectAsync(node.Host, node.Port);
        Console.WriteLine($"  已連線 IP {node.Host}:{node.Port}");
        var health = await ip.CheckHealthAsync();
        Console.WriteLine($"  CHECK_HEALTH: {health?.ToJsonString()}");

        var recipes = new RecipeService(cfg, log);
        var review = new OfflineReviewService(ip, recipes, cfg, log);
        var outcome = await review.AnalyzeImageAsync(a[0], a[1]);
        var res = outcome.Result;
        Console.WriteLine($"✓ step4 收回結果: DefectCnt={res.DefectCnt} pass={res.IsPass} rois={res.RoiInfoList.Count}");
        var d0 = res.AllDefects.FirstOrDefault();
        if (d0 != null) Console.WriteLine($"  defect[0]: Type={d0.Type} GC=({d0.GcX},{d0.GcY}) Size={d0.Size}");
        return res.RoiInfoList.Count > 0 ? 0 : 1;
    }
}
