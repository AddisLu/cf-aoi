using System.Xml.Serialization;

namespace CfAoiControl.Models;

/// <summary>
/// 對齊 legacy ClibCf/RecipeSetting.cs（RecipeSetting.xml，每配方共用設定）。
/// 存圖尺寸 / AI 尺寸 / 最大缺陷數等。IP 目前未讀此檔，保留為 legacy 相容與未來用。
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
}
