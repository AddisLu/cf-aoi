using System;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Markup.Xaml;
using Avalonia.Platform.Storage;
using CfAoiControl.ViewModels;

namespace CfAoiControl.Views;

public partial class SingleCcdSetupView : UserControl
{
    public SingleCcdSetupView()
    {
        AvaloniaXamlLoader.Load(this);
        DataContextChanged += (_, _) => Wire();
    }

    // 注入兩個檔案選擇（需 TopLevel/StorageProvider）到嵌入的 VM：影像(Step1) + 對位樣板(ZoneEditor)。
    private void Wire()
    {
        if (DataContext is not SingleCcdSetupViewModel vm) return;
        vm.Step1.FilePicker = () => Pick("選擇影像", new[] { "*.tif", "*.tiff", "*.png", "*.bmp", "*.jpg" });
        vm.Step1.RemoteImagePicker = () => RemoteImagePickerHelper.OpenAsync(this, vm.Step1.Services);
        vm.ZoneEditor.PatternPicker = () => Pick("選擇對位 Mark 樣板", new[] { "*.tif", "*.tiff", "*.png", "*.bmp", "*.jpg", "*.xml" });
    }

    private async Task<string?> Pick(string title, string[] patterns)
    {
        var top = TopLevel.GetTopLevel(this);
        if (top is null) return null;
        var files = await top.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = title, AllowMultiple = false,
            FileTypeFilter = new[] { new FilePickerFileType("檔案") { Patterns = patterns } },
        });
        return files.Count > 0 ? files[0].TryGetLocalPath() : null;
    }
}
