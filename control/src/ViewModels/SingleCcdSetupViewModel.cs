using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CfAoiControl.ViewModels;

/// <summary>
/// 單 CCD 設定整合頁（塊3-子塊1，A 版薄殼）。**組合既有 VM 類別、非重做**：
/// 持有 Step1ViewModel(左：影像/ROI) + ZoneParamEditorViewModel(右：27 參數 + 對位 Mark card) 各一個實例，
/// 嵌入的 View 直接綁它們——不在本 VM 重做 Step1 的 duck-typed 介面(SourceBitmap/命令…)。
///
/// 實例策略：**獨立新實例**（非沿用 Step1/配方編輯 螢幕的同一實例）。影響：
///   - 影像視埠狀態（縮放/平移/載入的 TIFF）獨立於 Step1 螢幕，互不干擾。
///   - recipe / PrimaryZone / AlignRoi 仍經**共用 RecipeStore**（單一資料源）同步，故參數/ROI 編輯跨頁一致。
/// 約束①：header 顯 ccd_id(CCD00)，底層 RecipeStore.SelectedIp = recipe_partition(IP0 儲存鍵)；不改名、不改嵌入編輯器下拉。
/// </summary>
public sealed partial class SingleCcdSetupViewModel : ViewModelBase
{
    private readonly AppServices _svc;

    public Step1ViewModel Step1 { get; }
    public ZoneParamEditorViewModel ZoneEditor { get; }

    [ObservableProperty] private string headerText = "未選 CCD（從系統設定的宣告陣列點一顆 CCD 進入）";
    [ObservableProperty] private bool hasSlot;

    public SingleCcdSetupViewModel(AppServices svc)
    {
        _svc = svc;
        Step1 = new Step1ViewModel(svc);                 // 獨立影像視埠實例
        ZoneEditor = new ZoneParamEditorViewModel(svc);  // 27 參數 + 對位 Mark card（既有）
    }

    /// <summary>master→detail：載入某宣告槽。設 RecipeStore.SelectedIp = recipe_partition（IP0 儲存鍵，觸發重載該 CCD 配方）；
    /// header 顯 ccd_id + 運算單元（唯讀）。底圖 = 沿用 Step1 BrowseImage 載入 TIFF（A1 實拍 = Phase 2）。</summary>
    public void LoadSlot(CcdSlotModel slot)
    {
        _svc.RecipeStore.SelectedIp = slot.RecipePartition;
        HeaderText = $"{slot.CcdId} · 由 {slot.ComputeUnit} 運算";
        HasSlot = true;
    }
}
