using Avalonia.Controls;
using Avalonia.Markup.Xaml;
using Avalonia.Threading;
using CfAoiControl.ViewModels;

namespace CfAoiControl.Views;

/// <summary>遠端影像瀏覽對話框（Window）。ShowDialog&lt;string?&gt; 回傳選中的遠端路徑（取消=null）。</summary>
public partial class RemoteImageBrowserView : Window
{
    public RemoteImageBrowserView()
    {
        AvaloniaXamlLoader.Load(this);

        var list = this.FindControl<ListBox>("EntryList");
        if (list != null)
            list.DoubleTapped += async (_, _) =>
            {
                if (DataContext is RemoteImageBrowserViewModel vm && list.SelectedItem is RemoteEntry e)
                    if (await vm.OpenAsync(e)) Close(e.Path);   // 影像 → 關閉回傳路徑；目錄 → 進入
            };

        if (this.FindControl<Button>("BtnLoad") is { } load)
            load.Click += (_, _) =>
            {
                if (DataContext is RemoteImageBrowserViewModel { SelectedEntry: { IsDir: false } e }) Close(e.Path);
            };
        if (this.FindControl<Button>("BtnCancel") is { } cancel)
            cancel.Click += (_, _) => Close((string?)null);

        // 開窗即列出起始目錄
        Opened += (_, _) =>
        {
            if (DataContext is RemoteImageBrowserViewModel vm)
                Dispatcher.UIThread.Post(async () => await vm.NavigateAsync(vm.CurrentDir));
        };
    }
}
