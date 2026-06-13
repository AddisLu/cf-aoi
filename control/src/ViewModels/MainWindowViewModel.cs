using System;
using Avalonia.Threading;
using CfAoiControl.Controllers;
using CfAoiControl.Models;
using CfAoiControl.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CfAoiControl.ViewModels;

public partial class MainWindowViewModel : ViewModelBase
{
    private readonly ConnectionManager _conn;
    private readonly LogService _log;

    public LogService Log => _log;

    [ObservableProperty] private ViewModelBase? currentView;
    [ObservableProperty] private string statusMessage = "就緒";
    [ObservableProperty] private bool isIpConnected;
    [ObservableProperty] private bool isGrabConnected;
    [ObservableProperty] private bool isUpstreamConnected;

    public Step1ViewModel Step1 { get; }
    public ZoneParamEditorViewModel ZoneEditor { get; }
    public SystemSettingsViewModel Settings { get; }

    // 設計階段預覽用無參數建構式
    public MainWindowViewModel() : this(AppServices.DesignTime()) { }

    public MainWindowViewModel(AppServices svc)
    {
        _conn = svc.Connection;
        _log = svc.Log;
        Step1 = new Step1ViewModel(svc);
        ZoneEditor = new ZoneParamEditorViewModel(svc);
        Settings = new SystemSettingsViewModel(svc);
        CurrentView = Step1;

        _conn.Start(svc.Config);

        // UI 執行緒輪詢連線狀態（避免背景執行緒直接更新綁定屬性）
        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        timer.Tick += (_, _) =>
        {
            IsIpConnected = _conn.IsIpConnected;
            IsGrabConnected = _conn.IsGrabConnected;
            IsUpstreamConnected = _conn.IsUpstreamConnected;
            if (_log.Entries.Count > 0) StatusMessage = _log.Entries[^1].Message;
        };
        timer.Start();
    }

    [RelayCommand] private void SelectStep(string step) => CurrentView = Step1; // Step 2-5 之後實作
    [RelayCommand] private void OpenRecipeEditor() => CurrentView = ZoneEditor;
    [RelayCommand] private void OpenSettings() => CurrentView = Settings;
    [RelayCommand] private void OpenHealthCheck() => CurrentView = Settings;
}
