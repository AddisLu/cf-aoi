using System;
using System.Linq;
using CfAoiControl.Services;

namespace CfAoiControl.Controllers;

/// <summary>
/// 把 UpstreamServer 的 CF_ 回呼接到既有 IP/Grab 流程（**重用 IpClient/GrabClient/RecipeService，非重做**）。
/// 觸發鏈（37 CCD 軟體觸發設計，2026-07-21）：進片 sensor → 上位機（與 Control 同機）→
///   CF_LOAD_RECIPE → IP 載配方 + Grab LOAD_RECIPE/GRAB_ARM 預熱（冷啟重活提前）
///   CF_GRAB_START  → Grab GRAB_START 觸發本體（僅 ms 級 start_all）
///   CF_STOP        → Grab GRAB_STOP（teardown）
/// Grab 未連線時上述自然回誠實失敗 ERR（決策 A 精神不變）；對位 CHECK_ALIGN/SET_ALIGN 仍不綁（#1/Step4）。
/// 抽成靜態 Bind(server, svc)：MainWindowViewModel 與 selftest 共用同一接線邏輯（selftest 能驗真接線）。
/// </summary>
public static class UpstreamWiring
{
    public static void Bind(UpstreamServer up, AppServices svc)
    {
        // 上位機 client 連上/斷線 → 連線燈（真）
        up.OnConnectedChanged = v => svc.Connection.SetUpstreamConnected(v);

        // CF_LOAD_RECIPE → 載入該配方 + 送 IP（重用 IpClient.LoadRecipeAsync，對齊 MainWindowViewModel.CfLoadRecipe）
        // ＋ Grab 預熱：LOAD_RECIPE（panel_id）+ GRAB_ARM（開陣列/套參數/RDMA connect，冪等）。
        // Grab 預熱失敗不擋 CF_LOAD_RECIPE（配方已載成功）——延遲風險在 CF_GRAB_START 誠實回 ERR，
        // 此處僅 Log.Warn 提前示警。
        up.OnLoadRecipe = async (recipe, panelId, _detectMode) =>
        {
            try
            {
                svc.RecipeStore.Select(recipe);
                var xml = svc.Recipes.ToXmlString(svc.RecipeStore.Recipe);
                // #16/#32 + 存圖設定：per-recipe RecipeSaving 經 recipe_saving 一併送 IP
                var saving = svc.RecipeStore.RecipeSaving.BuildRecipeSavingJson();
                var resp = await svc.Connection.Ip.LoadRecipeAsync(recipe, panelId, xml, recipeSaving: saving);
                var ipOk = resp?["status"]?.GetValue<string>() == "OK";
                if (!ipOk) return false;

                try
                {
                    await svc.Connection.Grab.LoadRecipeAsync(recipe, panelId);
                    var arm = await svc.Connection.Grab.GrabArmAsync();
                    if (arm?["status"]?.GetValue<string>() != "OK")
                        svc.Log.Warn($"Grab 預熱（GRAB_ARM）失敗：{arm?["error"]?.GetValue<string>() ?? "無回應"} — GRAB_START 將冷啟或失敗");
                }
                catch (Exception ex)
                {
                    svc.Log.Warn($"Grab 預熱不可用（{ex.Message}）— CF_GRAB_START 時將誠實回 ERR 或冷啟");
                }
                return true;
            }
            catch { return false; }
        };

        // CF_GRAB_START → Grab GRAB_START（觸發本體；已 ARM 時僅 ms 級）。
        // frames_per_panel 取 appsettings Grab.FramesPerPanel（0=連續 legacy）。
        // Grab 未連線/失敗 → false → 上位機收 ERR（誠實失敗，不假 OK）。
        up.OnGrabStart = async timeoutMs =>
        {
            try
            {
                var timeout = int.TryParse(timeoutMs, out var t) && t > 0 ? t : 40000;
                var resp = await svc.Connection.Grab.GrabStartAsync(timeout, svc.Config.Grab.FramesPerPanel);
                var ok = resp?["status"]?.GetValue<string>() == "OK";
                if (!ok) svc.Log.Warn($"GRAB_START 失敗：{resp?["error"]?.GetValue<string>() ?? "無回應"}");
                return ok;
            }
            catch (Exception ex) { svc.Log.Warn($"GRAB_START 例外：{ex.Message}"); return false; }
        };

        // CF_STOP（#25）→ Grab GRAB_STOP（停取像＋斷 RDMA，teardown 解除 ARM）。
        up.OnStop = async () =>
        {
            try
            {
                var resp = await svc.Connection.Grab.GrabStopAsync();
                return resp?["status"]?.GetValue<string>() == "OK";
            }
            catch { return false; }
        };

        // CF_GET_RESULT → 由 IP 列舉缺陷結果夾組「路徑,逗號 + 缺陷數,逗號」(非 JSON，對齊契約)
        up.OnGetResult = async () =>
        {
            try
            {
                var resp = await svc.Connection.Ip.ListDefectFoldersAsync("");
                var folders = resp?["folders"]?.AsArray();
                if (folders is null || folders.Count == 0)
                    return new UpstreamServer.GetResultPayload("", "0");
                var paths  = string.Join(",", folders.Select(f => f?["folder_name"]?.GetValue<string>() ?? ""));
                var counts = string.Join(",", folders.Select(f =>
                {
                    try { return (f?["defect_count"]?.GetValue<int>() ?? 0).ToString(); } catch { return "0"; }
                }));
                return new UpstreamServer.GetResultPayload(paths, counts);
            }
            catch { return new UpstreamServer.GetResultPayload("", "0"); }
        };

        // OnCheckAlign / OnSetAlign：對位仍不綁 → UpstreamServer 回誠實失敗（決策 A，待 #1/Step4）。
        // OnGrabStart / OnStop 已於上方接真 GrabClient（2026-07-21 觸發鏈）；Grab 離線時自然 ERR。
    }
}
