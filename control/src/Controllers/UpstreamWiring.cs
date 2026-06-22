using System;
using System.Linq;
using CfAoiControl.Services;

namespace CfAoiControl.Controllers;

/// <summary>
/// 把 UpstreamServer 的 CF_ 回呼接到既有 IP 流程（**重用 IpClient/RecipeService/RecipeStore，非重做**）。
/// offline 接不了的（取像 GRAB_START / 對位 CHECK_ALIGN·SET_ALIGN）**刻意不綁** → UpstreamServer 走
/// 誠實失敗分支（非假 OK，決策 A）。真對位/取像 = #1/Step4。
/// 抽成靜態 Bind(server, svc)：MainWindowViewModel 與 selftest 共用同一接線邏輯（selftest 能驗真接線）。
/// </summary>
public static class UpstreamWiring
{
    public static void Bind(UpstreamServer up, AppServices svc)
    {
        // 上位機 client 連上/斷線 → 連線燈（真）
        up.OnConnectedChanged = v => svc.Connection.SetUpstreamConnected(v);

        // CF_LOAD_RECIPE → 載入該配方 + 送 IP（重用 IpClient.LoadRecipeAsync，對齊 MainWindowViewModel.CfLoadRecipe）
        up.OnLoadRecipe = async (recipe, panelId, _detectMode) =>
        {
            try
            {
                svc.RecipeStore.Select(recipe);
                var xml = svc.Recipes.ToXmlString(svc.RecipeStore.Recipe);
                // #16/#32 + 存圖設定：per-recipe RecipeSaving 經 recipe_saving 一併送 IP
                var saving = svc.RecipeStore.RecipeSaving.BuildRecipeSavingJson();
                var resp = await svc.Connection.Ip.LoadRecipeAsync(recipe, panelId, xml, recipeSaving: saving);
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

        // OnGrabStart / OnCheckAlign / OnSetAlign：offline 刻意不綁 → UpstreamServer 回誠實失敗（決策 A）。
    }
}
