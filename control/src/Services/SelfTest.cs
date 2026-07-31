using System;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
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
                case "heartbeat": return rest.Length > 0 && rest[0] == "auto"
                                         ? await HeartbeatAutoTest()
                                         : await HeartbeatTest(rest);
                case "sort":   return await SortTest();
                case "patches": return await PatchClassifyTest();
                case "settings": return SettingsTest();
                case "camera": return await CameraTest();
                case "topology": return await TopologyTest();
                case "singleccd": return SingleCcdTest();
                case "upstream": return await UpstreamTest();
                case "grabtrigger": return await GrabTriggerTest();
                case "remoteimg": return await RemoteImgTest();
                case "recipemgmt": return RecipeMgmtTest();
                case "recipesaving": return await RecipeSavingTest(rest);
                case "workbench": return await WorkbenchTest();
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

    // ---- ShareSetting / RecipeSetting 存讀 round-trip ----
    // ShareSetting → appsettings.json（JSON，全域）；RecipeSetting → per-recipe XML。皆用暫存目錄隔離。
    private static int SettingsTest()
    {
        bool ok = true;

        // (1) ShareSetting：寫暫存 appsettings.json → 重讀值一致 + AiRootPath 預設空
        var cfgDir = Path.Combine(Path.GetTempPath(), "cfaoi_settings_cfg");
        Directory.CreateDirectory(cfgDir);
        File.WriteAllText(Path.Combine(cfgDir, "appsettings.json"), "{ \"ActiveIpNode\": \"IpOffline\" }");
        var share = new ShareSettingModel
        {
            SaveSourceImage = true, DebugAlgorithm = true, AiRootPath = "/data/ai",
            TuningRecipe = false, SaveFullImage = true, BypassAlignment = false,
        };
        ConfigLoader.SaveShareSetting(share, cfgDir);
        var reloaded = ConfigLoader.Load(cfgDir);
        var rs = reloaded.ShareSetting;
        bool shareOk = rs.SaveSourceImage && rs.DebugAlgorithm && rs.AiRootPath == "/data/ai"
                       && rs.SaveFullImage && !rs.TuningRecipe && !rs.BypassAlignment
                       && reloaded.ActiveIpNode == "IpOffline";   // 其餘節點保留未被覆蓋
        bool defaultEmpty = new ShareSettingModel().AiRootPath == "";   // 跨平台預設留空（非 O:\Recipe）
        Console.WriteLine($"  ShareSetting round-trip={shareOk}, AiRootPath 預設空={defaultEmpty}");
        ok &= shareOk && defaultEmpty;

        // (2) RecipeSetting：暫存 RecipeDir，存 per-recipe XML → 重讀值一致
        var rcpRoot = Path.Combine(Path.GetTempPath(), "cfaoi_settings_rcp");
        if (Directory.Exists(rcpRoot)) Directory.Delete(rcpRoot, true);
        Directory.CreateDirectory(rcpRoot);
        var cfg = new SystemConfigModel();
        cfg.Paths.RecipeDir = rcpRoot;
        var svc = new RecipeService(cfg, new LogService());
        var model = new RecipeSavingModel
        {
            MaxSaveDefectCount = 123, MaxSaveAiOkCount = 45, SaveDefectWidth = 80, SaveAiTrain = true,
        };
        svc.SaveRecipeSetting("RCP1", model);
        var path = svc.RecipeSettingXmlPath("RCP1");
        var back = svc.LoadRecipeSetting("RCP1");
        bool rcpOk = File.Exists(path)
                     && back.MaxSaveDefectCount == 123 && back.MaxSaveAiOkCount == 45
                     && back.SaveDefectWidth == 80 && back.SaveAiTrain
                     && back.RecipeName == "RCP1"
                     && back.MaxDefectCountPass == 10000;   // 預設＝IP MAX_DEFECTS
        bool missingDefault = svc.LoadRecipeSetting("NOPE").MaxSaveDefectCount == 250;  // 不存在→預設
        Console.WriteLine($"  RecipeSetting round-trip={rcpOk}（{path}）, 不存在回預設={missingDefault}");
        ok &= rcpOk && missingDefault;

        // (3) Step1 Debug 初值＝ShareSetting.DebugAlgorithm（全域預設→Step1 初值）
        var svc2 = AppServices.Build();
        svc2.Config.ShareSetting.DebugAlgorithm = true;
        var step1 = new ViewModels.Step1ViewModel(svc2);
        bool initOk = step1.DebugSaveDefectPatches;
        Console.WriteLine($"  Step1 Debug 初值＝ShareSetting.DebugAlgorithm={initOk}");
        ok &= initOk;

        Console.WriteLine(ok ? "✓ ShareSetting/RecipeSetting 存讀 + Step1 初值 正確" : "✗ 不符");
        return ok ? 0 : 1;
    }

    // ---- 缺陷整理 Parse + Sort 複製 ----
    // 遠端 DefectSort：用 in-process 假 IP server 模擬 LIST_DEFECT_FOLDERS / SORT_DEFECTS，
    // 驗證 Control 端送命令 + 解析回應 + 填 DataGrid + log（不需 GPU/真 IP）。
    // 相機陣列總覽：假 Grab server 回 LIST_CAMERAS 多台（bound + unbound）→ 驗 VM 填表/分群/KPI。
    // 真硬體只有 1 台驗不到分群，正好用假 payload 補。離線群維持空（無 config 映射，不假造離線台 = #21）。
    private static async Task<int> CameraTest()
    {
        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        int port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
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
                if (cmd == "LIST_CAMERAS")
                    // cam0：bound=true → 綁到 CCD00（example 拓樸有此槽）
                    // cam1：**persistent=true 但 bound=false** —— 這是語意衝突的關鍵回歸守門：
                    //       舊版用 persistent 推斷「已綁定」會誤判成已綁；正確答案是「待綁定」。
                    resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"cameras\":[" +
                           "{\"cam_id\":0,\"mac\":\"00:30:53:2A:0B:03\",\"model\":\"raL8192-12gm\",\"serial\":\"25445953\",\"ip\":\"192.168.5.10\",\"online\":true,\"persistent\":true,\"ip_config\":\"Persistent\",\"device_class\":\"BaslerGigE\",\"ccd_id\":\"CCD00\",\"bound\":true}," +
                           "{\"cam_id\":1,\"mac\":\"00:30:53:2B:12:10\",\"model\":\"raL8192-12gm\",\"serial\":\"25445999\",\"ip\":\"192.168.5.11\",\"online\":true,\"persistent\":true,\"ip_config\":\"Persistent\",\"device_class\":\"BaslerGigE\",\"ccd_id\":\"\",\"bound\":false}]}";
                else resp = $"{{\"seq\":{seq},\"status\":\"OK\"}}";
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes(resp + "\n"));
                await ns.FlushAsync();
            }
        });

        var svc = AppServices.Build();
        await svc.Connection.Grab.ConnectAsync("127.0.0.1", port);

        var vm = new ViewModels.SystemSettingsViewModel(svc);
        await vm.LoadCamerasCommand.ExecuteAsync(null);

        bool count   = vm.Cameras.Count == 2;
        // 配置 KPI 已改為宣告數(DeclaredSlotCount)，不再 = 偵測數 → 此處只驗偵測類 KPI（runtime）
        bool kpi      = vm.OnlineCount == 2
                        && vm.BoundCount == 1 && vm.UnboundCount == 1 && vm.OfflineCount == 0;
        bool grouping = vm.BoundCameras.Count == 1 && vm.BoundCameras[0].CamId == 0
                        && vm.UnboundCameras.Count == 1 && vm.UnboundCameras[0].CamId == 1
                        && vm.OfflineCameras.Count == 0;
        bool selected = vm.SelectedCamera is { CamId: 0 } && vm.HasSelection;
        bool fields   = vm.Cameras[1].Mac == "00:30:53:2B:12:10" && vm.Cameras[1].StatusLabel == "待綁定";
        // 新欄位解析（舊版 grab 不回時預設 ""/false，見 GrabClient）
        bool newFields = vm.Cameras[0].Bound && vm.Cameras[0].CcdId == "CCD00"
                         && !vm.Cameras[1].Bound && vm.Cameras[1].CcdId == "";
        // ★ 語意衝突回歸守門：cam1 persistent=true 但 bound=false → 必須是「待綁定」。
        //   舊版（用 persistent 推斷）會答「已綁定」，此斷言即防止回退。
        bool semantics = vm.Cameras[1].Persistent && !vm.Cameras[1].Bound
                         && vm.Cameras[1].Status == Models.CamStatusKind.Unbound;

        Console.WriteLine($"  列舉 2 台: {(count ? "PASS" : "FAIL")}");
        Console.WriteLine($"  KPI(偵測) 上線=2 已綁定=1 待綁定=1 離線=0: {(kpi ? "PASS" : "FAIL")} " +
            $"(實得 on={vm.OnlineCount} b={vm.BoundCount} u={vm.UnboundCount} off={vm.OfflineCount})");
        Console.WriteLine($"  分群 bound[CCD00]/unbound[CCD01]/offline[空]: {(grouping ? "PASS" : "FAIL")}");
        Console.WriteLine($"  預選第一台 + HasSelection: {(selected ? "PASS" : "FAIL")}");
        Console.WriteLine($"  欄位解析 (MAC/狀態標籤): {(fields ? "PASS" : "FAIL")}");
        Console.WriteLine($"  新欄位 ccd_id/bound 解析: {(newFields ? "PASS" : "FAIL")} (cam0 bound={vm.Cameras[0].Bound} ccd={vm.Cameras[0].CcdId})");
        Console.WriteLine($"  ★語意守門 persistent=true 但 bound=false → 待綁定: {(semantics ? "PASS" : "FAIL")} (實得 {vm.Cameras[1].StatusLabel})");

        svc.Connection.Grab.Disconnect();
        listener.Stop();
        bool ok = count && kpi && grouping && selected && fields && newFields && semantics;
        Console.WriteLine(ok ? "✓ 相機總覽：LIST_CAMERAS 解析(含 ccd_id/bound) + 分群 + KPI + 綁定語意不回退"
                             : "✗ 不符");
        return ok ? 0 : 1;
    }

    // ---- 塊1：多 CCD 陣列「宣告」拓樸 + 與「偵測相機」分開（約束②不假 merge）----
    // 餵 fixture topology（部分 expected_mac null/部分有值）+ 假 LIST_CAMERAS（1 台）→ 驗：
    //  ① 槽載入 + 依 compute_unit 分群；② 全部「已宣告·未綁」無人線上；③ 列舉相機獨立、未 merge 進槽。
    private static async Task<int> TopologyTest()
    {
        var json = """
        { "ccd_total_count": 3,
          "compute_units": [ {"id":"Spark1","node":"IpOffline","role":"aoi"}, {"id":"Spark2","node":"IpOnline","role":"ai"} ],
          "slots": [
            {"ccd_id":"CCD00","compute_unit":"Spark1","expected_mac":null,"recipe_partition":"IP0"},
            {"ccd_id":"CCD01","compute_unit":"Spark1","expected_mac":"00:30:53:2A:0B:03","recipe_partition":"IP1"},
            {"ccd_id":"CCD02","compute_unit":"Spark2","expected_mac":null,"recipe_partition":"IP2"} ] }
        """;
        var topo = Models.ArrayTopologyModel.Parse(json);
        bool load = topo.CcdTotalCount == 3 && topo.Slots.Count == 3 && topo.ComputeUnits.Count == 2;
        // 約束①：ccd_id(UI) 與 recipe_partition(儲存鍵) 並存解耦；expected_mac 可 null=TBD
        bool keys = topo.Slots[0].CcdId == "CCD00" && topo.Slots[0].RecipePartition == "IP0"
                    && topo.Slots[0].ExpectedMac is null && topo.Slots[0].ExpectedMacDisplay == "TBD"
                    && topo.Slots[1].ExpectedMac == "00:30:53:2A:0B:03";

        var svc = AppServices.Build();
        var vm = new ViewModels.SystemSettingsViewModel(svc);
        // 案①：建構即載入機台 array_topology.example.json(37 槽) + 尚未列舉相機
        //      → 配置(=DeclaredSlotCount)=37；偵測類 KPI 連線/已綁/待綁/離線 全 0。
        int declaredAtBoot = vm.DeclaredSlotCount;   // 建構載入 example.json 的宣告數（期望 37）
        bool kpiCase1 = declaredAtBoot == 37
                        && vm.OnlineCount == 0 && vm.BoundCount == 0 && vm.UnboundCount == 0 && vm.OfflineCount == 0;
        vm.ApplyTopology(topo);
        var g1 = vm.ComputeUnits.FirstOrDefault(g => g.Unit.Id == "Spark1");
        var g2 = vm.ComputeUnits.FirstOrDefault(g => g.Unit.Id == "Spark2");
        bool group = vm.ComputeUnits.Count == 2 && vm.DeclaredSlotCount == 3
                     && g1 is { } && g1.Slots.Count == 2 && g2 is { } && g2.Slots.Count == 1;
        // 約束②：尚未列舉相機 → 不得有任何槽被標成「就位」。
        // 注意 fixture 的 CCD01 有 expected_mac 卻沒相機 → 正確判為「離線(該就位卻沒出現)」，
        // 這**不是**線上，仍符合「未綁不標線上」；CCD00/CCD02 無 expected_mac → 「已宣告·未綁」。
        bool noOnline = vm.ComputeUnits.SelectMany(g => g.Slots)
                          .All(s => s.Kind is ViewModels.SlotBindKind.Declared or ViewModels.SlotBindKind.Offline)
                        && vm.ComputeUnits.SelectMany(g => g.Slots).Count(s => s.Kind == ViewModels.SlotBindKind.Offline) == 1;

        // 假 LIST_CAMERAS：**舊版 grab 格式（不含 ccd_id/bound）** → 向後相容案例：
        // 應退回今日行為（全部未綁、不崩），不可因缺欄位拋例外。
        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        int port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        var serverTask = Task.Run(async () =>
        {
            using var cli = await listener.AcceptTcpClientAsync();
            using var ns = cli.GetStream();
            var rd = new System.IO.StreamReader(ns, System.Text.Encoding.UTF8);
            while (await rd.ReadLineAsync() is { } line && line.Length > 0)
            {
                var req = System.Text.Json.Nodes.JsonNode.Parse(line)!;
                var seq = (int?)req["seq"] ?? 0;
                string resp = req["cmd"]!.GetValue<string>() == "LIST_CAMERAS"
                    ? $"{{\"seq\":{seq},\"status\":\"OK\",\"cameras\":[{{\"cam_id\":0,\"mac\":\"00:30:53:2B:12:10\",\"model\":\"raL8192-12gm\",\"serial\":\"X\",\"ip\":\"169.254.20.5\",\"online\":true,\"persistent\":false,\"ip_config\":\"AutoIP\",\"device_class\":\"BaslerGigE\"}}]}}"
                    : $"{{\"seq\":{seq},\"status\":\"OK\"}}";
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes(resp + "\n"));
                await ns.FlushAsync();
            }
        });
        await svc.Connection.Grab.ConnectAsync("127.0.0.1", port);
        await vm.LoadCamerasCommand.ExecuteAsync(null);

        bool detectedSeparate = vm.Cameras.Count == 1                                  // 偵測側有 1 台
            && vm.ComputeUnits.SelectMany(g => g.Slots).Count() == 3                    // 宣告槽數不因列舉而變
            && vm.ComputeUnits.SelectMany(g => g.Slots).All(s => s.Camera is null)      // 舊格式無 bound → 沒有任何槽被填
            && vm.Cameras[0].Bound == false && vm.Cameras[0].CcdId == "";               // 缺欄位 → 預設未綁（不崩）
        // 案②：fixture 3 槽 + 假列舉 1 台(unbound) → 配置=宣告數3；偵測類 KPI 反映該 1 台(連線1/待綁1/已綁0/離線0)。
        bool kpiCase2 = vm.DeclaredSlotCount == 3
                        && vm.OnlineCount == 1 && vm.UnboundCount == 1 && vm.BoundCount == 0
                        && vm.OfflineCount == 1;   // CCD01 有 expected_mac 卻沒相機 = 該就位沒出現
        // 快照 phase A/B 的實得值（後面還會換拓樸+換相機，直接印 vm.* 會是 phase C 的值）
        var snapA = $"配置={vm.DeclaredSlotCount} on={vm.OnlineCount} b={vm.BoundCount} u={vm.UnboundCount} off={vm.OfflineCount}";
        var snapGroup = $"units={vm.ComputeUnits.Count} declared={vm.DeclaredSlotCount}";
        int snapCams = vm.Cameras.Count;

        // ── 塊2：運算單元卡三項 ──
        var sp1 = vm.ComputeUnits.First(g => g.Unit.Id == "Spark1");
        var sp2 = vm.ComputeUnits.First(g => g.Unit.Id == "Spark2");
        // (a) 處理 N(真) = 槽數
        bool procN = sp1.SlotCount == 2 && sp2.SlotCount == 1;
        // (b) 負載%(估算) = 公式算出值（非寫死；用同公式獨立重算比對）+ LoadText 標「估算」+ tooltip 寫投影
        int expect1 = (int)System.Math.Round(sp1.SlotCount * 30 * 7.4 / 30000 * 100);
        int expect2 = (int)System.Math.Round(sp2.SlotCount * 30 * 7.4 / 30000 * 100);
        bool loadCalc = sp1.EstLoadPct == expect1 && sp2.EstLoadPct == expect2
                        && sp1.EstMarginPct == 100 - expect1
                        && sp1.LoadText.Contains("估算") && sp1.LoadDetail.Contains("未實機跑滿");
        // (c) 連線(真)：ApplyTopology 後（home IP 未連）兩單元皆 false（不假綠）
        bool connDefault = !sp1.IsConnected && !sp2.IsConnected;
        // 連線規則（純函式，deterministic，不依賴 heartbeat）：active node 連上才綠、非 active 仍灰、未連皆灰
        bool connRule = ViewModels.ComputeUnitGroup.UnitConnected("IpOffline", "IpOffline", true)
                        && !ViewModels.ComputeUnitGroup.UnitConnected("IpOnline", "IpOffline", true)
                        && !ViewModels.ComputeUnitGroup.UnitConnected("IpOffline", "IpOffline", false);

        svc.Connection.Grab.Disconnect();
        listener.Stop();

        // ══ Gap #21：宣告槽 × 偵測相機的真實 join（grab 回 bound/ccd_id 後才可能）══
        // 新拓樸涵蓋四種槽狀態，一次驗完：
        //   CCD10 expected_mac=null  + 有相機綁到        → 已綁定·就位
        //   CCD11 expected_mac 相符   + 有相機綁到        → 已綁定·就位（且 MAC 以**非正規化格式**填，驗正規化）
        //   CCD12 expected_mac 不符   + 有相機綁到        → ⚠ MAC 不符（插錯槽位）
        //   CCD13 expected_mac 有值   + 沒有相機          → 離線（該就位卻沒出現）
        //   CCD14 expected_mac=null  + 沒有相機          → 已宣告·未綁（**不算離線**，開發期不吵）
        // 另有一台 bound=false 的相機 → 絕不可被指派到任何槽（約束②底線）。
        var json2 = """
        { "ccd_total_count": 5,
          "compute_units": [ {"id":"Spark1","node":"IpOffline","role":"aoi"} ],
          "slots": [
            {"ccd_id":"CCD10","compute_unit":"Spark1","expected_mac":null,"recipe_partition":"IP10"},
            {"ccd_id":"CCD11","compute_unit":"Spark1","expected_mac":"003053 2a-0b11","recipe_partition":"IP11"},
            {"ccd_id":"CCD12","compute_unit":"Spark1","expected_mac":"00:30:53:AA:AA:AA","recipe_partition":"IP12"},
            {"ccd_id":"CCD13","compute_unit":"Spark1","expected_mac":"00:30:53:BB:BB:BB","recipe_partition":"IP13"},
            {"ccd_id":"CCD14","compute_unit":"Spark1","expected_mac":null,"recipe_partition":"IP14"} ] }
        """;
        vm.ApplyTopology(Models.ArrayTopologyModel.Parse(json2));

        var l2 = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l2.Start();
        int port2 = ((System.Net.IPEndPoint)l2.LocalEndpoint).Port;
        _ = Task.Run(async () =>
        {
            using var cli = await l2.AcceptTcpClientAsync();
            using var ns = cli.GetStream();
            var rd = new System.IO.StreamReader(ns, System.Text.Encoding.UTF8);
            // CCD11 的相機 MAC 用標準冒號格式，拓樸用 "003053 2a-0b11" → 必須被正規化視為同一台
            const string cams =
                "[{\"cam_id\":0,\"mac\":\"00:30:53:2A:0B:10\",\"model\":\"raL8192-12gm\",\"serial\":\"S10\",\"ip\":\"192.168.5.10\",\"online\":true,\"persistent\":true,\"ip_config\":\"Persistent\",\"device_class\":\"BaslerGigE\",\"ccd_id\":\"CCD10\",\"bound\":true}," +
                "{\"cam_id\":1,\"mac\":\"00:30:53:2A:0B:11\",\"model\":\"raL8192-12gm\",\"serial\":\"S11\",\"ip\":\"192.168.5.11\",\"online\":true,\"persistent\":true,\"ip_config\":\"Persistent\",\"device_class\":\"BaslerGigE\",\"ccd_id\":\"CCD11\",\"bound\":true}," +
                "{\"cam_id\":2,\"mac\":\"00:30:53:CC:CC:CC\",\"model\":\"raL4096-24gm\",\"serial\":\"S12\",\"ip\":\"192.168.5.12\",\"online\":true,\"persistent\":true,\"ip_config\":\"Persistent\",\"device_class\":\"BaslerGigE\",\"ccd_id\":\"CCD12\",\"bound\":true}," +
                "{\"cam_id\":9,\"mac\":\"00:30:53:FF:FF:FF\",\"model\":\"raL4096-24gm\",\"serial\":\"S99\",\"ip\":\"169.254.9.9\",\"online\":true,\"persistent\":false,\"ip_config\":\"AutoIP\",\"device_class\":\"BaslerGigE\",\"ccd_id\":\"\",\"bound\":false}]";
            while (await rd.ReadLineAsync() is { } line && line.Length > 0)
            {
                var req = System.Text.Json.Nodes.JsonNode.Parse(line)!;
                var seq = (int?)req["seq"] ?? 0;
                string resp = req["cmd"]!.GetValue<string>() == "LIST_CAMERAS"
                    ? $"{{\"seq\":{seq},\"status\":\"OK\",\"cameras\":{cams}}}"
                    : $"{{\"seq\":{seq},\"status\":\"OK\"}}";
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes(resp + "\n"));
                await ns.FlushAsync();
            }
        });
        await svc.Connection.Grab.ConnectAsync("127.0.0.1", port2);
        await vm.LoadCamerasCommand.ExecuteAsync(null);

        var slots2 = vm.ComputeUnits.SelectMany(g => g.Slots).ToDictionary(s => s.CcdId);
        bool bindJoin     = slots2["CCD10"].Kind == ViewModels.SlotBindKind.Bound
                            && slots2["CCD10"].Camera?.Serial == "S10";
        bool bindMacNorm  = slots2["CCD11"].Kind == ViewModels.SlotBindKind.Bound;   // 格式不同但同一台
        bool bindMismatch = slots2["CCD12"].Kind == ViewModels.SlotBindKind.MacMismatch;
        bool bindOffline  = slots2["CCD13"].Kind == ViewModels.SlotBindKind.Offline;
        bool bindDeclared = slots2["CCD14"].Kind == ViewModels.SlotBindKind.Declared;  // null mac 無相機 ≠ 離線
        // 約束②底線：bound=false 的相機不得出現在任何槽
        bool unboundNotAssigned = slots2.Values.All(s => s.Camera?.Serial != "S99");
        // KPI：已綁定=有相機的槽數(3，含 MAC 不符仍算就位) / 待綁定=未綁相機數(1) / 離線=該就位沒出現(1)
        bool kpiJoin = vm.BoundCount == 3 && vm.UnboundCount == 1 && vm.OfflineCount == 1
                       && vm.DeclaredSlotCount == 5 && vm.OnlineCount == 4;
        bool warnText = vm.BindingWarning.Contains("CCD12");   // 告警要指出是哪個槽

        svc.Connection.Grab.Disconnect();
        l2.Stop();

        bool join = bindJoin && bindMacNorm && bindMismatch && bindOffline && bindDeclared
                    && unboundNotAssigned && kpiJoin && warnText;
        // 快照 phase C 實得值（下面的綁定動作測試會再換相機清單，直接印 vm.* 會是之後的狀態）
        var snapWarn = vm.BindingWarning;
        var snapKpiC = $"b={vm.BoundCount} u={vm.UnboundCount} off={vm.OfflineCount} d={vm.DeclaredSlotCount} on={vm.OnlineCount}";

        // ══ Gap #21 綁定動作（SET_CAM_MAP）：Control 送**完整**映射表 ══
        // 假 Grab 記下收到的 entries；先驗「拒絕搶槽」，再驗正常綁定送出的內容正確。
        var l3 = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l3.Start();
        int port3 = ((System.Net.IPEndPoint)l3.LocalEndpoint).Port;
        System.Text.Json.Nodes.JsonNode? lastMap = null;
        _ = Task.Run(async () =>
        {
            using var cli = await l3.AcceptTcpClientAsync();
            using var ns = cli.GetStream();
            var rd = new System.IO.StreamReader(ns, System.Text.Encoding.UTF8);
            const string cams =
                "[{\"cam_id\":0,\"mac\":\"00:30:53:2A:0B:10\",\"model\":\"raL8192-12gm\",\"serial\":\"S10\",\"ip\":\"192.168.5.10\",\"online\":true,\"persistent\":true,\"ip_config\":\"Persistent\",\"device_class\":\"BaslerGigE\",\"ccd_id\":\"CCD10\",\"bound\":true}," +
                "{\"cam_id\":9,\"mac\":\"00:30:53:FF:FF:FF\",\"model\":\"raL4096-24gm\",\"serial\":\"S99\",\"ip\":\"169.254.9.9\",\"online\":true,\"persistent\":false,\"ip_config\":\"AutoIP\",\"device_class\":\"BaslerGigE\",\"ccd_id\":\"\",\"bound\":false}]";
            while (await rd.ReadLineAsync() is { } line && line.Length > 0)
            {
                var req = System.Text.Json.Nodes.JsonNode.Parse(line)!;
                var seq = (int?)req["seq"] ?? 0;
                var c = req["cmd"]!.GetValue<string>();
                string resp;
                if (c == "LIST_CAMERAS") resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"cameras\":{cams}}}";
                else if (c == "SET_CAM_MAP")
                {
                    lastMap = req["params"]?["entries"];
                    resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"written\":{lastMap?.AsArray().Count ?? 0},\"path\":\"/tmp/cam_map.json\"}}";
                }
                else resp = $"{{\"seq\":{seq},\"status\":\"OK\"}}";
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes(resp + "\n"));
                await ns.FlushAsync();
            }
        });
        await svc.Connection.Grab.ConnectAsync("127.0.0.1", port3);
        await vm.LoadCamerasCommand.ExecuteAsync(null);

        // ① 拒絕搶槽：把「未綁的 S99」綁到已被 S10 佔用的 CCD10 → 必須擋下且不送出命令
        vm.SelectCameraCommand.Execute(vm.Cameras.First(c => c.Serial == "S99"));
        vm.SelectSlotCommand.Execute(vm.ComputeUnits.SelectMany(g => g.Slots).First(x => x.CcdId == "CCD10"));
        lastMap = null;
        await vm.BindSelectedCommand.ExecuteAsync(null);
        bool refuseSteal = lastMap is null && vm.BindActionStatus.Contains("已綁");

        // ② 正常綁定：S99 → CCD14（空槽）。送出的完整表須含「原本的 S10」+「新的 S99」
        vm.SelectSlotCommand.Execute(vm.ComputeUnits.SelectMany(g => g.Slots).First(x => x.CcdId == "CCD14"));
        lastMap = null;
        await vm.BindSelectedCommand.ExecuteAsync(null);
        var sent = lastMap?.AsArray();
        bool bindSent = sent is { Count: 2 }
            && sent.Any(e => e!["mac"]!.GetValue<string>() == "00:30:53:2A:0B:10"
                             && e["ccd_id"]!.GetValue<string>() == "CCD10")
            && sent.Any(e => e!["mac"]!.GetValue<string>() == "00:30:53:FF:FF:FF"
                             && e["ccd_id"]!.GetValue<string>() == "CCD14"
                             && e["cam_id"]!.GetValue<int>() == 4);   // CCD14 在 5 槽 fixture 的索引 = 4

        svc.Connection.Grab.Disconnect();
        l3.Stop();
        bool bindAct = refuseSteal && bindSent;

        Console.WriteLine($"  ① 載入 3 槽/2 運算單元 + 欄位(ccd_id/recipe_partition/expected_mac): {(load && keys ? "PASS" : "FAIL")}");
        Console.WriteLine($"  ② 依 compute_unit 分群 Spark1[2]/Spark2[1] + DeclaredSlotCount=3: {(group ? "PASS" : "FAIL")} (實得 {snapGroup})");
        Console.WriteLine($"  ③ 未列舉前無槽標「就位」(CCD01 有 expected_mac→離線, 其餘未綁): {(noOnline ? "PASS" : "FAIL")}");
        Console.WriteLine($"  ④ 舊版 grab 格式(無 ccd_id/bound)相容：{snapCams} 台皆未綁、不填任何槽: {(detectedSeparate ? "PASS" : "FAIL")}");
        Console.WriteLine($"  KPI案① 37槽+0列舉 → 配置=37 偵測KPI全0: {(kpiCase1 ? "PASS" : "FAIL")} (實得 配置={declaredAtBoot} 偵測 on/b/u/off=0)");
        Console.WriteLine($"  KPI案② 3宣告槽+舊格式1台 → 配置=3 連線=1 待綁=1 已綁=0 離線=1(CCD01該在沒在): {(kpiCase2 ? "PASS" : "FAIL")} " +
            $"(實得 {snapA})");
        Console.WriteLine($"  塊2-a 處理 N(真)=槽數 Spark1=2/Spark2=1: {(procN ? "PASS" : "FAIL")} (實得 {sp1.SlotCount}/{sp2.SlotCount})");
        Console.WriteLine($"  塊2-b 負載%(估算,公式非寫死)+標旗標: {(loadCalc ? "PASS" : "FAIL")} (Spark1 EstLoadPct={sp1.EstLoadPct} 期望={expect1}; LoadText=\"{sp1.LoadText}\")");
        Console.WriteLine($"  塊2-c 連線(真): 預設未連不假綠={connDefault} + 規則(active才綠/非active灰/未連灰)={connRule}: {(connDefault && connRule ? "PASS" : "FAIL")}");
        Console.WriteLine($"  #21-a join：CCD10 綁到 S10={bindJoin} · CCD11 MAC 正規化視為同台={bindMacNorm}: {(bindJoin && bindMacNorm ? "PASS" : "FAIL")}");
        Console.WriteLine($"  #21-b MAC 不符(插錯槽位) CCD12 標告警: {(bindMismatch && warnText ? "PASS" : "FAIL")} (警語=\"{snapWarn}\")");
        Console.WriteLine($"  #21-c 離線=該就位卻沒出現 CCD13={bindOffline} · null-mac 無相機 CCD14 不算離線={bindDeclared}: {(bindOffline && bindDeclared ? "PASS" : "FAIL")}");
        Console.WriteLine($"  #21-d 未綁相機(S99)不得指派任何槽: {(unboundNotAssigned ? "PASS" : "FAIL")}");
        Console.WriteLine($"  #21-e KPI 已綁3(槽)/待綁1(相機)/離線1(槽)/宣告5/連線4: {(kpiJoin ? "PASS" : "FAIL")} " +
            $"(實得 {snapKpiC})");
        bool block2 = procN && loadCalc && connDefault && connRule;
        Console.WriteLine($"  #21-f 綁定動作：拒絕搶已佔用的槽={refuseSteal} · 送出完整表(舊綁+新綁,cam_id=槽索引)={bindSent}: {(bindAct ? "PASS" : "FAIL")}");
        bool ok = load && keys && group && noOnline && detectedSeparate && kpiCase1 && kpiCase2 && block2 && join && bindAct;
        Console.WriteLine(ok ? "✓ 塊1+2 + Gap#21 綁定：拓樸/分群/舊格式相容不假merge + 處理N真/負載估算/連線真 + join四態/MAC正規化/插錯槽告警/未綁不指派"
                             : "✗ 不符");
        return ok ? 0 : 1;
    }

    // ---- 塊3-子塊1：單 CCD 設定整合頁（A 版薄殼）----
    // 驗：① VM 組合既有 Step1ViewModel + ZoneParamEditorViewModel 實例（非重做）
    //     ② 進頁前 HasSlot=false ③ LoadSlot 設對 RecipeStore.SelectedIp=recipe_partition(IP0 儲存鍵,約束①)
    //     ④ header 顯 ccd_id(CCD05)+運算單元。不動偵測 section（不在此測 SystemSettings）。
    private static int SingleCcdTest()
    {
        var svc = AppServices.Build();
        var vm = new ViewModels.SingleCcdSetupViewModel(svc);

        bool composed   = !ReferenceEquals(vm.Step1, null) && !ReferenceEquals(vm.ZoneEditor, null);  // 組合既有 VM 實例（ReferenceEquals 不污染 null 流分析）
        bool beforeSlot = !vm.HasSlot;

        var slot = new Models.CcdSlotModel { CcdId = "CCD05", ComputeUnit = "Spark1", RecipePartition = "IP5" };
        vm.LoadSlot(slot);
        bool ipSet     = svc.RecipeStore.SelectedIp == "IP5";                         // 儲存鍵走 recipe_partition
        bool headerCcd = vm.HeaderText.Contains("CCD05") && vm.HeaderText.Contains("Spark1") && vm.HasSlot;

        // ⑤ 3c：影像「編選中 ROI」的連動基礎 — RoiImageView.EditZone 綁 ZoneEditor.EditZone(=選中 ROI 的 Zone)；
        //    AllZones 綁 Store.Recipe.DetectRoiList(畫全部 ROI)。選不同 ROI → EditZone 跟著換(SelectRoi 機制)。
        var ze = vm.ZoneEditor!;
        bool roiLink = ze.Rois.Count >= 1
                       && ReferenceEquals(ze.EditZone, ze.Rois[0].Zone)
                       && ReferenceEquals(svc.RecipeStore.Recipe.DetectRoiList, ze.Store.Recipe.DetectRoiList);

        Console.WriteLine($"  ① 組合既有 Step1/ZoneEditor 實例(非重做): {(composed ? "PASS" : "FAIL")}");
        Console.WriteLine($"  ② 進頁前 HasSlot=false: {(beforeSlot ? "PASS" : "FAIL")}");
        Console.WriteLine($"  ③ LoadSlot → SelectedIp=IP5(儲存鍵,不改名): {(ipSet ? "PASS" : "FAIL")} (實得 {svc.RecipeStore.SelectedIp})");
        Console.WriteLine($"  ④ header 顯 ccd_id+運算單元(CCD05/Spark1): {(headerCcd ? "PASS" : "FAIL")} (\"{vm.HeaderText}\")");
        Console.WriteLine($"  ⑤ EditZone=選中 ROI + AllZones=DetectRoiList(影像編選中 ROI): {(roiLink ? "PASS" : "FAIL")} (Rois={ze.Rois.Count})");
        bool ok = composed && beforeSlot && ipSet && headerCcd && roiLink;
        Console.WriteLine(ok ? "✓ 子塊1：整合頁 VM 組合既有實例 + LoadSlot 設對儲存鍵(IP) + header 顯 CCD 名(約束①不改名)"
                             : "✗ 不符");
        return ok ? 0 : 1;
    }

    // ---- 上位機 CF_/8787 接線 + 回呼 + 交握（in-process，L2 護欄）----
    // 假上位機 client 連自起的 UpstreamServer（接線 UpstreamWiring）；OnLoadRecipe/OnGetResult 接到「假 IP server」；
    // align/grab 刻意不綁 → 驗：LoadRecipe 接通(OK)、GetResult 回 path+count、CHECK/SET_ALIGN 回誠實失敗(ERR 非假 OK)、連線燈轉綠。
    private static async Task<int> UpstreamTest()
    {
        // 假 IP server（loopback）：回 CHECK_HEALTH/LOAD_RECIPE OK、LIST_DEFECT_FOLDERS 兩夾
        var ipL = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        ipL.Start();
        int ipPort = ((System.Net.IPEndPoint)ipL.LocalEndpoint).Port;
        _ = Task.Run(async () =>
        {
            using var cli = await ipL.AcceptTcpClientAsync();
            using var ns = cli.GetStream();
            var rd = new System.IO.StreamReader(ns, System.Text.Encoding.UTF8);
            while (await rd.ReadLineAsync() is { } line && line.Length > 0)
            {
                var req = System.Text.Json.Nodes.JsonNode.Parse(line)!;
                var cmd = req["cmd"]!.GetValue<string>();
                var seq = (int?)req["seq"] ?? 0;
                string resp = cmd == "LIST_DEFECT_FOLDERS"
                    ? $"{{\"seq\":{seq},\"status\":\"OK\",\"folders\":[{{\"folder_name\":\"IP0_panelA_DEFAULT\",\"defect_count\":3}},{{\"folder_name\":\"IP1_panelB_DEFAULT\",\"defect_count\":5}}]}}"
                    : $"{{\"seq\":{seq},\"status\":\"OK\"}}";
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes(resp + "\n"));
                await ns.FlushAsync();
            }
        });

        var svc = AppServices.Build();
        await svc.Connection.Ip.ConnectAsync("127.0.0.1", ipPort);

        // 自起 UpstreamServer（取空閒 port，避免撞 8787）+ 真接線
        var fp = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        fp.Start(); int upPort = ((System.Net.IPEndPoint)fp.LocalEndpoint).Port; fp.Stop();
        var server = new Controllers.UpstreamServer(upPort);
        Controllers.UpstreamWiring.Bind(server, svc);
        server.Start();

        // 假上位機 client：送 CF_ 讀 9 參數回應
        System.Net.Sockets.TcpClient up = new();
        for (int i = 0; i < 20 && !up.Connected; i++)
        { try { await up.ConnectAsync("127.0.0.1", upPort); } catch { await Task.Delay(50); } }
        using var us = up.GetStream();
        var ur = new System.IO.StreamReader(us, System.Text.Encoding.UTF8);
        var uw = new System.IO.StreamWriter(us, new System.Text.UTF8Encoding(false)) { AutoFlush = true, NewLine = "\r\n" };
        async Task<string> Cmd(string l) { await uw.WriteLineAsync(l); return await ur.ReadLineAsync() ?? ""; }

        var rReady  = await Cmd("CF_READY");
        var rLoad   = await Cmd("CF_LOAD_RECIPE|DEFAULT|panelA|2026-06-19-00-00-00|||||||0");
        var rGet    = await Cmd("CF_GET_RESULT");
        var rCheck  = await Cmd("CF_CHECK_ALIGN");
        var rSet    = await Cmd("CF_SET_ALIGN|Cs_AlignSet|1|2");
        var rStop   = await Cmd("CF_STOP");
        await Task.Delay(50);
        bool lampGreen = svc.Connection.IsUpstreamConnected;   // 連上時 OnConnectedChanged→燈轉綠

        up.Dispose(); server.Dispose(); svc.Connection.Ip.Disconnect(); ipL.Stop();

        bool ready = rReady.StartsWith("OK");
        bool load  = rLoad.StartsWith("OK");                                          // OnLoadRecipe→假 IP OK
        bool get   = rGet.StartsWith("OK") && rGet.Contains("IP0_panelA_DEFAULT") && rGet.Contains("3,5"); // path+count 非 JSON
        bool checkFail = rCheck.StartsWith("ERR");                                    // ★A 誠實失敗(非假 OK)
        bool setFail   = rSet.StartsWith("ERR");                                      // ★A 誠實失敗(非假 OK)
        bool stopFail  = rStop.StartsWith("ERR");                                     // #25 ★A 誠實失敗(offline 無取像可停)

        Console.WriteLine($"  CF_READY → OK: {(ready ? "PASS" : "FAIL")} (\"{rReady}\")");
        Console.WriteLine($"  CF_LOAD_RECIPE → OK(接 IP): {(load ? "PASS" : "FAIL")} (\"{rLoad}\")");
        Console.WriteLine($"  CF_GET_RESULT → OK|path|count 非JSON: {(get ? "PASS" : "FAIL")} (\"{rGet}\")");
        Console.WriteLine($"  CF_CHECK_ALIGN → ERR 誠實失敗(非假OK): {(checkFail ? "PASS" : "FAIL")} (\"{rCheck}\")");
        Console.WriteLine($"  CF_SET_ALIGN → ERR 誠實失敗(非假OK): {(setFail ? "PASS" : "FAIL")} (\"{rSet}\")");
        Console.WriteLine($"  CF_STOP → ERR 誠實失敗(offline 無取像可停): {(stopFail ? "PASS" : "FAIL")} (\"{rStop}\")");
        Console.WriteLine($"  上位機連線燈轉綠(OnConnectedChanged): {(lampGreen ? "PASS" : "FAIL")}");
        bool ok = ready && load && get && checkFail && setFail && stopFail && lampGreen;
        Console.WriteLine(ok ? "✓ 上位機 CF_：接線啟動+回呼接 IP+9參數交握；align/grab 誠實失敗(非假OK)；燈轉綠 (L2 in-process)"
                             : "✗ 不符");
        return ok ? 0 : 1;
    }

    // ---- 觸發鏈（37 CCD 軟體觸發設計）：CF_LOAD_RECIPE→Grab ARM 預熱、CF_GRAB_START→觸發、CF_STOP→teardown ----
    // 假 IP + 假 Grab server（loopback）驗 UpstreamWiring 真接線：命令轉送、參數傳遞（timeout_ms/
    // frames_per_panel）、Grab 離線時 CF_GRAB_START 誠實 ERR（決策 A 精神）。L2 in-process。
    private static async Task<int> GrabTriggerTest()
    {
        // 假 IP server：一律回 OK（LOAD_RECIPE 用）
        var ipL = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        ipL.Start();
        int ipPort = ((System.Net.IPEndPoint)ipL.LocalEndpoint).Port;
        _ = Task.Run(async () =>
        {
            using var cli = await ipL.AcceptTcpClientAsync();
            using var ns = cli.GetStream();
            var rd = new System.IO.StreamReader(ns, System.Text.Encoding.UTF8);
            while (await rd.ReadLineAsync() is { } line && line.Length > 0)
            {
                var req = System.Text.Json.Nodes.JsonNode.Parse(line)!;
                var seq = (int?)req["seq"] ?? 0;
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes($"{{\"seq\":{seq},\"status\":\"OK\"}}\n"));
                await ns.FlushAsync();
            }
        });

        // 假 Grab server：一律回 OK，並記錄收到的 (cmd, params)
        var received = new System.Collections.Concurrent.ConcurrentQueue<System.Text.Json.Nodes.JsonNode>();
        var grabL = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        grabL.Start();
        int grabPort = ((System.Net.IPEndPoint)grabL.LocalEndpoint).Port;
        _ = Task.Run(async () =>
        {
            using var cli = await grabL.AcceptTcpClientAsync();
            using var ns = cli.GetStream();
            var rd = new System.IO.StreamReader(ns, System.Text.Encoding.UTF8);
            while (await rd.ReadLineAsync() is { } line && line.Length > 0)
            {
                var req = System.Text.Json.Nodes.JsonNode.Parse(line)!;
                received.Enqueue(req);
                var seq = (int?)req["seq"] ?? 0;
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes($"{{\"seq\":{seq},\"status\":\"OK\"}}\n"));
                await ns.FlushAsync();
            }
        });

        var svc = AppServices.Build();
        svc.Config.Grab.FramesPerPanel = 27;   // 驗 frames_per_panel 由 config 傳遞
        await svc.Connection.Ip.ConnectAsync("127.0.0.1", ipPort);
        await svc.Connection.Grab.ConnectAsync("127.0.0.1", grabPort);

        var fp = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        fp.Start(); int upPort = ((System.Net.IPEndPoint)fp.LocalEndpoint).Port; fp.Stop();
        var server = new Controllers.UpstreamServer(upPort);
        Controllers.UpstreamWiring.Bind(server, svc);
        server.Start();

        System.Net.Sockets.TcpClient up = new();
        for (int i = 0; i < 20 && !up.Connected; i++)
        { try { await up.ConnectAsync("127.0.0.1", upPort); } catch { await Task.Delay(50); } }
        using var us = up.GetStream();
        var ur = new System.IO.StreamReader(us, System.Text.Encoding.UTF8);
        var uw = new System.IO.StreamWriter(us, new System.Text.UTF8Encoding(false)) { AutoFlush = true, NewLine = "\r\n" };
        async Task<string> Cmd(string l) { await uw.WriteLineAsync(l); return await ur.ReadLineAsync() ?? ""; }

        var rLoad  = await Cmd("CF_LOAD_RECIPE|DEFAULT|panelX|2026-07-21-00-00-00|||||||0");
        var rStart = await Cmd("CF_GRAB_START|12345");
        var rStop  = await Cmd("CF_STOP");
        await Task.Delay(50);
        up.Dispose(); server.Dispose();
        svc.Connection.Ip.Disconnect(); svc.Connection.Grab.Disconnect();
        ipL.Stop(); grabL.Stop();

        // 收到的 Grab 命令序列與參數
        var cmds = received.ToArray();
        string CmdAt(int i) => i < cmds.Length ? cmds[i]?["cmd"]?.GetValue<string>() ?? "" : "";
        var startNode = System.Linq.Enumerable.FirstOrDefault(cmds, c => c?["cmd"]?.GetValue<string>() == "GRAB_START");
        int? tMs = startNode?["params"]?["timeout_ms"]?.GetValue<int>();
        int? fpp = startNode?["params"]?["frames_per_panel"]?.GetValue<int>();
        var loadNode = System.Linq.Enumerable.FirstOrDefault(cmds, c => c?["cmd"]?.GetValue<string>() == "LOAD_RECIPE");
        string panel = loadNode?["params"]?["panel_id"]?.GetValue<string>() ?? "";

        bool seqOk   = CmdAt(0) == "LOAD_RECIPE" && CmdAt(1) == "GRAB_ARM" && CmdAt(2) == "GRAB_START" && CmdAt(3) == "GRAB_STOP";
        bool loadOk  = rLoad.StartsWith("OK") && panel == "panelX";
        bool startOk = rStart.StartsWith("OK") && tMs == 12345 && fpp == 27;
        bool stopOk  = rStop.StartsWith("OK");

        Console.WriteLine($"  Grab 收到命令序列 LOAD_RECIPE→GRAB_ARM→GRAB_START→GRAB_STOP: {(seqOk ? "PASS" : "FAIL")} ({string.Join(",", System.Linq.Enumerable.Select(cmds, c => c?["cmd"]?.GetValue<string>()))})");
        Console.WriteLine($"  CF_LOAD_RECIPE → OK + Grab panel_id=panelX: {(loadOk ? "PASS" : "FAIL")} (\"{rLoad}\")");
        Console.WriteLine($"  CF_GRAB_START|12345 → OK + timeout_ms=12345 + frames_per_panel=27: {(startOk ? "PASS" : "FAIL")} (\"{rStart}\", t={tMs}, fpp={fpp})");
        Console.WriteLine($"  CF_STOP → OK（GRAB_STOP teardown）: {(stopOk ? "PASS" : "FAIL")} (\"{rStop}\")");

        // 負案例：Grab 離線 → CF_GRAB_START 誠實 ERR（不假 OK）
        var svc2 = AppServices.Build();
        var fp2 = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        fp2.Start(); int upPort2 = ((System.Net.IPEndPoint)fp2.LocalEndpoint).Port; fp2.Stop();
        var server2 = new Controllers.UpstreamServer(upPort2);
        Controllers.UpstreamWiring.Bind(server2, svc2);
        server2.Start();
        System.Net.Sockets.TcpClient up2 = new();
        for (int i = 0; i < 20 && !up2.Connected; i++)
        { try { await up2.ConnectAsync("127.0.0.1", upPort2); } catch { await Task.Delay(50); } }
        using var us2 = up2.GetStream();
        var ur2 = new System.IO.StreamReader(us2, System.Text.Encoding.UTF8);
        var uw2 = new System.IO.StreamWriter(us2, new System.Text.UTF8Encoding(false)) { AutoFlush = true, NewLine = "\r\n" };
        await uw2.WriteLineAsync("CF_GRAB_START|40000");
        var rOffline = await ur2.ReadLineAsync() ?? "";
        up2.Dispose(); server2.Dispose();
        bool offlineOk = rOffline.StartsWith("ERR");
        Console.WriteLine($"  Grab 離線 → CF_GRAB_START 誠實 ERR: {(offlineOk ? "PASS" : "FAIL")} (\"{rOffline}\")");

        bool ok = seqOk && loadOk && startOk && stopOk && offlineOk;
        Console.WriteLine(ok ? "✓ 觸發鏈：CF_LOAD_RECIPE→ARM 預熱 + CF_GRAB_START→觸發(參數正確) + CF_STOP→teardown + 離線誠實 ERR (L2 in-process)"
                             : "✗ 不符");
        return ok ? 0 : 1;
    }

    // ---- #33 配方管理（Delete/SaveAll/開資料夾）+ #7 跨配方批次複製（暫存 RecipeDir 隔離，不碰真實配方）----
    private static int RecipeMgmtTest()
    {
        bool ok = true;
        var root = Path.Combine(Path.GetTempPath(), "cfaoi_recipemgmt");
        if (Directory.Exists(root)) Directory.Delete(root, true);
        Directory.CreateDirectory(root);
        var cfg = new SystemConfigModel();
        cfg.Paths.RecipeDir = root;
        var svc = new RecipeService(cfg, new LogService());
        var store = new RecipeStore(svc, new LogService(), new[] { "IP0", "IP1" });

        // 來源配方 SRC：PitchX=77 存到 IP0
        store.Select("SRC");
        store.PrimaryZone!.PitchX = 77;
        store.Save();

        // (#7) 批次複製 SRC → DST1/DST2（IP0）
        int copied = store.CopyParamsToMany(new[] { "DST1", "DST2" });
        var d1 = svc.Load(svc.RecipeXmlPath("DST1", "IP0")).DetectRoiList[0].PitchX;
        var d2 = svc.Load(svc.RecipeXmlPath("DST2", "IP0")).DetectRoiList[0].PitchX;
        bool copyOk = copied == 2 && d1 == 77 && d2 == 77;
        Console.WriteLine($"  #7 批次複製 SRC→DST1/DST2: copied={copied} DST1.PitchX={d1} DST2.PitchX={d2} → {(copyOk ? "PASS" : "FAIL")}");
        ok &= copyOk;

        // (#33 SaveAll) 目前 SRC（PitchX=88）存到所有 IP（IP0+IP1）
        store.Select("SRC");
        store.PrimaryZone!.PitchX = 88;
        store.SaveToAllIps();
        var ip1Pitch = svc.Load(svc.RecipeXmlPath("SRC", "IP1")).DetectRoiList[0].PitchX;
        bool allOk = File.Exists(svc.RecipeXmlPath("SRC", "IP0")) && File.Exists(svc.RecipeXmlPath("SRC", "IP1")) && ip1Pitch == 88;
        Console.WriteLine($"  #33 SaveAll→所有 IP: IP0存在={File.Exists(svc.RecipeXmlPath("SRC", "IP0"))} IP1.PitchX={ip1Pitch} → {(allOk ? "PASS" : "FAIL")}");
        ok &= allOk;

        // (#33 開資料夾) 路徑正確
        bool folderOk = store.RecipeFolder("SRC") == Path.Combine(root, "SRC") && Directory.Exists(store.RecipeFolder("SRC"));
        Console.WriteLine($"  #33 配方資料夾: {store.RecipeFolder("SRC")} 存在={Directory.Exists(store.RecipeFolder("SRC"))} → {(folderOk ? "PASS" : "FAIL")}");
        ok &= folderOk;

        // (#33 Delete) 刪 DST1 → 資料夾消失 + 清單移除
        store.DeleteRecipe("DST1");
        bool delOk = !Directory.Exists(Path.Combine(root, "DST1")) && !store.RecipeNames.Contains("DST1");
        Console.WriteLine($"  #33 Delete DST1: 資料夾消失={!Directory.Exists(Path.Combine(root, "DST1"))} 清單移除={!store.RecipeNames.Contains("DST1")} → {(delOk ? "PASS" : "FAIL")}");
        ok &= delOk;

        // 刪目前配方 → 退回 DEFAULT
        store.Select("DST2");
        store.DeleteRecipe("DST2");
        bool fallbackOk = store.SelectedRecipe == "DEFAULT";
        Console.WriteLine($"  #33 刪目前配方→退回 DEFAULT: SelectedRecipe={store.SelectedRecipe} → {(fallbackOk ? "PASS" : "FAIL")}");
        ok &= fallbackOk;

        Console.WriteLine(ok ? "✓ #33 配方管理(Delete/SaveAll/資料夾) + #7 批次複製 正確" : "✗ 不符");
        return ok ? 0 : 1;
    }

    // #16 Rule 改判 / #32 邊界略過 的「Control→IP recipe_saving 送出端」。
    //   (1) unit：RecipeSavingModel.BuildRecipeSavingJson 鍵名/值/型別/鍵數逐一對齊 IP control_server.cpp 解析端。
    //   (2) e2e（選填 <host> [port]）：用 *真* IpClient.LoadRecipeAsync(recipeSaving:..) 連真 IP，
    //       bypass_edge_x=100 使近左邊界缺陷被丟 → 缺陷數下降（證送出端到 IP 套用端整條打通）。
    private static async Task<int> RecipeSavingTest(string[] rest)
    {
        bool ok = true;

        // ---- (1) unit：JSON 契約 ----
        var m = new RecipeSavingModel
        {
            MaxSaveDefectCount = 123, SaveDefectWidth = 80, SaveDefectHeight = 90, MaxDefectCountPass = 555,
            BypassEdgeX = 100, BypassEdgeY = 60,
            ImageRuleEnable = true, MeanLowThreshold = 40.5, HdivWThreshold = 4.25, NgSizeThreshold = 4096.0,
        };
        var j = m.BuildRecipeSavingJson();
        var expect = new (string key, string val)[]
        {
            ("max_save_defect_count", "123"), ("save_defect_width", "80"), ("save_defect_height", "90"),
            ("max_defect_count_pass", "555"), ("bypass_edge_x", "100"), ("bypass_edge_y", "60"),
            ("image_rule_enable", "true"), ("mean_low_threshold", "40.5"), ("hdivw_threshold", "4.25"),
            ("ng_size_threshold", "4096"),
        };
        foreach (var (key, val) in expect)
        {
            bool match = j.ContainsKey(key) && j[key]!.ToJsonString() == val;
            ok &= match;
            Console.WriteLine($"  recipe_saving[{key}]={(j.ContainsKey(key) ? j[key]!.ToJsonString() : "<缺>")}" +
                              $"（期望 {val}）→ {(match ? "PASS" : "FAIL")}");
        }
        bool count10 = j.Count == 10;
        Console.WriteLine($"  recipe_saving 鍵數={j.Count}（期望 10，無漏無多）→ {(count10 ? "PASS" : "FAIL")}");
        ok &= count10;

        // ---- (2) e2e（選填）：連真 IP 驗 bypass 端到端 ----
        if (rest.Length >= 1)
        {
            string host = rest[0];
            int port = rest.Length >= 2 && int.TryParse(rest[1], out var p) ? p : 8200;
            Console.WriteLine($"\n  ── e2e：連真 IP {host}:{port}（bypass_edge_x=100 → 近左邊界缺陷被丟）──");
            const int W = 8192, H = 5000, PX = 26, PY = 19;
            var img = MakeEdgeDefectImage(W, H, PX, PY);
            string xml = EdgeRecipeXml(PX, PY);
            using var ip = new IpClient();
            await ip.ConnectAsync(host, port);
            int nNo = await CountWithSaving(ip, img, W, H, xml, "rs_nobypass",  new RecipeSavingModel());
            int nBy = await CountWithSaving(ip, img, W, H, xml, "rs_bypass100", new RecipeSavingModel { BypassEdgeX = 100 });
            bool e2eOk = nNo > 0 && nBy < nNo;
            Console.WriteLine($"  e2e: N(bypass=0)={nNo}  N(bypass_edge_x=100)={nBy}  → 丟 {nNo - nBy} 顆近邊界 → {(e2eOk ? "PASS" : "FAIL")}");
            ok &= e2eOk;
        }
        else
        {
            Console.WriteLine("  （未給 <host> → 略過 e2e；僅跑 JSON 契約 unit 測）");
        }

        Console.WriteLine(ok ? "✓ recipe_saving 送出端（#16/#32）JSON 契約 + e2e 正確" : "✗ 不符");
        return ok ? 0 : 1;
    }

    private static async Task<int> CountWithSaving(IpClient ip, byte[] img, int w, int h,
                                                   string xml, string panel, RecipeSavingModel saving)
    {
        var lr = await ip.LoadRecipeAsync("RS", panel, xml, recipeSaving: saving.BuildRecipeSavingJson());
        if (lr?["status"]?.GetValue<string>() != "OK")
            throw new InvalidOperationException($"LOAD_RECIPE 失敗：{lr?.ToJsonString()}");
        var resp = await ip.SendImageForReviewAsync(panel, 0, w, h, 0, img, last: true);
        if (resp?["status"]?.GetValue<string>() != "OK")
            throw new InvalidOperationException($"SEND_IMAGE 失敗：{resp?.ToJsonString()}");
        int n = 0;
        var rois = resp?["result"]?["RoiInfoList"]?.AsArray();
        if (rois != null)
            foreach (var r in rois) n += r?["DefectInfoList"]?.AsArray()?.Count ?? 0;
        return n;
    }

    // 與 scripts/verify_rules_edge.py 同一張合成圖：grid(140/128) + 3 中間 + 2 近左邊界(x=70,85) 缺陷。
    private static byte[] MakeEdgeDefectImage(int w, int h, int px, int py)
    {
        var img = new byte[(long)w * h];
        Array.Fill(img, (byte)128);
        for (int r = 0; r < h; r += py)
            for (int rr = r; rr < Math.Min(r + 2, h); rr++)
                for (int c = 0; c < w; c++) img[(long)rr * w + c] = 140;
        for (int c = 0; c < w; c += px)
            for (int cc = c; cc < Math.Min(c + 2, w); cc++)
                for (int r = 0; r < h; r++) img[(long)r * w + cc] = 140;
        (int x, int y, int v, int dw, int dh)[] defs =
            { (2000, 2500, 220, 5, 5), (3000, 3000, 215, 4, 4), (4000, 2000, 40, 5, 5),
              (70, 2500, 220, 5, 5), (85, 3000, 220, 4, 4) };
        foreach (var (x, y, v, dw, dh) in defs)
            for (int yy = y; yy < y + dh; yy++)
                for (int xx = x; xx < x + dw; xx++) img[(long)yy * w + xx] = (byte)v;
        return img;
    }

    private static string EdgeRecipeXml(int px, int py) =>
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n" +
        "<Recipe><M_AlignRoi><AlignEnable>false</AlignEnable></M_AlignRoi>\n" +
        "<DetectRoiList><DetectRoi>\n" +
        "<StartX>-1</StartX><StartY>-1</StartY><EndX>-1</EndX><EndY>-1</EndY>\n" +
        "<AlgorithmWay>8-Way-Star</AlgorithmWay><AlgorithmCompare>DIV</AlgorithmCompare>\n" +
        "<BrightThreshold>1.4</BrightThreshold><DarkThreshold>0.6</DarkThreshold>\n" +
        $"<PitchX>{px}</PitchX><PitchY>{py}</PitchY><SearchX>1</SearchX><SearchY>1</SearchY>\n" +
        "<BlobMinSize>2</BlobMinSize><BlobMaxSize>500</BlobMaxSize>\n" +
        "</DetectRoi></DetectRoiList><DetectIoiList/></Recipe>";

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
        // current_class 含中文「未分類」→ 驗證 TCP 讀取 UTF-8 解碼正確（非逐 byte Latin-1 亂碼）
        bool utf8Ok = vm.Patches.Count == 2 && vm.Patches[0].CurrentClass == "未分類";
        bool listed = vm.InPatchView && vm.TotalCount == 3 && vm.ClassifiedCount == 1
                      && vm.Patches.Count == 2
                      && vm.Patches[0].GcX == 10 && vm.Patches[0].Type == "Bright"
                      && vm.Patches[1].Size == 7;
        Console.WriteLine($"  OpenFolder（預設只顯示未分類）→ 全集 {vm.TotalCount}、可見 {vm.Patches.Count}、" +
            $"已分類 {vm.ClassifiedCount}, metadata 正確={listed}, 中文UTF-8正常={utf8Ok}（CurrentClass=\"{vm.Patches[0].CurrentClass}\"）");

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

        bool ok = listed && utf8Ok && hiddenAfterClassify && persistedImmediately && showAll && onlyTrue
                  && vm.ClassifiedCount == 3 && vm.TrueDefectCount == 2 && vm.ParticleCount == 1;
        Console.WriteLine(ok ? "✓ filter + 即時持久化 + 統計 + 切換檢視 + 中文 UTF-8 正確" : "✗ 不符");
        return ok ? 0 : 1;
    }

    // ---- 連線心跳偵測（綠↔紅 + 自動重連）----
    // ── 相機工作台（操作員五步驟動線）：假 Grab server 驗 綁定→取像→對位→(調參)→套用 ──
    private static async Task<int> WorkbenchTest()
    {
        int pass = 0, fail = 0;
        void Check(bool ok, string name)
        {
            if (ok) { pass++; Console.WriteLine($"  ✓ {name}"); }
            else    { fail++; Console.WriteLine($"  ✗ {name}"); }
        }

        var svc = AppServices.Build();
        var engine = new ViewModels.SystemSettingsViewModel(svc);
        var single = new ViewModels.SingleCcdSetupViewModel(svc);
        var wb = new ViewModels.CameraWorkbenchViewModel(svc, engine, single);

        // 拓樸：3 槽（CCD90 綁 MAC AA / CCD91、92 未宣告 MAC）；分區用 IPT9x 不汙染真配方
        engine.ApplyTopology(Models.ArrayTopologyModel.Parse("""
            { "ccd_total_count": 3,
              "compute_units": [ { "id":"SparkT","node":"IpOffline","role":"aoi" } ],
              "slots": [
                {"ccd_id":"CCD90","compute_unit":"SparkT","expected_mac":"00:30:53:00:00:AA","recipe_partition":"IPT90"},
                {"ccd_id":"CCD91","compute_unit":"SparkT","expected_mac":null,"recipe_partition":"IPT91"},
                {"ccd_id":"CCD92","compute_unit":"SparkT","expected_mac":null,"recipe_partition":"IPT92"} ] }
            """));

        // ── 假 Grab server：可變 cams 表 + 記錄 SET_CAM_MAP / SET_CAM_PARAMS ──
        var camsLock = new object();
        var cams = new System.Collections.Generic.List<(string Mac, string Model, string Serial, string Ccd, bool Bound, int CamId)>
        {
            ("00:30:53:00:00:AA", "raL8192-12gm", "SA", "CCD90", true, 0),
            ("00:30:53:00:00:BB", "raL8192-12gm", "SB", "",      false, 9),
        };
        string CamsJson()
        {
            lock (camsLock)
                return "[" + string.Join(",", cams.Select(c =>
                    $"{{\"cam_id\":{c.CamId},\"mac\":\"{c.Mac}\",\"model\":\"{c.Model}\",\"serial\":\"{c.Serial}\"," +
                    $"\"ip\":\"192.168.5.{c.CamId + 1}\",\"online\":true,\"persistent\":true,\"ip_config\":\"Persistent\"," +
                    $"\"device_class\":\"BaslerGigE\",\"ccd_id\":\"{c.Ccd}\",\"bound\":{(c.Bound ? "true" : "false")}}}")) + "]";
        }
        System.Text.Json.Nodes.JsonNode? lastMapReq = null;
        var setParamCalls = new System.Collections.Generic.List<(int CamId, double Exp, int Gain)>();

        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        int port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        _ = Task.Run(async () =>
        {
            using var cli = await listener.AcceptTcpClientAsync();
            using var ns = cli.GetStream();
            var rd = new System.IO.StreamReader(ns, System.Text.Encoding.UTF8);
            while (await rd.ReadLineAsync() is { } line && line.Length > 0)
            {
                var req = System.Text.Json.Nodes.JsonNode.Parse(line)!;
                var seq = (int?)req["seq"] ?? 0;
                string cmd = req["cmd"]!.GetValue<string>();
                string resp;
                switch (cmd)
                {
                    case "LIST_CAMERAS":
                        resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"cameras\":{CamsJson()}}}";
                        break;
                    case "SET_CAM_MAP":
                        lastMapReq = req;
                        lock (camsLock)
                        {
                            var entries = req["params"]!["entries"]!.AsArray();
                            for (int i = 0; i < cams.Count; i++)
                            {
                                var hit = entries.FirstOrDefault(e =>
                                    e!["mac"]!.GetValue<string>() == cams[i].Mac);
                                cams[i] = hit is null
                                    ? (cams[i].Mac, cams[i].Model, cams[i].Serial, "", false, cams[i].CamId)
                                    : (cams[i].Mac, cams[i].Model, cams[i].Serial,
                                       hit["ccd_id"]!.GetValue<string>(), true, hit["cam_id"]!.GetValue<int>());
                            }
                        }
                        resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"written\":{req["params"]!["entries"]!.AsArray().Count}}}";
                        break;
                    case "TUNE_MEAN":
                    {
                        double exp = req["params"]!["exposure_us"]!.GetValue<double>();
                        double mean = exp >= 1000 ? 90.4 : 2.5;   // 高曝光=打光工作點 / 低曝光=暗場
                        resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"mean_gray\":{mean}," +
                               $"\"exposure_us_actual\":{exp},\"gain_raw_actual\":{req["params"]!["gain_raw"]!.GetValue<int>()}}}";
                        break;
                    }
                    case "SET_CAM_PARAMS":
                    {
                        var p = req["params"]!;
                        setParamCalls.Add((p["cam_id"]!.GetValue<int>(),
                                           p["exposure_us"]!.GetValue<double>(), p["gain_raw"]!.GetValue<int>()));
                        resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"exposure_us_actual\":{p["exposure_us"]!.GetValue<double>()}," +
                               $"\"gain_raw_actual\":{p["gain_raw"]!.GetValue<int>()}}}";
                        break;
                    }
                    default:
                        resp = $"{{\"seq\":{seq},\"status\":\"OK\"}}";
                        break;
                }
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes(resp + "\n"));
                await ns.FlushAsync();
            }
        });
        await svc.Connection.Grab.ConnectAsync("127.0.0.1", port);

        // 配方環境：WB_TEST；預先給 IPT90 一個「自己的 Mark」驗 Step5 不覆蓋
        svc.RecipeStore.Select("WB_TEST");
        var pre = new Models.RecipeModel();
        pre.AlignRoi.PatternPath = "keep_ccd90_mark.tif";
        svc.Recipes.Save("WB_TEST", pre, "IPT90");

        // ── ① 列舉 + 左欄 join ──
        await engine.LoadCamerasCommand.ExecuteAsync(null);
        Check(wb.Rail.Count == 3, $"左欄 3 槽（實得 {wb.Rail.Count}）");
        var r90 = wb.Rail.First(r => r.CcdId == "CCD90");
        var r91 = wb.Rail.First(r => r.CcdId == "CCD91");
        Check(r90.Kind == ViewModels.SlotBindKind.Bound && r90.Done1, "CCD90 已綁定 → 進度點①亮");
        Check(r91.Kind == ViewModels.SlotBindKind.Declared && !r91.Done1, "CCD91 未綁定");
        Check(wb.Unassigned.Count == 1, "未指派清單 1 台（MAC BB）");

        // ── Step 1：把 BB 綁到 CCD91（候選→綁定，完整表語意）──
        wb.SelectedSlot = r91;
        Check(wb.BindModeIsBind, "未綁定槽 → 綁定面板模式 bind");
        wb.SelectedCandidate = engine.UnboundCameras.First();
        await wb.BindCandidateCommand.ExecuteAsync(null);
        var entriesSent = lastMapReq?["params"]?["entries"]?.AsArray();
        bool fullTable = entriesSent is { Count: 2 }
            && entriesSent.Any(e => e!["mac"]!.GetValue<string>().EndsWith("AA") && e["ccd_id"]!.GetValue<string>() == "CCD90")
            && entriesSent.Any(e => e!["mac"]!.GetValue<string>().EndsWith("BB") && e["ccd_id"]!.GetValue<string>() == "CCD91"
                                    && e["cam_id"]!.GetValue<int>() == 1);
        Check(fullTable, "SET_CAM_MAP 送完整表（AA 保留 + BB→CCD91 cam_id=槽索引 1）");
        var r91b = wb.Rail.First(r => r.CcdId == "CCD91");
        Check(r91b.Kind == ViewModels.SlotBindKind.Bound && wb.BindModeIsBound, "綁定後 CCD91 轉綠 + 面板轉 bound 模式");
        Check(wb.Unassigned.Count == 0, "未指派清單清空");

        // ── Step 2：取像驗證（暗場判讀 → 調高曝光 → 正常）──
        wb.SelectedSlot = wb.Rail.First(r => r.CcdId == "CCD91");
        wb.ExposureUs = 70; wb.GainRaw = 256;
        await wb.GrabOneCommand.ExecuteAsync(null);
        Check(wb.MeanText == "2.5" && wb.MeanJudgment.Contains("暗場"), $"暗場判讀（mean {wb.MeanText}）");
        wb.ExposureUs = 2000; wb.GainRaw = 2047;
        await wb.ApplyAndGrabCommand.ExecuteAsync(null);
        Check(wb.MeanText == "90.4" && wb.MeanJudgment.Contains("正常"), $"工作點判讀（mean {wb.MeanText}）");
        Check(wb.Rail.First(r => r.CcdId == "CCD91").Done2, "進度點②亮");
        Check(setParamCalls.Any(c => c.CamId == 1 && Math.Abs(c.Exp - 2000) < 0.1), "套用寫入 cam1 曝光 2000");

        // ── Step 3：對位 Mark 存入該槽分區 ──
        wb.AlignEnable = true; wb.AlignReferX = 197; wb.AlignReferY = 168;
        wb.AlignSearchW = 640; wb.AlignSearchH = 480; wb.AlignPatternPath = "mark_ccd91.tif";
        wb.SaveAlignCommand.Execute(null);
        var p91 = System.IO.File.ReadAllText(svc.Recipes.RecipeXmlPath("WB_TEST", "IPT91"));
        Check(p91.Contains("<ReferX>197</ReferX>") && p91.Contains("<PatternPath>mark_ccd91.tif</PatternPath>")
              && p91.Contains("<AlignEnable>true</AlignEnable>"), "Mark 寫入 WB_TEST/IPT91");
        Check(wb.Rail.First(r => r.CcdId == "CCD91").Done3, "進度點③亮");

        // ── Step 5：套用到其他相機（保留目標 Mark；曝光/增益跟送）──
        svc.RecipeStore.PrimaryZone!.PitchX = 77;
        svc.RecipeStore.Save();
        wb.GoStepCommand.Execute("5");
        Check(wb.CopyTargets.Count == 2, $"目標清單 2 槽（實得 {wb.CopyTargets.Count}）");
        Check(wb.CopyTargets.First(t => t.CcdId == "CCD90").Selected
              && !wb.CopyTargets.First(t => t.CcdId == "CCD92").CanSelect, "已綁定預選、未綁定不可選");
        setParamCalls.Clear();
        await wb.ApplyToTargetsCommand.ExecuteAsync(null);
        var p90 = System.IO.File.ReadAllText(svc.Recipes.RecipeXmlPath("WB_TEST", "IPT90"));
        Check(p90.Contains("<PitchX>77</PitchX>"), "檢測參數複製到 IPT90（PitchX 77）");
        Check(p90.Contains("keep_ccd90_mark.tif"), "IPT90 自己的 Mark 未被覆蓋（各教各的）");
        Check(setParamCalls.Any(c => c.CamId == 0), "CCD90 的相機收到曝光/增益");
        Check(wb.CopyStatus.StartsWith("✓"), $"套用回饋（{wb.CopyStatus}）");
        Check(wb.Rail.First(r => r.CcdId == "CCD91").Done5, "進度點⑤亮");

        svc.Connection.Grab.Disconnect();
        listener.Stop();
        Console.WriteLine($"結果: {pass} pass, {fail} fail");
        return fail == 0 ? 0 : 1;
    }

    // ★8 自動化驗證（--selftest heartbeat auto）：假 IP server 可控「回應/停擺」，
    // 驗「單次逾時不翻線、連續 2 次才斷、恢復後自動重連」門檻邏輯。
    // 時間常數經 ConfigureForTest 縮到毫秒級（400ms 逾時 × 150ms 間隔），全程約 6 秒。
    private static async Task<int> HeartbeatAutoTest()
    {
        int pass = 0, fail = 0;
        void Check(bool ok, string name)
        {
            if (ok) { pass++; Console.WriteLine($"  ✓ {name}"); }
            else    { fail++; Console.WriteLine($"  ✗ {name}"); }
        }
        async Task<bool> WaitUntil(Func<bool> cond, int timeoutMs)
        {
            var t0 = Environment.TickCount64;
            while (Environment.TickCount64 - t0 < timeoutMs)
            {
                if (cond()) return true;
                await Task.Delay(30);
            }
            return cond();
        }

        // ---- 假 IP server：stall==0 正常回 OK；>0 吃掉 N 次不回；-1 全部停擺 ----
        int stall = 0;
        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        int port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        var serverCts = new System.Threading.CancellationTokenSource();
        _ = Task.Run(async () =>
        {
            while (!serverCts.IsCancellationRequested)
            {
                System.Net.Sockets.TcpClient? c;
                try { c = await listener.AcceptTcpClientAsync(serverCts.Token); } catch { break; }
                _ = Task.Run(async () =>
                {
                    try
                    {
                        using var cl = c;
                        var st = cl.GetStream();
                        var rd = new System.IO.StreamReader(st);
                        var wr = new System.IO.StreamWriter(st) { AutoFlush = true };
                        while (true)
                        {
                            var line = await rd.ReadLineAsync();
                            if (line is null) break;
                            var mode = System.Threading.Volatile.Read(ref stall);
                            if (mode == -1) continue;                                   // 停擺：收下不回
                            if (mode > 0) { System.Threading.Interlocked.Decrement(ref stall); continue; }
                            await wr.WriteLineAsync("{\"status\":\"OK\"}");
                        }
                    }
                    catch { }
                });
            }
        });

        var cfg = new Models.SystemConfigModel();
        cfg.Nodes["IpOffline"] = new Models.NodeConfig { Host = "127.0.0.1", Port = port };
        cfg.ActiveIpNode = "IpOffline";
        var log = new LogService();
        var errors = new System.Collections.Concurrent.ConcurrentQueue<string>();
        var infos  = new System.Collections.Concurrent.ConcurrentQueue<string>();
        log.Logged += e =>
        {
            if (e.Message.Contains("連線中斷")) errors.Enqueue(e.Message);
            if (e.Message.Contains("已重新連線") || e.Message.Contains("已連線")) infos.Enqueue(e.Message);
        };
        using var conn = new Controllers.ConnectionManager();
        conn.ConfigureForTest(TimeSpan.FromMilliseconds(400), TimeSpan.FromMilliseconds(400),
                              maxConsecutiveFails: 2, beatIntervalMs: 150);
        conn.Start(cfg, log);

        // Phase 1：健康 server → 綠
        Check(await WaitUntil(() => conn.IsIpConnected, 5000), "Phase1 假 IP 健康 → IsIpConnected=true");

        // Phase 2：單次逾時 → 不翻線、不記斷線 log（★8 的核心語意）
        int errBefore = errors.Count;
        System.Threading.Volatile.Write(ref stall, 1);
        bool stayedGreen = true;
        var t1 = Environment.TickCount64;
        while (Environment.TickCount64 - t1 < 1500)                 // 涵蓋失敗那拍 + 恢復拍
        {
            if (!conn.IsIpConnected) { stayedGreen = false; break; }
            await Task.Delay(20);
        }
        Check(stayedGreen, "Phase2 單次逾時 → 全程維持綠燈（未達 2 次門檻不翻線）");
        Check(errors.Count == errBefore, "Phase2 單次逾時 → 無「連線中斷」log（不洗版）");

        // Phase 3：全面停擺 → 連續 2 次失敗 → 翻紅 + 記 log
        System.Threading.Volatile.Write(ref stall, -1);
        Check(await WaitUntil(() => !conn.IsIpConnected, 5000), "Phase3 連續失敗 ≥2 → IsIpConnected=false");
        Check(await WaitUntil(() => errors.Count > errBefore, 2000), "Phase3 有「連線中斷」log");

        // Phase 4：恢復 → 自動重連 → 綠 + 記重連 log
        int infoBefore = infos.Count;
        System.Threading.Volatile.Write(ref stall, 0);
        Check(await WaitUntil(() => conn.IsIpConnected, 8000), "Phase4 server 恢復 → 自動重連 IsIpConnected=true");
        Check(await WaitUntil(() => infos.Count > infoBefore, 2000), "Phase4 有「已重新連線」log");

        serverCts.Cancel();
        listener.Stop();
        Console.WriteLine($"結果: {pass} pass, {fail} fail");
        return fail == 0 ? 0 : 1;
    }

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

        // #6 多 IP 配方單一入口：切 IP → 編該台的 {recipe}/{IP}/RecipeInfo.xml，per-IP 隔離
        var mstore = new RecipeStore(svc.Recipes, svc.Log, new[] { "IP0", "IP1" });
        mstore.Select("MULTIIP_TEST");
        mstore.SelectedIp = "IP0"; mstore.PrimaryZone!.PitchX = 11; mstore.Save();
        mstore.SelectedIp = "IP1"; mstore.PrimaryZone!.PitchX = 22; mstore.Save();
        var p0 = svc.Recipes.RecipeXmlPath("MULTIIP_TEST", "IP0");
        var p1 = svc.Recipes.RecipeXmlPath("MULTIIP_TEST", "IP1");
        bool ipIso = System.IO.File.Exists(p0) && System.IO.File.Exists(p1)
                     && System.IO.File.ReadAllText(p0).Contains("<PitchX>11</PitchX>")
                     && System.IO.File.ReadAllText(p1).Contains("<PitchX>22</PitchX>");
        mstore.SelectedIp = "IP0";   // 切回應載到 11
        bool ipReload = mstore.PrimaryZone!.PitchX == 11;
        Console.WriteLine($"  多 IP: IpNames=[{string.Join(",", mstore.IpNames)}] IP0.PitchX=11 / IP1.PitchX=22 隔離={ipIso} 切回載={ipReload}");
        Console.WriteLine(ipIso && ipReload ? "✓ 多 IP 單一入口:per-IP 配方隔離 + 切 IP 重載" : "✗ 多 IP 不符");

        // #34 per-IP 對位 Mark（M_AlignRoi）：每台 CCD 自己的對位樣板/參考點 → 隔離 + 存回各 IP RecipeInfo.xml + 切回重載
        mstore.SelectedIp = "IP0";
        mstore.Recipe.AlignRoi.AlignEnable = true;
        mstore.Recipe.AlignRoi.ReferX = 100; mstore.Recipe.AlignRoi.ReferY = 200;
        mstore.Recipe.AlignRoi.SearchWidth = 640; mstore.Recipe.AlignRoi.SearchHeight = 480;
        mstore.Recipe.AlignRoi.PatternPath = "mark_ip0.tif";
        mstore.Save();
        mstore.SelectedIp = "IP1";
        mstore.Recipe.AlignRoi.AlignEnable = false;
        mstore.Recipe.AlignRoi.ReferX = 300; mstore.Recipe.AlignRoi.ReferY = 400;
        mstore.Recipe.AlignRoi.PatternPath = "mark_ip1.tif";
        mstore.Save();
        var a0 = System.IO.File.ReadAllText(p0);
        var a1 = System.IO.File.ReadAllText(p1);
        bool alignIso = a0.Contains("<ReferX>100</ReferX>") && a0.Contains("<PatternPath>mark_ip0.tif</PatternPath>")
                        && a0.Contains("<AlignEnable>true</AlignEnable>")
                        && a1.Contains("<ReferX>300</ReferX>") && a1.Contains("<PatternPath>mark_ip1.tif</PatternPath>");
        mstore.SelectedIp = "IP0";   // 切回應載到 IP0 的對位 Mark
        bool alignReload = mstore.Recipe.AlignRoi.ReferX == 100
                           && mstore.Recipe.AlignRoi.PatternPath == "mark_ip0.tif"
                           && mstore.Recipe.AlignRoi.AlignEnable;
        Console.WriteLine($"  對位 Mark: IP0 ReferX=100/mark_ip0 / IP1 ReferX=300/mark_ip1 隔離={alignIso} 切回載={alignReload}");
        Console.WriteLine(alignIso && alignReload ? "✓ per-IP M_AlignRoi 隔離 + 存回各 IP RecipeInfo.xml + 切回重載" : "✗ 對位 Mark 不符");

        return shared && xmlOk && ipIso && ipReload && alignIso && alignReload ? 0 : 1;
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

    // ---- 「從 IP 載入影像」遠端瀏覽 + 預覽 + 全解析度檢測（L2，假 IP server，不需真 GPU/Linux）----
    // 假 IP server 回 LIST_DIR(目錄+影像) / GET_IMAGE_PREVIEW(小 PNG base64 + full 8192×5000) /
    //   LOAD_RECIPE(OK) / REVIEW_LOCAL_IMAGE(result 2 缺陷)。驗：
    //  ① RemoteImageBrowserViewModel 列舉/導航；② Step1 遠端載入：預覽 decode + ImageWidth/Height=全解析度 +
    //     PixelData=null（像素值遠端關閉）+ IsRemoteImage；③ Test 路由 REVIEW_LOCAL_IMAGE → 缺陷疊上去。
    private static async Task<int> RemoteImgTest()
    {
        // 縮小灰階 PNG（模擬 IP 的 GET_IMAGE_PREVIEW 回傳）：用 ImageSharp 即時產生有效 PNG bytes，
        // 確保 wire 契約「IP 回得了可 decode 的 PNG」可被驗（避免硬編 base64 CRC 失效）。
        string previewB64;
        using (var pv = new SixLabors.ImageSharp.Image<SixLabors.ImageSharp.PixelFormats.L8>(8, 8))
        using (var pms = new System.IO.MemoryStream())
        { pv.Save(pms, new SixLabors.ImageSharp.Formats.Png.PngEncoder()); previewB64 = Convert.ToBase64String(pms.ToArray()); }
        const string imgPath = "/home/auo/T550QVN10_TGT_G/img001.tif";

        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        int port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
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
                string resp = cmd switch
                {
                    "LIST_DIR" =>
                        $"{{\"seq\":{seq},\"status\":\"OK\",\"dir\":\"/home/auo/T550QVN10_TGT_G\",\"entries\":[" +
                        "{\"name\":\"..\",\"is_dir\":true,\"size\":0,\"path\":\"/home/auo\"}," +
                        "{\"name\":\"sub\",\"is_dir\":true,\"size\":0,\"path\":\"/home/auo/T550QVN10_TGT_G/sub\"}," +
                        $"{{\"name\":\"img001.tif\",\"is_dir\":false,\"size\":41943040,\"path\":\"{imgPath}\"}}]}}",
                    "GET_IMAGE_PREVIEW" =>
                        $"{{\"seq\":{seq},\"status\":\"OK\",\"png_base64\":\"{previewB64}\"," +
                        "\"full_width\":8192,\"full_height\":5000,\"preview_width\":2048,\"preview_height\":1250}",
                    "REVIEW_LOCAL_IMAGE" =>
                        $"{{\"seq\":{seq},\"status\":\"OK\",\"result\":{{\"panel_id\":\"img001\",\"DefectCnt\":2," +
                        "\"RoiInfoList\":[{\"RoiIndex\":0,\"DefectInfoList\":[" +
                        "{\"GC_X\":100,\"GC_Y\":200,\"Size\":5,\"Type\":\"PointBright\"}," +
                        "{\"GC_X\":300,\"GC_Y\":400,\"Size\":7,\"Type\":\"PointDark\"}]}]}}",
                    _ => $"{{\"seq\":{seq},\"status\":\"OK\"}}",   // LOAD_RECIPE 等
                };
                await ns.WriteAsync(System.Text.Encoding.UTF8.GetBytes(resp + "\n"));
                await ns.FlushAsync();
            }
        });

        var svc = AppServices.Build();
        await svc.Connection.Ip.ConnectAsync("127.0.0.1", port);

        // ① 遠端瀏覽器列舉/導航
        var browser = new ViewModels.RemoteImageBrowserViewModel(svc, "/home/auo/T550QVN10_TGT_G");
        await browser.NavigateAsync("/home/auo/T550QVN10_TGT_G");
        bool listed = browser.Entries.Count == 3
                      && browser.Entries[0].IsDir && browser.Entries[0].Name == ".."   // 上層在前
                      && browser.Entries.Any(e => !e.IsDir && e.Name == "img001.tif");
        var imgEntry = browser.Entries.First(e => !e.IsDir);
        bool openImg = await browser.OpenAsync(imgEntry);                 // 影像 → true（回傳 Path）
        bool openDir = !await browser.OpenAsync(browser.Entries[1]);      // 目錄 → false（進入）

        // 預覽 PNG bytes 合法性：headless 無 Avalonia render backend → 改用 ImageSharp 驗 base64 解得出有效影像
        // （production 用 Avalonia Bitmap 顯示；wire 契約=回得了可 decode 的 PNG 由此確認；實機顯示 L3 驗）。
        bool pngValid;
        try { using var p = SixLabors.ImageSharp.Image.Load(Convert.FromBase64String(previewB64)); pngValid = p.Width >= 1 && p.Height >= 1; }
        catch (Exception ex) { pngValid = false; Console.WriteLine($"  [pngValid 失敗] {ex.Message}"); }

        // ② Step1 遠端載入：注入 picker 回影像路徑 → 取預覽 + 全解析度寬高
        var step1 = new ViewModels.Step1ViewModel(svc) { RemoteImagePicker = () => Task.FromResult<string?>(imgPath) };
        await step1.BrowseRemoteImageCommand.ExecuteAsync(null);
        bool preview = pngValid
                       && step1.ImageWidth == 8192 && step1.ImageHeight == 5000   // ★ ROI 走全解析度（非預覽 1×1）
                       && step1.PixelData is null                                  // 像素值遠端關閉
                       && step1.IsRemoteImage && step1.ImageLoaded;

        // ③ Test 路由 REVIEW_LOCAL_IMAGE → 缺陷疊在預覽上（縮圖牆遠端略過）
        await step1.RunAnalysisCommand.ExecuteAsync(null);
        bool detect = step1.NavDefects.Count == 2 && step1.Thumbs.Count == 0       // 縮圖牆遠端略過（全圖不在 Mac）
                      && step1.IsRemoteImage;                                        // 仍遠端模式（未被本機重建覆蓋）

        svc.Connection.Ip.Disconnect();
        listener.Stop();

        Console.WriteLine($"  ① 遠端瀏覽列舉 3 項(.. 在前)+雙擊影像回 true/目錄進入: {(listed && openImg && openDir ? "PASS" : "FAIL")} (實得 {browser.Entries.Count} 項)");
        Console.WriteLine($"  ② 遠端載入：預覽 decode + ImageSize=全解析度 8192×5000 + PixelData=null(像素值關) + IsRemoteImage: {(preview ? "PASS" : "FAIL")} (實得 {step1.ImageWidth}×{step1.ImageHeight} px={(step1.PixelData is null ? "null" : "set")})");
        Console.WriteLine($"  ③ Test 路由 REVIEW_LOCAL_IMAGE → 2 缺陷疊預覽 + 縮圖牆略過: {(detect ? "PASS" : "FAIL")} (實得 NavDefects={step1.NavDefects.Count} Thumbs={step1.Thumbs.Count})");
        bool ok = listed && openImg && openDir && preview && detect;
        Console.WriteLine(ok ? "✓ 從 IP 載入：遠端瀏覽 + 縮小預覽顯示(全解析度寬高/像素值關) + Test 走全解析度檢測(bit-exact 路徑)"
                             : "✗ 不符");
        return ok ? 0 : 1;
    }
}
