using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Markup.Xaml;
using CfAoiControl.ViewModels;

namespace CfAoiControl.Views;

public partial class DefectSortView : UserControl
{
    public DefectSortView()
    {
        AvaloniaXamlLoader.Load(this);

        // 雙擊資料夾列 → 進入小圖人工分類檢視
        if (this.FindControl<DataGrid>("FolderGrid") is { } grid)
            grid.DoubleTapped += async (_, _) =>
            {
                if (DataContext is DefectSortViewModel vm && grid.SelectedItem is SortFolderItem f)
                    await vm.OpenFolderAsync(f.FolderName);
            };

        // 鍵盤快速分類：T=TrueDefect、P=Particle、←→ 切換選中小圖
        KeyDown += OnKeyDown;
    }

    private void OnKeyDown(object? sender, KeyEventArgs e)
    {
        if (DataContext is not DefectSortViewModel vm || !vm.InPatchView) return;
        switch (e.Key)
        {
            case Key.T: vm.ClassifySelected("TrueDefect"); e.Handled = true; break;
            case Key.P: vm.ClassifySelected("Particle");   e.Handled = true; break;
            case Key.Left:  vm.MoveSelection(-1); e.Handled = true; break;
            case Key.Right: vm.MoveSelection(1);  e.Handled = true; break;
        }
    }
}
