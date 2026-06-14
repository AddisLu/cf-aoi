namespace CfAoiControl.Models;

/// <summary>
/// 對齊 legacy ClibCf/ShareSetting.cs（全域系統旗標）。新架構存於 appsettings.json 的 "ShareSetting" 區塊
/// （取代 legacy 的網路 ShareSetting.xml，跨平台一致）。屬性名 = appsettings 鍵（供 ConfigLoader.Bind）。
///
/// 新架構處置（見 docs 規格）：
///   可編輯：SaveSourceImage、DebugAlgorithm（全域預設，Step1 當次可覆寫）、AiRootPath。
///   停用（UI 上 disabled+tooltip）：TuningRecipe、SaveFullImage、BypassAlignment（MIL/對位/多-frame 舊概念）。
/// 本批不自動推給 IP。
/// </summary>
public sealed class ShareSettingModel
{
    // ---- 可編輯 ----
    public bool SaveSourceImage { get; set; } = false;   // 存原圖（Step4 capture / online 存原始 frame）
    public bool DebugAlgorithm { get; set; } = false;    // 算法除錯：全域「預設存全部缺陷小圖」，Step1 當次可覆寫
    public string AiRootPath { get; set; } = "";         // AI/配方根目錄；留空＝用 Paths.RecipeDir（非 legacy 的 O:\Recipe）

    // ---- 目前停用（保留欄位，UI 上灰掉）----
    public bool TuningRecipe { get; set; } = false;      // 空跑不檢測（新流程未啟用）
    public bool SaveFullImage { get; set; } = false;     // 存合圖（多-frame/MIL，未啟用）
    public bool BypassAlignment { get; set; } = false;   // 略過對位（新版無 MIL 對位）
}
