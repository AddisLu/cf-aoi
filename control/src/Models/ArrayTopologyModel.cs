using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;

namespace CfAoiControl.Models;

// ============================================================================
// 多 CCD 陣列「機台層拓樸」宣告（array_topology.json）。見 docs/CLAUDE.md §2 三層模型。
//   約束①（不糊改名）：ccd_id = CCD 概念/UI 名；recipe_partition = 現行儲存鍵(IpName "IP0")，兩者並存解耦。
//   約束②（宣告≠綁定）：本檔只「宣告」槽位結構；expected_mac 多為 null=TBD；
//                       哪台 LIST_CAMERAS 列舉到的真相機 = 哪個槽 的 live 綁定 = #21（不在此 merge）。
// ============================================================================

/// <summary>運算單元（Spark/RTX）。node 對映 appsettings.Nodes 鍵。</summary>
public sealed class ComputeUnitModel
{
    public string Id { get; init; } = "";       // "Spark1"
    public string Node { get; init; } = "";      // appsettings.Nodes 鍵（"IpOffline"…）= 連線目標
    public string Role { get; init; } = "aoi";   // "aoi" / 未來 "ai"
}

/// <summary>
/// 一個 CCD 宣告槽（**純宣告**）。約束②：本型別不得帶任何 live 狀態。
/// 「這個槽現在有沒有相機就位」= 宣告 × 偵測的 join，屬 runtime，
/// 由 ViewModels/SystemSettingsViewModel.cs 的 <c>SlotBinding</c> 計算。
/// （原本此處有寫死的 SlotStatusLabel = "已宣告 · 未綁"，在 grab 提供 bound/ccd_id 後已無意義，故移除。）
/// </summary>
public sealed class CcdSlotModel
{
    public string CcdId { get; init; } = "";           // "CCD00" — UI 名（約束①）
    public string ComputeUnit { get; init; } = "";     // → ComputeUnitModel.Id（這顆由哪台算）
    public string? ExpectedMac { get; init; }           // 可 null = TBD；有值時用於交叉檢查「插錯槽位」
    public string RecipePartition { get; init; } = ""; // "IP0" — 儲存鍵（約束①）→ {recipe}/{IpName}/RecipeInfo.xml

    public bool HasExpectedMac => !string.IsNullOrWhiteSpace(ExpectedMac);
    public string ExpectedMacDisplay => HasExpectedMac ? ExpectedMac! : "TBD";
}

/// <summary>機台層陣列拓樸（所有配方共用）。</summary>
public sealed class ArrayTopologyModel
{
    public int CcdTotalCount { get; init; }
    public List<ComputeUnitModel> ComputeUnits { get; init; } = new();
    public List<CcdSlotModel> Slots { get; init; } = new();

    private static readonly JsonSerializerOptions Opts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,   // ccd_id ↔ CcdId 等
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
    };

    /// <summary>解析 JSON 字串（selftest fixture / 一般載入共用）。</summary>
    public static ArrayTopologyModel Parse(string json)
        => JsonSerializer.Deserialize<ArrayTopologyModel>(json, Opts) ?? new ArrayTopologyModel();

    /// <summary>
    /// 載入拓樸：本機 config/array_topology.json 優先；缺則回退版控模板 config/array_topology.example.json
    /// （比照 cam_config.json：本機檔不版控）。皆缺 → 回空拓樸（不丟例外，UI 顯示 0 槽）。
    /// </summary>
    public static ArrayTopologyModel Load(string? basePath = null)
    {
        var path = ResolveExisting(basePath, "array_topology.json")
                   ?? ResolveExisting(basePath, "array_topology.example.json");
        if (path is null) return new ArrayTopologyModel();
        // ⚠️ 已知限制（docs/code_review_20260802.md K13）：JSON 壞掉（手編 37 槽時漏逗號等）
        // 會被靜默吞成空拓樸，UI 只顯示「0 槽」而查不到是哪一行壞。
        try { return Parse(File.ReadAllText(path)); }
        catch { return new ArrayTopologyModel(); }
    }

    // 候選路徑：{base}/config、{base}、{cwd}/config、{cwd}（兼容 dotnet run 與 build 輸出）。
    private static string? ResolveExisting(string? basePath, string name)
    {
        basePath ??= AppContext.BaseDirectory;
        var cwd = Directory.GetCurrentDirectory();
        foreach (var p in new[]
                 {
                     Path.Combine(basePath, "config", name),
                     Path.Combine(basePath, name),
                     Path.Combine(cwd, "config", name),
                     Path.Combine(cwd, name),
                 })
            if (File.Exists(p)) return p;
        return null;
    }
}
