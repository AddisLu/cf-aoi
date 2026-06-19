using System;
using Avalonia.Controls;
using Avalonia.Markup.Xaml;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using CfAoiControl.Controls;
using CfAoiControl.ViewModels;

namespace CfAoiControl.Views;

/// <summary>
/// 離線分析（frmAlgorithmTestTools）。影像/ROI 互動已抽到共用 RoiImageView（行為不變）；
/// 本 View 只保留：檔案選取注入、縮圖牆 ↔ 控制項缺陷選取同步、開窗/刷新時重建 overlay。
/// </summary>
public partial class Step1View : UserControl
{
    private RoiImageView? _roi;
    private ListBox? _thumbList;
    private bool _navigating;   // 防止 thumb↔控制項缺陷選取互設造成迴圈

    public Step1View()
    {
        AvaloniaXamlLoader.Load(this);
        _roi = this.FindControl<RoiImageView>("Roi");
        _thumbList = this.FindControl<ListBox>("ThumbList");
        if (_thumbList != null) _thumbList.SelectionChanged += OnThumbSelectionChanged;
        if (_roi != null) _roi.DefectSelected += ScrollThumbTo;   // 控制項內部選缺陷 → 捲縮圖牆
        DataContextChanged += OnDataContextChanged;
    }

    private Step1ViewModel? Vm => DataContext as Step1ViewModel;

    private void OnDataContextChanged(object? sender, EventArgs e)
    {
        WireFilePicker();
        if (Vm is { } vm)
        {
            vm.RefreshOverlayRequested -= OnRefreshOverlay;
            vm.RefreshOverlayRequested += OnRefreshOverlay;
        }
        // 重新開窗：VM 仍持缺陷 → 控制項從 Defects 重建（Post 等綁定就緒）
        Dispatcher.UIThread.Post(() => _roi?.RefreshOverlay());
    }

    private void OnRefreshOverlay() => _roi?.RefreshOverlay();

    private void OnThumbSelectionChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (_navigating) return;
        if (_thumbList?.SelectedItem is DefectThumb t) _roi?.SelectDefect(t.Index, center: true);
    }

    private void ScrollThumbTo(int idx)
    {
        if (_thumbList is null || Vm is not { } vm) return;
        if (idx >= 0 && idx < vm.Thumbs.Count)
        {
            _navigating = true;
            _thumbList.SelectedItem = vm.Thumbs[idx];
            _thumbList.ScrollIntoView(vm.Thumbs[idx]);
            _navigating = false;
        }
    }

    // 檔案選取（StorageProvider，需 TopLevel）注入 ViewModel，保持 VM 平台無關。
    private void WireFilePicker()
    {
        if (Vm is not { } vm) return;
        vm.FilePicker = async () =>
        {
            var top = TopLevel.GetTopLevel(this);
            if (top is null) return null;
            var files = await top.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
            {
                Title = "選擇影像", AllowMultiple = false,
                FileTypeFilter = new[]
                {
                    new FilePickerFileType("影像") { Patterns = new[] { "*.tif", "*.tiff", "*.png", "*.bmp", "*.jpg" } }
                },
            });
            return files.Count > 0 ? files[0].TryGetLocalPath() : null;
        };
        vm.RemoteImagePicker = () => RemoteImagePickerHelper.OpenAsync(this, vm.Services);
    }
}
