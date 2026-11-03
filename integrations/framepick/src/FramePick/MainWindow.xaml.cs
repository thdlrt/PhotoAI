using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using FramePick.ViewModels;
using Microsoft.Win32;

namespace FramePick;

public partial class MainWindow : Window
{
    private readonly MainViewModel _viewModel = new();

    public MainWindow()
    {
        InitializeComponent();
        FramePick.Services.WindowTheme.Attach(this);
        DataContext = _viewModel;
    }

    private void NewProject_Click(object sender, RoutedEventArgs e)
    {
        if (!_viewModel.CanChangeProject) return;
        var dialog = new SaveFileDialog
        {
            Title = "创建 FramePick 项目", Filter = "FramePick 项目 (*.framepick)|*.framepick",
            DefaultExt = ".framepick", AddExtension = true, FileName = "新项目.framepick",
            InitialDirectory = _viewModel.InitialProjectDirectory, OverwritePrompt = false
        };
        if (dialog.ShowDialog(this) != true) return;
        if (File.Exists(dialog.FileName))
        {
            MessageBox.Show(this, "此项目已存在，请使用“打开项目”，或选择其他名称。", "创建项目", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        try { _viewModel.CreateProject(dialog.FileName); }
        catch (Exception ex) { MessageBox.Show(this, ex.Message, "创建项目失败", MessageBoxButton.OK, MessageBoxImage.Error); }
    }

    private async void OpenProject_Click(object sender, RoutedEventArgs e)
    {
        if (!_viewModel.CanChangeProject) return;
        var dialog = new OpenFileDialog
        {
            Title = "打开 FramePick 项目", Filter = "FramePick 项目 (*.framepick)|*.framepick",
            Multiselect = false, InitialDirectory = _viewModel.InitialProjectDirectory,
            FileName = File.Exists(_viewModel.LastProjectPath) ? _viewModel.LastProjectPath : string.Empty
        };
        if (dialog.ShowDialog(this) != true) return;
        try { await _viewModel.OpenProjectAsync(dialog.FileName); }
        catch (Exception ex) { MessageBox.Show(this, ex.Message, "打开项目失败", MessageBoxButton.OK, MessageBoxImage.Error); }
    }

    private async void ImportLegacy_Click(object sender, RoutedEventArgs e)
    {
        try { await _viewModel.ImportLegacySettingsAsync(); }
        catch (Exception ex) { MessageBox.Show(this, ex.Message, "导入旧版设置失败", MessageBoxButton.OK, MessageBoxImage.Error); }
    }

    private async void Import_Click(object sender, RoutedEventArgs e)
    {
        if (!_viewModel.CanImport) return;
        var dialog = new OpenFolderDialog
        {
            Title = "选择视频文件夹",
            Multiselect = false,
            InitialDirectory = Directory.Exists(_viewModel.InitialFolder) ? _viewModel.InitialFolder : string.Empty
        };
        if (dialog.ShowDialog(this) != true) return;
        try
        {
            await _viewModel.ImportFolderAsync(dialog.FolderName);
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "帧选", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void Categories_Click(object sender, RoutedEventArgs e)
    {
        if (!_viewModel.CanManageCategories) return;
        var dialog = new CategoryManagerWindow(_viewModel.Categories, _viewModel.PreviewConcurrency) { Owner = this };
        if (dialog.ShowDialog() == true)
        {
            try { _viewModel.ApplyCategories(dialog.CategoriesResult, dialog.PreviewConcurrency); }
            catch (Exception ex) { MessageBox.Show(this, ex.Message, "设置保存失败", MessageBoxButton.OK, MessageBoxImage.Error); }
        }
    }

    private async void Execute_Click(object sender, RoutedEventArgs e)
    {
        if (!_viewModel.CanExecute) return;
        var answer = MessageBox.Show(this,
            $"即将移动 {_viewModel.MoveCount} 个视频到对应分类目录，\n将 {_viewModel.DeleteCount} 个视频送入回收站。\n\n重名文件自动编号，不覆盖已有文件。是否执行？",
            "执行", MessageBoxButton.OKCancel, MessageBoxImage.Warning);
        if (answer != MessageBoxResult.OK) return;
        try
        {
            var result = await _viewModel.ExecuteAsync();
            var message = $"{(result.Cancelled ? "已停止，保留已完成结果。\n" : "")}已移动 {result.Moved}，已送入回收站 {result.Deleted}";
            if (result.Failed > 0)
            {
                message += $"，失败 {result.Failed}";
                if (result.Errors.Count > 0) message += "\n\n" + string.Join("\n", result.Errors.Take(3));
            }
            MessageBox.Show(this, message, result.Cancelled ? "已停止" : "整理完成", MessageBoxButton.OK,
                result.Failed > 0 ? MessageBoxImage.Warning : MessageBoxImage.Information);
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "执行失败", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void Cancel_Click(object sender, RoutedEventArgs e) => _viewModel.Cancel();

    private async void Retry_Click(object sender, RoutedEventArgs e)
    {
        try { await _viewModel.RetryPreviewsAsync(); }
        catch (Exception ex) { MessageBox.Show(this, ex.Message, "预览失败", MessageBoxButton.OK, MessageBoxImage.Error); }
    }

    private void ResetFilter_Click(object sender, RoutedEventArgs e)
    {
        _viewModel.SearchText = string.Empty;
        _viewModel.SelectedFilter = _viewModel.FilterOptions.First();
    }

    private void Page_Click(object sender, RoutedEventArgs e) => GalleryScrollViewer.ScrollToTop();

    private void Window_DragOver(object sender, DragEventArgs e)
    {
        e.Effects = _viewModel.CanImport && e.Data.GetData(DataFormats.FileDrop) is string[] { Length: 1 } paths && Directory.Exists(paths[0])
            ? DragDropEffects.Copy : DragDropEffects.None;
        e.Handled = true;
    }

    private async void Window_Drop(object sender, DragEventArgs e)
    {
        if (!_viewModel.CanImport || e.Data.GetData(DataFormats.FileDrop) is not string[] paths) return;
        if (paths.Length != 1 || !Directory.Exists(paths[0]))
        {
            MessageBox.Show(this, "每次请拖入一个文件夹。", "导入文件夹", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        var folder = paths[0];
        try { await _viewModel.ImportFolderAsync(folder); }
        catch (Exception ex) { MessageBox.Show(this, ex.Message, "导入失败", MessageBoxButton.OK, MessageBoxImage.Error); }
    }

    private void Window_KeyDown(object sender, KeyEventArgs e)
    {
        if (Keyboard.Modifiers == ModifierKeys.Control)
        {
            if (e.Key == Key.N) { NewProject_Click(sender, e); e.Handled = true; return; }
            if (e.Key == Key.O) { OpenProject_Click(sender, e); e.Handled = true; return; }
            if (e.Key == Key.I) { Import_Click(sender, e); e.Handled = true; return; }
        }
        if (e.Key == Key.F && Keyboard.Modifiers == ModifierKeys.Control) { SearchBox.Focus(); e.Handled = true; return; }
        if (!_viewModel.IsDetailOpen || Keyboard.FocusedElement is TextBox || IsComboBoxEditing(Keyboard.FocusedElement)) return;
        if (e.Key == Key.Escape) _viewModel.CloseDetailCommand.Execute(null);
        else if (e.Key == Key.Left && _viewModel.PreviousDetailCommand.CanExecute(null)) _viewModel.PreviousDetailCommand.Execute(null);
        else if (e.Key == Key.Right && _viewModel.NextDetailCommand.CanExecute(null)) _viewModel.NextDetailCommand.Execute(null);
        else if (e.Key == Key.Delete && _viewModel.SelectedVideo is not null) _viewModel.ToggleDeleteCommand.Execute(_viewModel.SelectedVideo);
        else return;
        e.Handled = true;
    }

    private static bool IsComboBoxEditing(IInputElement? element)
    {
        var obj = element as DependencyObject;
        while (obj is not null)
        {
            if (obj is ComboBox) return true;
            obj = obj is System.Windows.Media.Visual ? System.Windows.Media.VisualTreeHelper.GetParent(obj) : LogicalTreeHelper.GetParent(obj);
        }
        return false;
    }

    private void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_viewModel.IsExecuting)
        {
            _viewModel.Cancel();
            e.Cancel = true;
            return;
        }
        _viewModel.Cancel();
        try { _viewModel.FlushWorkspace(throwOnError: true); }
        catch (Exception ex)
        {
            e.Cancel = true;
            MessageBox.Show(this, $"项目尚未保存，窗口将保持打开。\n{ex.Message}", "项目保存失败", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }
}
