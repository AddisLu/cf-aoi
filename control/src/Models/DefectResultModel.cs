using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Serialization;
using System.Xml.Serialization;

namespace CfAoiControl.Models;

// ============================================================================
// 對齊 IP 程式實際輸出（ip/src/result_saver.cpp）：
//   - JSON（port 8200 回應 / *_ResultInfo.json）：System.Text.Json，key 見 [JsonPropertyName]
//   - legacy XML（*_ResultInfo.xml）：JudgeResult schema，element 見 [XmlElement]
// 同一組 class 同時支援兩者反序列化。屬性用 C# 慣用名，attribute 映射回線上名稱。
// ============================================================================

/// <summary>單一缺陷 — 對齊 legacy DefectInfo（ClibCf/JudgeResult.cs）。</summary>
public sealed class DefectModel
{
    [JsonPropertyName("RunIndex")]   [XmlElement("RunIndex")]    public int RunIndex { get; set; }
    [JsonPropertyName("GlobalPosX")] [XmlElement("GlobalPosX")]  public int GlobalPosX { get; set; }
    [JsonPropertyName("GlobalPosY")] [XmlElement("GlobalPosY")]  public int GlobalPosY { get; set; }
    [JsonPropertyName("Size")]       [XmlElement("Size")]        public int Size { get; set; }
    [JsonPropertyName("Width")]      [XmlElement("Width")]       public int Width { get; set; }
    [JsonPropertyName("Height")]     [XmlElement("Height")]      public int Height { get; set; }
    [JsonPropertyName("Type")]       [XmlElement("Type")]        public string Type { get; set; } = "";
    [JsonPropertyName("Filter")]     [XmlElement("Filter")]      public string Filter { get; set; } = "NoFilter";
    [JsonPropertyName("Y_Min")]      [XmlElement("Y_Min")]       public int YMin { get; set; }
    [JsonPropertyName("Y_Max")]      [XmlElement("Y_Max")]       public int YMax { get; set; }
    [JsonPropertyName("X_Min")]      [XmlElement("X_Min")]       public int XMin { get; set; }
    [JsonPropertyName("X_Max")]      [XmlElement("X_Max")]       public int XMax { get; set; }
    [JsonPropertyName("GC_Y")]       [XmlElement("GC_Y")]        public int GcY { get; set; }
    [JsonPropertyName("GC_X")]       [XmlElement("GC_X")]        public int GcX { get; set; }
    [JsonPropertyName("CV_Sigma")]   [XmlElement("CV_Sigma")]    public float CvSigma { get; set; }
    [JsonPropertyName("CV_Mean")]    [XmlElement("CV_Mean")]     public float CvMean { get; set; }
    [JsonPropertyName("CV_Min")]     [XmlElement("CV_Min")]      public int CvMin { get; set; }
    [JsonPropertyName("CV_Max")]     [XmlElement("CV_Max")]      public int CvMax { get; set; }
    [JsonPropertyName("GL_Sigma")]   [XmlElement("GL_Sigma")]    public float GlSigma { get; set; }
    [JsonPropertyName("GL_Mean")]    [XmlElement("GL_Mean")]     public float GlMean { get; set; }
    [JsonPropertyName("GL_Min")]     [XmlElement("GL_Min")]      public int GlMin { get; set; }
    [JsonPropertyName("GL_Max")]     [XmlElement("GL_Max")]      public int GlMax { get; set; }
    [JsonPropertyName("AiIndex")]    [XmlElement("AiIndex")]     public int AiIndex { get; set; } = -1;
    [JsonPropertyName("AiType")]     [XmlElement("AiType")]      public string AiType { get; set; } = "";
    [JsonPropertyName("AiScore")]    [XmlElement("AiScore")]     public float AiScore { get; set; } = -1;
    [JsonPropertyName("RuleType")]   [XmlElement("RuleType")]    public string RuleType { get; set; } = "";
    [JsonPropertyName("MeanValue")]  [XmlElement("MeanValue")]   public float MeanValue { get; set; } = -1;
    [JsonPropertyName("DetectReason")][XmlElement("DetectReason")]public string DetectReason { get; set; } = "";
    [JsonPropertyName("ImagePath")]  [XmlElement("ImagePath")]   public string ImagePath { get; set; } = "";

    // === UI 衍生欄位（不序列化）===
    [JsonIgnore] [XmlIgnore] public bool IsBright => Type.Contains("Bright");
    [JsonIgnore] [XmlIgnore] public string SizeStr => $"{Width}×{Height} ({Size})";
    [JsonIgnore] [XmlIgnore] public string AiResult => AiType.Length == 0 ? "-" : AiType;
    [JsonIgnore] [XmlIgnore] public string AiScoreStr => AiScore < 0 ? "-" : AiScore.ToString("F2");
    // 影像 overlay 用（全圖像素座標；Canvas.Left/Top = X_Min/Y_Min）
    [JsonIgnore] [XmlIgnore] public int BoxW => System.Math.Max(1, XMax - XMin + 1);
    [JsonIgnore] [XmlIgnore] public int BoxH => System.Math.Max(1, YMax - YMin + 1);
}

/// <summary>單一 ROI 結果 — JSON 多了 roi_offset/num_defects/process_time_ms；XML 為 legacy RoiInfo。</summary>
public sealed class RoiResultModel
{
    [JsonPropertyName("RoiIndex")] [XmlElement("RoiIndex")] public int RoiIndex { get; set; }

    // JSON-only（IP 額外提供；legacy XML 無）
    [JsonPropertyName("roi_offset_x")]   [XmlIgnore] public int RoiOffsetX { get; set; }
    [JsonPropertyName("roi_offset_y")]   [XmlIgnore] public int RoiOffsetY { get; set; }
    [JsonPropertyName("num_defects")]    [XmlIgnore] public int NumDefects { get; set; }
    [JsonPropertyName("process_time_ms")][XmlIgnore] public double ProcessTimeMs { get; set; }

    [JsonPropertyName("DefectInfoList")]
    [XmlArray("DefectInfoList")] [XmlArrayItem("DefectInfo")]
    public List<DefectModel> Defects { get; set; } = new();
}

/// <summary>整張影像結果 — 頂層對齊 IP JSON；XML 對齊 legacy JudgeResult（root 元素 JudgeResult）。</summary>
[XmlRoot("JudgeResult")]
public sealed class DefectResultModel
{
    // JSON-only metadata（legacy XML 無，故 XmlIgnore）
    [JsonPropertyName("panel_id")]    [XmlIgnore] public string PanelId { get; set; } = "";
    [JsonPropertyName("recipe_name")] [XmlIgnore] public string RecipeName { get; set; } = "";
    [JsonPropertyName("pass")]        [XmlIgnore] public bool Pass { get; set; }
    [JsonPropertyName("total_time_ms")][XmlIgnore] public double TotalTimeMs { get; set; }
    [JsonPropertyName("image_width")] [XmlIgnore] public int ImageWidth { get; set; }
    [JsonPropertyName("image_height")][XmlIgnore] public int ImageHeight { get; set; }

    // 兩端共有
    [JsonPropertyName("DefectCnt")] [XmlElement("DefectCnt")] public int DefectCnt { get; set; }
    [JsonPropertyName("AiOkCnt")]   [XmlElement("AiOkCnt")]   public int AiOkCnt { get; set; }
    [JsonPropertyName("RuleOkCnt")] [XmlElement("RuleOkCnt")] public int RuleOkCnt { get; set; }

    // XML-only（legacy JudgeResult；JSON 無）
    [JsonIgnore] [XmlElement("SaveDefectWidth")]  public int SaveDefectWidth { get; set; }
    [JsonIgnore] [XmlElement("SaveDefectHeight")] public int SaveDefectHeight { get; set; }

    [JsonPropertyName("RoiInfoList")]
    [XmlArray("RoiInfoList")] [XmlArrayItem("RoiInfo")]
    public List<RoiResultModel> RoiInfoList { get; set; } = new();

    // === UI 衍生 ===
    [JsonIgnore] [XmlIgnore] public IEnumerable<DefectModel> AllDefects =>
        RoiInfoList.SelectMany(r => r.Defects);
    [JsonIgnore] [XmlIgnore] public bool IsPass => DefectCnt == 0;
    [JsonIgnore] [XmlIgnore] public string Summary =>
        $"缺陷 {DefectCnt}（AI改判OK {AiOkCnt}）{(IsPass ? "PASS" : "NG")} / {TotalTimeMs:F1}ms";
}
