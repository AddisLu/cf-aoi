using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Markup.Xaml;
using Avalonia.Platform.Storage;
using CfAoiControl.ViewModels;

namespace CfAoiControl.Views;

public partial class Step1View : UserControl
{
    public Step1View()
    {
        AvaloniaXamlLoader.Load(this);
        DataContextChanged += (_, _) => WireFilePicker();
    }

    // 把檔案選取（StorageProvider，需 TopLevel）注入 ViewModel，保持 VM 平台無關。
    private void WireFilePicker()
    {
        if (DataContext is not Step1ViewModel vm) return;
        vm.FilePicker = async () =>
        {
            var top = TopLevel.GetTopLevel(this);
            if (top is null) return null;
            var files = await top.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
            {
                Title = "選擇影像",
                AllowMultiple = false,
                FileTypeFilter = new[]
                {
                    new FilePickerFileType("影像") { Patterns = new[] { "*.tif", "*.tiff", "*.png", "*.bmp", "*.jpg" } }
                },
            });
            return files.Count > 0 ? files[0].TryGetLocalPath() : null;
        };
    }
}
