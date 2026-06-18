using System.Collections.Generic;

namespace CfAoiControl.Models;

// ============================================================================
// 對齊 appsettings.json（control/CLAUDE.md §7）。透過 Microsoft.Extensions.Configuration 綁定。
// 不變式：appsettings.json 無 hardcode 位址。
// ============================================================================

public sealed class NodeConfig
{
    public string Host { get; set; } = "127.0.0.1";
    public int Port { get; set; }
    public string Mode { get; set; } = "";
}

public sealed class UpstreamServerConfig
{
    public int ListenPort { get; set; } = 8000;
    public bool Optional { get; set; } = true;
}

public sealed class PathsConfig
{
    public string RecipeDir { get; set; } = "~/cf-aoi/recipes";
    public string OutputDir { get; set; } = "~/cf-aoi/output";
    public string ImageDir { get; set; } = "~/cf-aoi/test_images";
}

public sealed class SystemConfigModel
{
    public UpstreamServerConfig UpstreamServer { get; set; } = new();
    public Dictionary<string, NodeConfig> Nodes { get; set; } = new();
    public string ActiveIpNode { get; set; } = "IpOffline";
    // 配方可編輯的 IP/CCD 清單（單一入口的 IP 選擇器來源）。
    // ⚠️ 預設留空：.NET config 綁定對 List<T> 是「附加」非「取代」，這裡若預填 "IP0"，
    // appsettings 的 RecipeIps 會疊上去 → 重複（IP0,IP0）。空清單時 RecipeStore fallback 成 ["IP0"]。
    // 多台 IP 機（預留 GPU 給外圍 AI 區運算）→ appsettings RecipeIps 加 "IP1","IP2"… 即可擴充。
    public List<string> RecipeIps { get; set; } = new();
    public PathsConfig Paths { get; set; } = new();
    public ShareSettingModel ShareSetting { get; set; } = new();   // 全域系統旗標（appsettings.json）

    public NodeConfig? ActiveIp =>
        Nodes.TryGetValue(ActiveIpNode, out var n) ? n : null;
}
