using System.Threading.Tasks;
using Avalonia.Controls;
using CfAoiControl.Services;
using CfAoiControl.ViewModels;

namespace CfAoiControl.Views;

/// <summary>開「從 IP 載入影像」遠端瀏覽對話框（Step1View / SingleCcdSetupView 共用）。回選中的遠端路徑或 null。</summary>
internal static class RemoteImagePickerHelper
{
    public static async Task<string?> OpenAsync(Control host, AppServices svc)
    {
        if (TopLevel.GetTopLevel(host) is not Window owner) return null;
        var dlg = new RemoteImageBrowserView
        {
            DataContext = new RemoteImageBrowserViewModel(svc, svc.Config.Paths.RemoteImageDir),
        };
        return await dlg.ShowDialog<string?>(owner);
    }
}
