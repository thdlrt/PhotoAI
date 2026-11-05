using System.Collections.ObjectModel;
using System.IO;
using System.Windows;
using FramePick.Models;
using Microsoft.Win32;

namespace FramePick;

public partial class CategoryManagerWindow : Window
{
    private readonly ObservableCollection<CategoryDefinition> _categories;

    public CategoryManagerWindow(IEnumerable<CategoryDefinition> categories, int previewConcurrency, string projectName = "项目", int previewFrameCount = 16)
    {
        InitializeComponent();
        FramePick.Services.WindowTheme.Attach(this);
        _categories = new ObservableCollection<CategoryDefinition>(categories.Select(c => c.Clone()));
        PreviewConcurrency = Math.Clamp(previewConcurrency, 1, 4);
        ProjectName = projectName;
        PreviewFrameCount = previewFrameCount;
        CategoryList.ItemsSource = _categories;
        DataContext = this;
    }

    public int PreviewConcurrency { get; set; }
    public string ProjectName { get; set; }
    public int PreviewFrameCount { get; set; }
    public IEnumerable<CategoryDefinition> CategoriesResult => _categories;

    private void Add_Click(object sender, RoutedEventArgs e)
    {
        ValidationMessage.Text = string.Empty;
        var category = new CategoryDefinition();
        _categories.Add(category);
        Dispatcher.BeginInvoke(System.Windows.Threading.DispatcherPriority.Loaded, new Action(() =>
        {
            CategoryList.UpdateLayout();
            if (CategoryList.ItemContainerGenerator.ContainerFromItem(category) is FrameworkElement row)
            {
                row.BringIntoView();
                FindTextBox(row)?.Focus();
            }
        }));
    }

    private static System.Windows.Controls.TextBox? FindTextBox(DependencyObject parent)
    {
        for (var i = 0; i < System.Windows.Media.VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = System.Windows.Media.VisualTreeHelper.GetChild(parent, i);
            if (child is System.Windows.Controls.TextBox input) return input;
            if (FindTextBox(child) is { } nested) return nested;
        }
        return null;
    }

    private void Browse_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.Tag is not CategoryDefinition category) return;
        var dialog = new OpenFolderDialog
        {
            Title = category.Name.Length > 0 ? category.Name : "目标文件夹",
            Multiselect = false,
            InitialDirectory = Directory.Exists(category.Destination) ? category.Destination : string.Empty
        };
        if (dialog.ShowDialog(this) == true) category.Destination = dialog.FolderName;
    }

    private void Remove_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.Tag is CategoryDefinition category) _categories.Remove(category);
    }

    private void Save_Click(object sender, RoutedEventArgs e)
    {
        ProjectName = ProjectName.Trim();
        if (ProjectName.Length == 0)
        {
            ValidationMessage.Text = "请输入项目名称。";
            ProjectNameInput.Focus();
            return;
        }
        if (PreviewFrameCount is not (16 or 25))
        {
            ValidationMessage.Text = "请选择 16 或 25 帧预览。";
            return;
        }
        foreach (var category in _categories)
        {
            category.Name = category.Name.Trim();
            category.Destination = category.Destination.Trim();
        }
        foreach (var empty in _categories.Where(c => c.Name.Length == 0 && c.Destination.Length == 0).ToList())
            _categories.Remove(empty);
        if (_categories.Any(c => c.Name.Length == 0 || c.Destination.Length == 0))
        {
            ValidationMessage.Text = "请为每个分类填写名称与目标文件夹。";
            return;
        }
        if (_categories.GroupBy(c => c.Name, StringComparer.CurrentCultureIgnoreCase).Any(g => g.Count() > 1))
        {
            ValidationMessage.Text = "分类名称重复，请使用不同的名称。";
            return;
        }
        if (_categories.Any(c => !IsValidDestination(c.Destination)))
        {
            ValidationMessage.Text = "请输入完整的目标文件夹路径，或点击文件夹图标选择。";
            return;
        }
        DialogResult = true;
    }

    private static bool IsValidDestination(string path)
    {
        try
        {
            return Path.IsPathFullyQualified(path) && path.IndexOfAny(Path.GetInvalidPathChars()) < 0
                && !File.Exists(Path.GetFullPath(path));
        }
        catch { return false; }
    }

    private void Cancel_Click(object sender, RoutedEventArgs e) => DialogResult = false;
}
