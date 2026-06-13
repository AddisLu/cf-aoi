using System.Collections.Generic;
using System.Xml.Serialization;

namespace CfAoiControl.Models;

// ============================================================================
// 對齊 legacy ClibCf/Recipe.cs 的 Recipe 結構（RecipeInfo.xml，每台 IP 一份）。
// IP 的 from_recipe_xml 只字串搜尋 <DetectRoi> 內的欄位，故 root/wrapper 名稱主要為
// legacy 工具相容；DetectRoi 內容才是 IP 解析依據。
// ============================================================================

/// <summary>對位 ROI — legacy AlignRoi。</summary>
public sealed class AlignRoiModel
{
    [XmlElement("AlignEnable")]     public bool AlignEnable { get; set; } = false;
    [XmlElement("AlignResultSave")] public bool AlignResultSave { get; set; } = true;
    [XmlElement("PatternPath")]     public string PatternPath { get; set; } = "";
    [XmlElement("ReferX")]          public int ReferX { get; set; } = -1;
    [XmlElement("ReferY")]          public int ReferY { get; set; } = -1;
    [XmlElement("SearchWidth")]     public int SearchWidth { get; set; } = 1000;
    [XmlElement("SearchHeight")]    public int SearchHeight { get; set; } = 1000;
}

/// <summary>檢測 IOI — legacy DetectIoi（僅 ROI 範圍）。</summary>
public sealed class DetectIoiModel
{
    [XmlElement("StartX")] public int StartX { get; set; } = -1;
    [XmlElement("StartY")] public int StartY { get; set; } = -1;
    [XmlElement("EndX")]   public int EndX { get; set; } = -1;
    [XmlElement("EndY")]   public int EndY { get; set; } = -1;
}

/// <summary>配方 — legacy Recipe（root 元素 Recipe）。</summary>
[XmlRoot("Recipe")]
public sealed class RecipeModel
{
    [XmlElement("M_AlignRoi")]
    public AlignRoiModel AlignRoi { get; set; } = new();

    [XmlArray("DetectRoiList")] [XmlArrayItem("DetectRoi")]
    public List<ZoneSettingModel> DetectRoiList { get; set; } = new();

    [XmlArray("DetectIoiList")] [XmlArrayItem("DetectIoi")]
    public List<DetectIoiModel> DetectIoiList { get; set; } = new();
}
