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
                case "sort":   return await SortTest();
                case "patches": return await PatchClassifyTest();
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

    // ---- 缺陷整理 Parse + Sort 複製 ----
    // 遠端 DefectSort：用 in-process 假 IP server 模擬 LIST_DEFECT_FOLDERS / SORT_DEFECTS，
    // 驗證 Control 端送命令 + 解析回應 + 填 DataGrid + log（不需 GPU/真 IP）。
    private static async Task<int> SortTest()
    {
        // ---- 假 IP server：回 newline-delimited JSON ----
        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        int port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        string? lastSort = null;
        var serverTask = Task.Run(async () =>
        {
            using var cli = await listener.AcceptTcpClientAsync();
            using var ns = cli.GetStream();
            var rd = new System.IO.StreamReader(ns, System.Text.Encoding.UTF8);
            while (await rd.ReadLineAsync() is { } line && line.Length > 0)
            {
                var req = System.Text.Json.Nodes.JsonNode.Parse(line)!;
                var cmd = req["cmd"]!.GetValue<string>();
                var seq = (int?)req["seq"] ?? 0;
                string resp;
                if (cmd == "LIST_DEFECT_FOLDERS")
                    resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"folders\":[" +
                           "{\"folder_name\":\"IP01_panelA\",\"panel_id\":\"IP01_panelA\",\"defect_count\":3}," +
                           "{\"folder_name\":\"IP01_panelB\",\"panel_id\":\"IP01_panelB\",\"defect_count\":5}]}";
                else if (cmd == "SORT_DEFECTS")
                {
                    lastSort = line;
                    var sel = req["params"]!["selected_folders"]!.AsArray();
                    var byId = (bool?)req["params"]!["by_id_folder"] ?? false;
                    var sub = req["params"]!["output_subdir"]!.GetValue<string>();
                    var items = string.Join(",", sel.Select(s => $"{{\"folder\":\"{s!.GetValue<string>()}\",\"copied\":2}}"));
                    resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"results\":[{items}],\"total\":{sel.Count * 2}," +
                           $"\"output_dir\":\"output/{sub}{(byId ? "/IP01" : "")}\"}}";
                }
                else resp = $"{{\"seq\":{seq},\"status\":\"OK\"}}";
                var bytes = System.Text.Encoding.UTF8.GetBytes(resp + "\n");
                await ns.WriteAsync(bytes);
                await ns.FlushAsync();
            }
        });

        var svc = AppServices.Build();
        await svc.Connection.Ip.ConnectAsync("127.0.0.1", port);

        var vm = new ViewModels.DefectSortViewModel(svc) { OutputFolder = "sorted" };
        await vm.ParseCommand.ExecuteAsync(null);
        Console.WriteLine($"  Parse → {vm.Folders.Count} 資料夾: " +
            string.Join(", ", vm.Folders.Select(f => $"{f.FolderName}({f.DefectCount})")));

        vm.SortAll = true;
        await vm.SortCommand.ExecuteAsync(null);
        bool sentList = vm.Folders.Count == 2 && vm.Folders[0].DefectCount == 3;
        bool sentSort = lastSort != null && lastSort.Contains("\"IP01_panelA\"") && lastSort.Contains("\"IP01_panelB\"");
        bool loggedTotal = vm.LogLines.Any(l => l.Contains("共 4 檔"));
        Console.WriteLine($"  Sort(全選) → IP 收到 selected_folders={sentSort}, log 共4檔={loggedTotal}");

        // By ID：命令帶 by_id_folder=true
        var vm2 = new ViewModels.DefectSortViewModel(svc) { OutputFolder = "sorted", ByIdFolder = true, SortAll = true };
        await vm2.ParseCommand.ExecuteAsync(null);
        await vm2.SortCommand.ExecuteAsync(null);
        bool byIdOk = lastSort != null && lastSort.Contains("\"by_id_folder\":true");
        Console.WriteLine($"  By ID → 命令帶 by_id_folder=true: {byIdOk}");

        svc.Connection.Ip.Disconnect();
        listener.Stop();

        bool ok = sentList && sentSort && loggedTotal && byIdOk;
        Console.WriteLine(ok ? "✓ 遠端 LIST/SORT 命令送出 + 回應解析 + log 正確" : "✗ 不符");
        return ok ? 0 : 1;
    }

    // 小圖人工分類：假 IP server 模擬 LIST_DEFECT_PATCHES / GET_DEFECT_PATCHES_BATCH /
    // SAVE_DEFECT_CLASSIFICATION，驗證進資料夾→載 metadata→T/P 分類→存回 IP 的完整鏈。
    private static async Task<int> PatchClassifyTest()
    {
        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        int port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        var saves = new System.Collections.Concurrent.ConcurrentQueue<string>();   // 收到的 SAVE 命令
        // 1x1 PNG base64（headless 無 render backend 時 decode 會回 null，不影響 metadata 測試）
        const string PNG1x1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQAY3Y2wAAAAAElFTkSuQmCC";
        var server = Task.Run(async () =>
        {
            using var cli = await listener.AcceptTcpClientAsync();
            using var ns = cli.GetStream();
            var rd = new System.IO.StreamReader(ns, System.Text.Encoding.UTF8);
            while (await rd.ReadLineAsync() is { } line && line.Length > 0)
            {
                var req = System.Text.Json.Nodes.JsonNode.Parse(line)!;
                var cmd = req["cmd"]!.GetValue<string>();
                var seq = (int?)req["seq"] ?? 0;
                string resp;
                if (cmd == "LIST_DEFECT_PATCHES")
                    resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"folder_name\":\"IP02_panelA_DEFAULT\",\"patches\":[" +
                        "{\"patch_id\":\"Defect_IP02_Slice00_Roi00_Run00_X0010_Y0020_DrBright.png\",\"run_index\":0,\"roi_index\":0,\"GC_X\":10,\"GC_Y\":20,\"Size\":5,\"Type\":\"Bright\",\"current_class\":\"未分類\"}," +
                        "{\"patch_id\":\"Defect_IP02_Slice00_Roi00_Run01_X0030_Y0040_DrDark.png\",\"run_index\":1,\"roi_index\":0,\"GC_X\":30,\"GC_Y\":40,\"Size\":7,\"Type\":\"Dark\",\"current_class\":\"未分類\"}," +
                        "{\"patch_id\":\"Defect_IP02_Slice00_Roi00_Run02_X0050_Y0060_DrBright.png\",\"run_index\":2,\"roi_index\":0,\"GC_X\":50,\"GC_Y\":60,\"Size\":3,\"Type\":\"Bright\",\"current_class\":\"TrueDefect\"}]}";
                else if (cmd == "GET_DEFECT_PATCHES_BATCH")
                {
                    var ids = req["params"]!["patch_ids"]!.AsArray();
                    var items = string.Join(",", ids.Select(id =>
                        $"{{\"patch_id\":\"{id!.GetValue<string>()}\",\"png_base64\":\"{PNG1x1}\"}}"));
                    resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"patches\":[{items}]}}";
                }
                else if (cmd == "SAVE_DEFECT_CLASSIFICATION")
                {
                    saves.Enqueue(line);
                    var cs = req["params"]!["classifications"]!.AsArray();
                    int t = cs.Count(c => c!["class"]!.GetValue<string>() == "TrueDefect");
                    int p = cs.Count(c => c!["class"]!.GetValue<string>() == "Particle");
                    resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"TrueDefect\":{t},\"Particle\":{p},\"total\":{t + p},\"output_dir\":\"/out/IP02_panelA_DEFAULT\"}}";
                }
                else resp = $"{{\"seq\":{seq},\"status\":\"OK\"}}";
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes(resp + "\n"));
                await ns.FlushAsync();
            }
        });

        var svc = AppServices.Build();
        await svc.Connection.Ip.ConnectAsync("127.0.0.1", port);
        var vm = new ViewModels.DefectSortViewModel(svc);

        // 預設 filter「只顯示未分類」：_all=3、其中 1 張(patch2)已是 TrueDefect → 可見只 2 張未分類。
        await vm.OpenFolderAsync("IP02_panelA_DEFAULT");
        bool listed = vm.InPatchView && vm.TotalCount == 3 && vm.ClassifiedCount == 1
                      && vm.Patches.Count == 2
                      && vm.Patches[0].GcX == 10 && vm.Patches[0].Type == "Bright"
                      && vm.Patches[1].Size == 7;
        Console.WriteLine($"  OpenFolder（預設只顯示未分類）→ 全集 {vm.TotalCount}、可見 {vm.Patches.Count}、" +
            $"已分類 {vm.ClassifiedCount}, metadata 正確={listed}");

        // 標選中第 0 張為 TrueDefect → 即時存 + 從未分類視圖消失（剩 1 張）
        vm.SelectedPatch = vm.Patches[0];
        vm.ClassifySelected("TrueDefect");
        bool hiddenAfterClassify = vm.Patches.Count == 1;
        // 對剩下那張按 Particle 鈕 → 也消失（剩 0 張）
        vm.MarkParticleCommand.Execute(vm.Patches[0]);
        Console.WriteLine($"  標 T/P 後 → 未分類視圖剩 {vm.Patches.Count} 張, " +
            $"統計(已分類 {vm.ClassifiedCount}/{vm.TotalCount} T{vm.TrueDefectCount} P{vm.ParticleCount})");

        // 等即時持久化（fire-and-forget）抵達假 server：應有 2 筆 SAVE（T、P 各一）
        for (int i = 0; i < 40 && saves.Count < 2; i++) await Task.Delay(50);
        var saveLines = saves.ToArray();
        bool persistedImmediately = saveLines.Length >= 2
            && saveLines.Any(l => l.Contains("\"class\":\"TrueDefect\""))
            && saveLines.Any(l => l.Contains("\"class\":\"Particle\""));
        Console.WriteLine($"  即時持久化 → 收到 {saveLines.Length} 筆 SAVE（不需按 Sort）={persistedImmediately}");

        // 切換「顯示全部」→ 應看回全部 3 張（含已分類）
        vm.SelectedFilter = "顯示全部";
        bool showAll = vm.Patches.Count == 3;
        vm.SelectedFilter = "只顯示 TrueDefect";
        bool onlyTrue = vm.Patches.Count == 2 && vm.Patches.All(p => p.CurrentClass == "TrueDefect");
        Console.WriteLine($"  filter → 顯示全部={showAll}（3 張）, 只顯示TrueDefect={onlyTrue}（2 張）");

        svc.Connection.Ip.Disconnect();
        listener.Stop();

        bool ok = listed && hiddenAfterClassify && persistedImmediately && showAll && onlyTrue
                  && vm.ClassifiedCount == 3 && vm.TrueDefectCount == 2 && vm.ParticleCount == 1;
        Console.WriteLine(ok ? "✓ filter（隱藏已分類）+ 即時持久化 + 統計 + 切換檢視 正確" : "✗ 不符");
        return ok ? 0 : 1;
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
