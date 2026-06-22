using System.Text.Json.Nodes;
using System.Xml.Serialization;

namespace CfAoiControl.Models;

/// <summary>
/// 對齊 legacy ClibCf/RecipeSetting.cs（RecipeSetting.xml，每配方共用設定）。
/// 存圖尺寸 / AI 尺寸 / 最大缺陷數 / #32 邊界略過 / #16 Rule 改判等。
/// IP 端經 LOAD_RECIPE 的 <c>recipe_saving</c> 欄位消費這些值（見 <see cref="BuildRecipeSavingJson"/>）。
/// </summary>
[XmlRoot("RecipeSetting")]
public sealed class RecipeSavingModel
{
    [XmlElement("RecipeName")]         public string RecipeName { get; set; } = "";
    [XmlElement("MaxSaveDefectCount")] public int MaxSaveDefectCount { get; set; } = 250;
    [XmlElement("MaxSaveAiOkCount")]   public int MaxSaveAiOkCount { get; set; } = 250;
    [XmlElement("MaxSaveRuleOkCount")] public int MaxSaveRuleOkCount { get; set; } = 250;
    [XmlElement("MaxDefectCountPass")] public int MaxDefectCountPass { get; set; } = 10000;
    [XmlElement("SaveDefectWidth")]    public int SaveDefectWidth { get; set; } = 64;
    [XmlElement("SaveDefectHeight")]   public int SaveDefectHeight { get; set; } = 64;
    [XmlElement("AiDefectWidth")]      public int AiDefectWidth { get; set; } = 64;
    [XmlElement("AiDefectHeight")]     public int AiDefectHeight { get; set; } = 64;
    [XmlElement("SaveAiTrain")]        public bool SaveAiTrain { get; set; } = false;
    [XmlElement("KernalFile")]         public string KernalFile { get; set; } = "";
    [XmlElement("KernalValue")]        public double KernalValue { get; set; } = 273.0;
    [XmlElement("KernalFile2")]        public string KernalFile2 { get; set; } = "";
    [XmlElement("KernalValue2")]       public double KernalValue2 { get; set; } = 8.0;

    // ── #32 邊界略過（BypassEdgeX/Y）：全域缺陷中心落在影像邊緣此範圍內 → 丟棄（對齊 legacy）。預設 0＝停用。
    [XmlElement("BypassEdgeX")]        public int BypassEdgeX { get; set; } = 0;
    [XmlElement("BypassEdgeY")]        public int BypassEdgeY { get; set; } = 0;
    // ── #16 Rule 改判（ImageRule*）：size>NgSize 強制 NG；否則 patch 均值<MeanLow 或 H/W>HdivW → 改判 OK（丟）。
    //    預設停用（ImageRuleEnable=false）→ 完全不改變結果（與 IP defect_rules 預設一致，不破 bit-exact）。
    [XmlElement("ImageRuleEnable")]    public bool   ImageRuleEnable { get; set; } = false;
    [XmlElement("MeanLowThreshold")]   public double MeanLowThreshold { get; set; } = 40.0;
    [XmlElement("HdivWThreshold")]     public double HdivWThreshold { get; set; } = 4.0;
    [XmlElement("NgSizeThreshold")]    public double NgSizeThreshold { get; set; } = 4096.0;

    /// <summary>
    /// 組 LOAD_RECIPE 的 <c>recipe_saving</c> JSON（鍵名與 IP <c>control_server.cpp</c> 解析端逐字對齊）。
    /// 送出端：MainWindowViewModel.CfLoadRecipe / UpstreamWiring / OfflineReviewService 經 IpClient.LoadRecipeAsync 帶上。
    /// </summary>
    public JsonObject BuildRecipeSavingJson() => new()
    {
        ["max_save_defect_count"] = MaxSaveDefectCount,
        ["save_defect_width"]     = SaveDefectWidth,
        ["save_defect_height"]    = SaveDefectHeight,
        ["max_defect_count_pass"] = MaxDefectCountPass,
        // #32 邊界略過
        ["bypass_edge_x"]         = BypassEdgeX,
        ["bypass_edge_y"]         = BypassEdgeY,
        // #16 Rule 改判
        ["image_rule_enable"]     = ImageRuleEnable,
        ["mean_low_threshold"]    = MeanLowThreshold,
        ["hdivw_threshold"]       = HdivWThreshold,
        ["ng_size_threshold"]     = NgSizeThreshold,
    };
}
