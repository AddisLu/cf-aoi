using CfAoiControl.Controllers;
using CfAoiControl.Models;

namespace CfAoiControl.Services;

/// <summary>手動 DI 容器：集中組裝服務，傳給 ViewModel。</summary>
public sealed class AppServices
{
    public SystemConfigModel Config { get; }
    public LogService Log { get; }
    public ConnectionManager Connection { get; }
    public RecipeService Recipes { get; }
    public OfflineReviewService Review { get; }

    public AppServices(SystemConfigModel cfg, LogService log, ConnectionManager conn,
                       RecipeService recipes, OfflineReviewService review)
    {
        Config = cfg; Log = log; Connection = conn; Recipes = recipes; Review = review;
    }

    /// <summary>正式組裝（讀 appsettings.json）。</summary>
    public static AppServices Build()
    {
        var cfg = ConfigLoader.Load();
        var log = new LogService();
        var conn = new ConnectionManager();
        var recipes = new RecipeService(cfg, log);
        var review = new OfflineReviewService(conn.Ip, recipes, cfg, log);
        return new AppServices(cfg, log, conn, recipes, review);
    }

    /// <summary>設計階段/預覽用（最小可運作，不連線）。</summary>
    public static AppServices DesignTime()
    {
        var cfg = new SystemConfigModel();
        var log = new LogService();
        var conn = new ConnectionManager();
        var recipes = new RecipeService(cfg, log);
        var review = new OfflineReviewService(conn.Ip, recipes, cfg, log);
        return new AppServices(cfg, log, conn, recipes, review);
    }
}
