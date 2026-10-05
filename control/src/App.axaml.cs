using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using CfAoiControl.Services;
using CfAoiControl.ViewModels;
using CfAoiControl.Views;

namespace CfAoiControl;

public partial class App : Application
{
    public override void Initialize()
    {
        AvaloniaXamlLoader.Load(this);
    }

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            var services = AppServices.Build();   // 讀 appsettings.json + 組裝服務
            var vm = new MainWindowViewModel(services);
            // --page <畫面>：啟動直接開到指定頁（dashboard/workbench/step1/sort/settings），
            // 例如工程用捷徑直接開「系統設定」（第一個分頁 = 系統狀態）
            var args = desktop.Args ?? System.Array.Empty<string>();
            var i = System.Array.IndexOf(args, "--page");
            if (i >= 0 && i + 1 < args.Length) vm.NavigateCommand.Execute(args[i + 1]);
            desktop.MainWindow = new MainWindow { DataContext = vm };
        }

        base.OnFrameworkInitializationCompleted();
    }
}
