using System;
using System.Threading;
using System.Threading.Tasks;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

/// <summary>系統設定 + 測試連線（IP 位址 / 路徑來自 appsettings.json，無 hardcode）。</summary>
public partial class SystemSettingsViewModel : ViewModelBase
{
    private readonly AppServices _svc;

    public SystemSettingsViewModel(AppServices svc)
    {
        _svc = svc;
        var ip = svc.Config.ActiveIp;
        IpHost = ip?.Host ?? "127.0.0.1";
        IpPort = ip?.Port ?? 8200;
        RecipeDir = svc.Config.Paths.RecipeDir;
        OutputDir = svc.Config.Paths.OutputDir;
        ImageDir = svc.Config.Paths.ImageDir;
        UpstreamPort = svc.Config.UpstreamServer.ListenPort;
    }

    [ObservableProperty] private string ipHost = "";
    [ObservableProperty] private int ipPort;
    [ObservableProperty] private string recipeDir = "";
    [ObservableProperty] private string outputDir = "";
    [ObservableProperty] private string imageDir = "";
    [ObservableProperty] private int upstreamPort;
    [ObservableProperty] private string testResult = "";

    [RelayCommand]
    private async Task TestConnection()
    {
        TestResult = "連線中…";
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            await _svc.Connection.Ip.ConnectAsync(IpHost, IpPort, cts.Token);
            var health = await _svc.Connection.Ip.CheckHealthAsync(cts.Token);
            TestResult = $"✓ IP OK：{health?.ToJsonString()}";
        }
        catch (Exception ex) { TestResult = $"❌ {ex.Message}"; }
    }
}
