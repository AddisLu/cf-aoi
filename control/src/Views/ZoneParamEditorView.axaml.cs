using Avalonia.Controls;
using Avalonia.Markup.Xaml;
using Avalonia.Platform.Storage;
using CfAoiControl.ViewModels;

namespace CfAoiControl.Views;

public partial class ZoneParamEditorView : UserControl
{
    public ZoneParamEditorView()
    {
        AvaloniaXamlLoader.Load(this);
        DataContextChanged += (_, _) => WirePatternPicker();
    }

    // 注入對位 Mark 樣板的檔案選擇（需 TopLevel/StorageProvider），保持 ViewModel 平台無關。
    private void WirePatternPicker()
    {
        if (DataContext is not ZoneParamEditorViewModel vm) return;
        vm.PatternPicker = async () =>
        {
            var top = TopLevel.GetTopLevel(this);
            if (top is null) return null;
            var files = await top.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
            {
                Title = "選擇對位 Mark 樣板", AllowMultiple = false,
                FileTypeFilter = new[]
                {
                    new FilePickerFileType("Mark 樣板") { Patterns = new[] { "*.tif", "*.tiff", "*.png", "*.bmp", "*.jpg", "*.xml" } }
                },
            });
            return files.Count > 0 ? files[0].TryGetLocalPath() : null;
        };
    }
}
