using System.ComponentModel;
using System.Xml.Serialization;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CfAoiControl.Models;

// 對齊 legacy ClibCf/Recipe.cs 的列舉
public enum ImagePreproc { Ip_Remap = 0, Ip_None }

public enum AlgorithmWayCompare
{
    Awc_4_Way_Arrow_Sub = 0, Awc_4_Way_Arrow_Div,
    Awc_2_Way_UD_Sub, Awc_2_Way_UD_Div,
    Awc_2_Way_RL_Sub, Awc_2_Way_RL_Div,
    Awc_8_Way_Star_Sub,
    Awc_8_Way_Star_Div,   // ← 融合 DIV-voting(mode2)：DIV 比值逐路投票（IP 守門認 'div'+'star'→mode2）
    Awc_None
}

/// <summary>
/// 鏡射 legacy DetectRoi（ClibCf/Recipe.cs:36-204）全部 32 個可序列化欄位。
/// 屬性名為 C# 慣用（多數已與 legacy element 同名）；M_ 開頭兩個用 [XmlElement] 映射。
/// XML element 名須與 IP from_recipe_xml 相容（IP 只讀 BrightThreshold/DarkThreshold/PitchX/Y/
/// SearchX/Y/StartX/Y/EndX/Y/AlgorithmCompare，其餘為 legacy 工具相容保留）。
/// 預設值面向 DIV 模式（AlgorithmCompare="DIV", BTH/DTH 為比例域）。
/// </summary>
public partial class ZoneSettingModel : ObservableObject
{
    // ===== ROI =====
    [ObservableProperty][property: Category("ROI")][property: DisplayName("StartX")] private int startX = -1;
    [ObservableProperty][property: Category("ROI")][property: DisplayName("StartY")] private int startY = -1;
    [ObservableProperty][property: Category("ROI")][property: DisplayName("EndX")]   private int endX = -1;
    [ObservableProperty][property: Category("ROI")][property: DisplayName("EndY")]   private int endY = -1;

    // ===== Algorithm / 前處理 =====
    [ObservableProperty][property: Category("Algorithm")][property: XmlElement("M_ImagePreproc")]
    private ImagePreproc imagePreproc = ImagePreproc.Ip_None;
    [ObservableProperty][property: Category("Algorithm")] private int smoothTimes = 1;
    [ObservableProperty][property: Category("Algorithm")] private int smoothTimes2 = 0;
    [ObservableProperty][property: Category("Algorithm")] private double darkThreshold = 0.6;   // → DTH（DIV 比例域）
    [ObservableProperty][property: Category("Algorithm")] private double brightThreshold = 1.4; // → BTH（DIV 比例域）
    [ObservableProperty][property: Category("Algorithm")] private bool sobelDetectEnable = true;
    [ObservableProperty][property: Category("Algorithm")] private int sobelSmoothTimes = 1;
    [ObservableProperty][property: Category("Algorithm")] private int sobelSmoothTimes2 = 0;
    [ObservableProperty][property: Category("Algorithm")] private double sobelDarkThreshold = -20;
    [ObservableProperty][property: Category("Algorithm")] private double sobelBrightThreshold = 20;
    [ObservableProperty][property: Category("Algorithm")] private string algorithmWay = "8-Way-Star";
    [ObservableProperty][property: Category("Algorithm")] private string algorithmCompare = "DIV"; // 本系統只接受 DIV
    [ObservableProperty][property: Category("Algorithm")][property: XmlElement("M_AlgorithmWayCompare")]
    private AlgorithmWayCompare algorithmWayCompare = AlgorithmWayCompare.Awc_None;
    [ObservableProperty][property: Category("Algorithm")] private string adjustment = "";
    [ObservableProperty][property: Category("Algorithm")] private int pitchTime = 3;
    [ObservableProperty][property: Category("Algorithm")] private int chooseAmount = -1;
    [ObservableProperty][property: Category("Algorithm")] private int pitchX = 26;
    [ObservableProperty][property: Category("Algorithm")] private int pitchY = 19;
    [ObservableProperty][property: Category("Algorithm")] private int searchX = 1;
    [ObservableProperty][property: Category("Algorithm")] private int searchY = 1;
    [ObservableProperty][property: Category("Algorithm")] private float edgePassRatio = 0.5f;
    [ObservableProperty][property: Category("Algorithm")] private int edgePassThreshold = 32;

    // ===== Blob Filter =====
    [ObservableProperty][property: Category("Filter")] private double blobMaxSize = 100000;
    [ObservableProperty][property: Category("Filter")] private double blobMinSize = 80;
    [ObservableProperty][property: Category("Filter")] private double blobElongation = 100;
    [ObservableProperty][property: Category("Filter")] private double blobFeretElong = 100;
    [ObservableProperty][property: Category("Filter")] private double blobDarkMergeDistance = 1;
    [ObservableProperty][property: Category("Filter")] private double blobBrightMergeDistance = 1;
    [ObservableProperty][property: Category("Filter")] private double blobAllMergeDistance = 1;

    // ===== 融合 DIV-voting (mode2) 參數（IP 從 DetectRoi 讀取）=====
    [ObservableProperty][property: Category("Fusion")] private int meanLowThreshold = 40;  // dark_eps：暗區棄權門檻(鄰點<此值該路不投,壓暗邊界FP)
    [ObservableProperty][property: Category("Fusion")] private int enableMultiscale = 1;   // 0=關 / 1=+2× / 2=+2×+4×（大顆 Defect resize-redetect 補強）

    // ===== LSC 鏡頭暗角校正（通常機台級;此處可 per-recipe 覆寫,預設關待現場校正係數）=====
    [ObservableProperty][property: Category("LSC")] private bool lscEnable = false;
    [ObservableProperty][property: Category("LSC")] private double lscK1 = 0.15;     // 二次項(主暗角)
    [ObservableProperty][property: Category("LSC")] private double lscK2 = 0.05;     // 四次項
    [ObservableProperty][property: Category("LSC")] private double lscK3 = 0.0;      // 六次項
    [ObservableProperty][property: Category("LSC")] private double lscMaxGain = 1.5; // 增益上限(防過校正)

    /// <summary>UI 顯示用標籤（ZoneList 下拉）。</summary>
    [XmlIgnore]
    public string ZoneLabel => $"Zone ({StartX},{StartY})-({EndX},{EndY}) BTH={BrightThreshold} DTH={DarkThreshold}";

    /// <summary>深拷貝（批次套用到多個 ROI 時用）。</summary>
    public ZoneSettingModel Clone() => (ZoneSettingModel)MemberwiseClone();

    /// <summary>把 o 的所有可寫屬性複製進此物件（編輯緩衝 ↔ ROI 互拷，不換實例）。</summary>
    public void CopyFrom(ZoneSettingModel o)
    {
        foreach (var p in typeof(ZoneSettingModel).GetProperties())
            if (p.CanRead && p.CanWrite) p.SetValue(this, p.GetValue(o));
    }
}
