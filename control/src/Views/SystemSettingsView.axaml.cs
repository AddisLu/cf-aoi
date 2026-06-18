using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Markup.Xaml;

namespace CfAoiControl.Views;

public partial class SystemSettingsView : UserControl
{
    public SystemSettingsView() => AvaloniaXamlLoader.Load(this);

    // 點宣告槽 chip → 設 SelectedSlot（master→detail：MainWindowVM 訂閱 SelectedSlot 進單 CCD 設定頁）。
    // 純導覽選取，不改宣告/偵測 section 語意、不 merge（約束②）。
    private void OnSlotTapped(object? sender, TappedEventArgs e)
    {
        if (sender is Control { DataContext: Models.CcdSlotModel slot }
            && DataContext is ViewModels.SystemSettingsViewModel vm)
            vm.SelectSlotCommand.Execute(slot);
    }
}
