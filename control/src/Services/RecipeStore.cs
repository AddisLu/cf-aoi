using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using CfAoiControl.Models;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CfAoiControl.Services;

/// <summary>
/// 配方單一資料來源（single source of truth）。所有視圖（主視窗 / Step1 / ZoneParamEditor）
/// 共用此一份 Recipe + PrimaryZone，任一處修改其他處透過資料綁定自動同步。
/// 切換 SelectedRecipe → 重新載入並觸發 RecipeReloaded，各視圖重綁。
/// </summary>
public sealed partial class RecipeStore : ObservableObject
{
    private readonly RecipeService _recipes;
    private readonly LogService _log;

    public ObservableCollection<string> RecipeNames { get; } = new();
    /// <summary>可編輯的 IP/CCD 清單（單一入口的 IP 選擇器來源；現預設單台 IP0,可由 config 擴充）。</summary>
    public ObservableCollection<string> IpNames { get; } = new();

    [ObservableProperty] private string selectedRecipe = "DEFAULT";
    // 目前編輯中的 IP/CCD（多 IP 配方:每台一份 {recipe}/{ip}/RecipeInfo.xml）。
    [ObservableProperty] private string selectedIp = "IP0";
    [ObservableProperty] private RecipeModel recipe = new();
    // 第一個 DetectRoi：快速調參 / 主視窗預覽共用同一實例
    [ObservableProperty] private ZoneSettingModel? primaryZone;
    // per-recipe 存圖設定（= legacy RecipeSetting.xml），切換配方一起載入
    [ObservableProperty] private RecipeSavingModel recipeSaving = new();

    /// <summary>配方重新載入（切換配方 / 首次載入）後觸發，供各 VM 重建清單。</summary>
    public event Action? RecipeReloaded;

    private bool _inSelect;

    public RecipeStore(RecipeService recipes, LogService log, IReadOnlyList<string>? ipNames = null)
    {
        _recipes = recipes; _log = log;
        foreach (var ip in (ipNames is { Count: > 0 } ? ipNames : new[] { "IP0" })) IpNames.Add(ip);
        SelectedIp = IpNames[0];
        RefreshNames();
        Select(SelectedRecipe);
    }

    partial void OnSelectedRecipeChanged(string value) => Select(value);
    partial void OnSelectedIpChanged(string value) => Select(SelectedRecipe);   // 切 IP → 載該 IP 的配方

    /// <summary>同步載入（桌面檔案 IO，避免 async 競態）。設 SelectedRecipe + Recipe + PrimaryZone。</summary>
    public void Select(string name)
    {
        if (_inSelect) return;
        _inSelect = true;
        try
        {
            if (SelectedRecipe != name) SelectedRecipe = name;   // 與資料一致
            var ensure = _recipes.EnsureRecipeExists(name, SelectedIp);
            Recipe = ensure.Recipe;
            if (Recipe.DetectRoiList.Count == 0) Recipe.DetectRoiList.Add(new ZoneSettingModel());
            PrimaryZone = Recipe.DetectRoiList[0];
            RecipeSaving = _recipes.LoadRecipeSetting(name);   // per-recipe 存圖設定一起載入
            RecipeReloaded?.Invoke();
        }
        finally { _inSelect = false; }
    }

    public Task SelectAsync(string name) { Select(name); return Task.CompletedTask; }

    public void Save()
    {
        _recipes.Save(SelectedRecipe, Recipe, SelectedIp);
        if (!RecipeNames.Contains(SelectedRecipe)) RecipeNames.Add(SelectedRecipe);
    }

    public Task SaveAsync() { Save(); return Task.CompletedTask; }

    /// <summary>#33 SaveAll：把目前編輯中的 Recipe 存到所有 IP 分區（多 CCD 一次套用同參數）。</summary>
    public void SaveToAllIps()
    {
        foreach (var ip in IpNames) _recipes.Save(SelectedRecipe, Recipe, ip);
        if (!RecipeNames.Contains(SelectedRecipe)) RecipeNames.Add(SelectedRecipe);
        _log.Info($"配方 '{SelectedRecipe}' 已存到所有 IP：{string.Join(",", IpNames)}");
    }

    /// <summary>#33 刪除配方（整個資料夾）。若刪的是目前配方 → 退回 DEFAULT。</summary>
    public void DeleteRecipe(string name)
    {
        _recipes.DeleteRecipe(name);
        RefreshNames();
        if (SelectedRecipe == name) Select("DEFAULT");
    }

    /// <summary>#33 開資料夾用：配方資料夾路徑。</summary>
    public string RecipeFolder(string name) => _recipes.RecipeFolder(name);

    /// <summary>#7 把目前配方某 IP 參數批次複製到多個目標配方。回傳實際複製數。</summary>
    public int CopyParamsToMany(System.Collections.Generic.IEnumerable<string> dstRecipes)
        => _recipes.CopyRecipeParamsToMany(SelectedRecipe, dstRecipes, SelectedIp);

    /// <summary>存 per-recipe RecipeSetting.xml（主視窗 RecipeSetting 面板「儲存」用）。</summary>
    public void SaveRecipeSetting() => _recipes.SaveRecipeSetting(SelectedRecipe, RecipeSaving);

    public void RefreshNames()
    {
        RecipeNames.Clear();
        RecipeNames.Add("DEFAULT");
        var root = RecipeService.ExpandPath(_recipes.RecipeDirRoot);
        if (Directory.Exists(root))
            foreach (var d in Directory.GetDirectories(root))
            {
                var n = Path.GetFileName(d);
                if (!RecipeNames.Contains(n)) RecipeNames.Add(n);
            }
    }
}
