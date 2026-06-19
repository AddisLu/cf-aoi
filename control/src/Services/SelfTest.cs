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
                case "settings": return SettingsTest();
                case "camera": return await CameraTest();
                case "topology": return await TopologyTest();
                case "singleccd": return SingleCcdTest();
                case "upstream": return await UpstreamTest();
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
                    resp = $"{{\"seq\":{seq},\"status\":\"OK\",\"cameras\":[" +
                           "{\"cam_id\":0,\"mac\":\"00:30:53:2A:0B:03\",\"model\":\"raL8192-12gm\",\"serial\":\"25445953\",\"ip\":\"192.168.5.10\",\"online\":true,\"persistent\":true,\"ip_config\":\"Persistent\",\"device_class\":\"BaslerGigE\"}," +
                           "{\"cam_id\":1,\"mac\":\"00:30:53:2B:12:10\",\"model\":\"raL8192-12gm\",\"serial\":\"25445999\",\"ip\":\"169.254.20.5\",\"online\":true,\"persistent\":false,\"ip_config\":\"AutoIP\",\"device_class\":\"BaslerGigE\"}]}";
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

        Console.WriteLine($"  列舉 2 台: {(count ? "PASS" : "FAIL")}");
        Console.WriteLine($"  KPI(偵測) 上線=2 已綁定=1 待綁定=1 離線=0: {(kpi ? "PASS" : "FAIL")} " +
            $"(實得 on={vm.OnlineCount} b={vm.BoundCount} u={vm.UnboundCount} off={vm.OfflineCount})");
        Console.WriteLine($"  分群 bound[CCD00]/unbound[CCD01]/offline[空]: {(grouping ? "PASS" : "FAIL")}");
        Console.WriteLine($"  預選第一台 + HasSelection: {(selected ? "PASS" : "FAIL")}");
        Console.WriteLine($"  欄位解析 (MAC/狀態標籤): {(fields ? "PASS" : "FAIL")}");

        svc.Connection.Grab.Disconnect();
        listener.Stop();
        bool ok = count && kpi && grouping && selected && fields;
        Console.WriteLine(ok ? "✓ 相機總覽：LIST_CAMERAS 解析 + 分群 + KPI 正確（離線維持 0，不假造）"
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
        // 約束②：全部宣告槽「已宣告·未綁」，無人標線上
        bool noOnline = vm.ComputeUnits.SelectMany(g => g.Slots).All(s => s.SlotStatusLabel == "已宣告 · 未綁");

        // 假 LIST_CAMERAS：1 台 → 偵測側單獨呈現，且宣告槽不因列舉而改變（未 merge）
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
            && vm.ComputeUnits.SelectMany(g => g.Slots).All(s => s.SlotStatusLabel == "已宣告 · 未綁");  // 列舉後槽仍未綁（未 merge）
        // 案②：fixture 3 槽 + 假列舉 1 台(unbound) → 配置=宣告數3；偵測類 KPI 反映該 1 台(連線1/待綁1/已綁0/離線0)。
        bool kpiCase2 = vm.DeclaredSlotCount == 3
                        && vm.OnlineCount == 1 && vm.UnboundCount == 1 && vm.BoundCount == 0 && vm.OfflineCount == 0;

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

        Console.WriteLine($"  ① 載入 3 槽/2 運算單元 + 欄位(ccd_id/recipe_partition/expected_mac): {(load && keys ? "PASS" : "FAIL")}");
        Console.WriteLine($"  ② 依 compute_unit 分群 Spark1[2]/Spark2[1] + DeclaredSlotCount=3: {(group ? "PASS" : "FAIL")} (實得 units={vm.ComputeUnits.Count} declared={vm.DeclaredSlotCount})");
        Console.WriteLine($"  ③ 全部宣告槽「已宣告·未綁」無人線上: {(noOnline ? "PASS" : "FAIL")}");
        Console.WriteLine($"  ④ 列舉相機獨立呈現({vm.Cameras.Count} 台)且未 merge 進宣告槽: {(detectedSeparate ? "PASS" : "FAIL")}");
        Console.WriteLine($"  KPI案① 37槽+0列舉 → 配置=37 偵測KPI全0: {(kpiCase1 ? "PASS" : "FAIL")} (實得 配置={declaredAtBoot} 偵測 on/b/u/off=0)");
        Console.WriteLine($"  KPI案② 3宣告槽+假列舉1台 → 配置=3 連線=1 待綁=1 已綁=0 離線=0: {(kpiCase2 ? "PASS" : "FAIL")} " +
            $"(實得 配置={vm.DeclaredSlotCount} on={vm.OnlineCount} b={vm.BoundCount} u={vm.UnboundCount} off={vm.OfflineCount})");
        Console.WriteLine($"  塊2-a 處理 N(真)=槽數 Spark1=2/Spark2=1: {(procN ? "PASS" : "FAIL")} (實得 {sp1.SlotCount}/{sp2.SlotCount})");
        Console.WriteLine($"  塊2-b 負載%(估算,公式非寫死)+標旗標: {(loadCalc ? "PASS" : "FAIL")} (Spark1 EstLoadPct={sp1.EstLoadPct} 期望={expect1}; LoadText=\"{sp1.LoadText}\")");
        Console.WriteLine($"  塊2-c 連線(真): 預設未連不假綠={connDefault} + 規則(active才綠/非active灰/未連灰)={connRule}: {(connDefault && connRule ? "PASS" : "FAIL")}");
        bool block2 = procN && loadCalc && connDefault && connRule;
        bool ok = load && keys && group && noOnline && detectedSeparate && kpiCase1 && kpiCase2 && block2;
        Console.WriteLine(ok ? "✓ 塊1+2：拓樸/分群/配置=宣告數/偵測runtime-only/不假merge + 處理N真/負載估算公式/連線真不假綠"
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
        await Task.Delay(50);
        bool lampGreen = svc.Connection.IsUpstreamConnected;   // 連上時 OnConnectedChanged→燈轉綠

        up.Dispose(); server.Dispose(); svc.Connection.Ip.Disconnect(); ipL.Stop();

        bool ready = rReady.StartsWith("OK");
        bool load  = rLoad.StartsWith("OK");                                          // OnLoadRecipe→假 IP OK
        bool get   = rGet.StartsWith("OK") && rGet.Contains("IP0_panelA_DEFAULT") && rGet.Contains("3,5"); // path+count 非 JSON
        bool checkFail = rCheck.StartsWith("ERR");                                    // ★A 誠實失敗(非假 OK)
        bool setFail   = rSet.StartsWith("ERR");                                      // ★A 誠實失敗(非假 OK)

        Console.WriteLine($"  CF_READY → OK: {(ready ? "PASS" : "FAIL")} (\"{rReady}\")");
        Console.WriteLine($"  CF_LOAD_RECIPE → OK(接 IP): {(load ? "PASS" : "FAIL")} (\"{rLoad}\")");
        Console.WriteLine($"  CF_GET_RESULT → OK|path|count 非JSON: {(get ? "PASS" : "FAIL")} (\"{rGet}\")");
        Console.WriteLine($"  CF_CHECK_ALIGN → ERR 誠實失敗(非假OK): {(checkFail ? "PASS" : "FAIL")} (\"{rCheck}\")");
        Console.WriteLine($"  CF_SET_ALIGN → ERR 誠實失敗(非假OK): {(setFail ? "PASS" : "FAIL")} (\"{rSet}\")");
        Console.WriteLine($"  上位機連線燈轉綠(OnConnectedChanged): {(lampGreen ? "PASS" : "FAIL")}");
        bool ok = ready && load && get && checkFail && setFail && lampGreen;
        Console.WriteLine(ok ? "✓ 上位機 CF_：接線啟動+回呼接 IP+9參數交握；align/grab 誠實失敗(非假OK)；燈轉綠 (L2 in-process)"
                             : "✗ 不符");
        return ok ? 0 : 1;
    }

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
}
