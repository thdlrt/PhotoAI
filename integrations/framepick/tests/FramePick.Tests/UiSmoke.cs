using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using FramePick.Models;
using FramePick.Services;
using FramePick.ViewModels;

namespace FramePick.Tests;

internal static class UiSmoke
{
    private static int _checks;
    public static void Run(string sourceFolder, string outputDirectory, bool inspect = false)
    {
        Directory.CreateDirectory(outputDirectory);
        RenderOptions.ProcessRenderMode = RenderMode.SoftwareOnly;
        var app = new FramePick.App();
        app.InitializeComponent();
        app.Properties["UiSmoke"] = true;
        var window = new FramePick.MainWindow { ShowActivated = false };
        window.Loaded += async (_, _) =>
        {
            try
            {
                var vm = (MainViewModel)window.DataContext;
                Check(!vm.HasProject && !vm.CanImport && vm.Categories.Count == 0, "未打开项目时禁用导入和分类");
                Capture(window, Path.Combine(outputDirectory, "ui-welcome.png"));
                var projectA = Path.Combine(outputDirectory, "旅行整理.framepick");
                vm.CreateProject(projectA);
                Check(vm.HasProject && vm.CanImport && vm.TotalCount == 0, "创建空项目后允许导入");
                await vm.ImportFolderAsync(sourceFolder);
                await Task.Delay(150);
                Check(ReadableTitle(window, vm.ProjectName), "主窗口标题具有可读的浅色前景");
                Check(vm.VisibleVideos.Count == 4 && vm.VisibleVideos.All(v => v.HasPreview), "图库显示全部有效预览");
                Capture(window, Path.Combine(outputDirectory, "ui-gallery.png"));

                var category = new CategoryDefinition { Name = "精选片段", Destination = Path.Combine(outputDirectory, "selected") };
                var second = new CategoryDefinition { Name = "待剪辑", Destination = Path.Combine(outputDirectory, "editing") };
                vm.ApplyCategories(new[] { category, second }, 2);
                var first = vm.VisibleVideos[0];
                first.CategoryId = category.Id;
                await Task.Delay(80);
                Check(vm.MoveCount == 1 && first.CategoryName == category.Name, "分类决定与汇总同步");
                category.Name = "精选收藏";
                vm.ApplyCategories(new[] { category, second }, 3);
                await Task.Delay(100);
                Check(first.CategoryId == category.Id && vm.MoveCount == 1 && first.CategoryName == "精选收藏", "更新设置保留现有分类决定");

                vm.SearchText = first.FileName.ToUpperInvariant();
                Check(vm.VisibleVideos.Count == 1, "搜索不区分大小写");
                vm.SearchText = "no-such-video";
                Check(vm.ShowNoResults, "无匹配结果提示");
                vm.SearchText = "";
                vm.SelectedFilter = vm.FilterOptions.First(f => f.Key == "uncategorized");
                Check(vm.VisibleVideos.Count == 3, "未分类筛选");
                var detail = vm.VisibleVideos[0];
                vm.OpenDetailCommand.Execute(detail);
                await Task.Delay(150);
                Check(detail.DetailImage is not null, "详情图片加载");
                vm.ToggleDeleteCommand.Execute(detail);
                await Task.Delay(100);
                Check(detail.IsMarkedForDeletion && vm.DeleteCount == 1, "详情标记删除");
                Check(vm.NextDetailCommand.CanExecute(null), "筛选中标记后仍能连续浏览");
                Capture(window, Path.Combine(outputDirectory, "ui-detail.png"));
                vm.NextDetailCommand.Execute(null);
                Check(vm.SelectedVideo != detail, "切换下一条");
                vm.CloseDetailCommand.Execute(null);
                vm.FlushWorkspace();
                var saved = ProjectStore.Load(vm.ProjectFilePath);
                Check(saved.Decisions.Count == 2, "分类与删除决定持久化");
                await vm.ImportFolderAsync(sourceFolder);
                Check(vm.MoveCount == 1 && vm.DeleteCount == 1, "重新导入恢复决定");
                Check(vm.ImportedFolders.Count == 1 && vm.TotalCount == 4, "重复导入同一目录不产生重复卡片");
                vm.SelectedFilter = vm.FilterOptions.First();
                window.Width = 1080;
                window.Height = 720;
                await Task.Delay(150);
                Capture(window, Path.Combine(outputDirectory, "ui-gallery-compact.png"));
                window.Width = 1440;
                window.Height = 920;
                await TestCategoryDialogAsync(window, vm, outputDirectory);

                var projectB = Path.Combine(outputDirectory, "另一个项目.framepick");
                vm.CreateProject(projectB);
                Check(vm.Categories.Count == 0 && vm.ImportedFolders.Count == 0 && vm.TotalCount == 0, "新项目不继承上个项目的分类和文件夹");
                await vm.ImportFolderAsync(sourceFolder);
                Check(vm.MoveCount == 0 && vm.DeleteCount == 0, "同一素材在另一个项目中没有旧决定");
                var categoryB = new CategoryDefinition { Name = "另一个项目分类", Destination = Path.Combine(outputDirectory, "project-b-output") };
                vm.ApplyCategories(new[] { categoryB }, 1);
                vm.VisibleVideos[0].CategoryId = categoryB.Id;
                vm.FlushWorkspace();
                await vm.OpenProjectAsync(projectA);
                Check(vm.Categories.Count == 4 && vm.Categories.All(c => c.Id != categoryB.Id) && vm.MoveCount == 1 && vm.DeleteCount == 1, "打开项目A恢复A的分类与进度");
                await vm.OpenProjectAsync(projectB);
                Check(vm.Categories.Count == 1 && vm.Categories[0].Id == categoryB.Id && vm.MoveCount == 1 && vm.DeleteCount == 0, "打开项目B恢复B的独立进度");
                var offline = ProjectStore.Load(projectB);
                vm.CreateProject(Path.Combine(outputDirectory, "切换中转.framepick"));
                offline.SourceFolders = new() { Path.Combine(outputDirectory, "offline-folder") };
                ProjectStore.Save(projectB, offline);
                await vm.OpenProjectAsync(projectB);
                Check(vm.TotalCount == 0 && ProjectStore.Load(projectB).Decisions.Count == 1 && vm.StatusText.Contains("不可访问"), "目录离线时保留决定并提示");
                vm.CreateProject(Path.Combine(outputDirectory, "恢复中转.framepick"));
                offline.SourceFolders = new() { sourceFolder };
                ProjectStore.Save(projectB, offline);
                await vm.OpenProjectAsync(projectB);
                Check(vm.MoveCount == 1, "离线目录恢复后仍能恢复项目决定");
                var damaged = Path.Combine(outputDirectory, "damaged.framepick");
                File.WriteAllText(damaged, "invalid");
                try { await vm.OpenProjectAsync(damaged); throw new InvalidOperationException("损坏项目不应打开"); }
                catch (InvalidDataException) { Check(vm.ProjectFilePath == projectB && vm.MoveCount == 1, "损坏项目不替换当前项目"); }
                await vm.OpenProjectAsync(projectA);
                await vm.ImportFolderAsync(Path.Combine(sourceFolder, "nested"));
                Check(vm.ImportedFolders.Count == 2 && vm.TotalCount == 4, "重叠目录不重复添加视频");

                var paged = Path.Combine(outputDirectory, "paged-input");
                Directory.CreateDirectory(paged);
                var template = Directory.GetFiles(sourceFolder, "*.mp4", SearchOption.AllDirectories).First();
                for (var i = 0; i < 40; i++) File.Copy(template, Path.Combine(paged, $"clip-{i:00}.mp4"), true);
                var import = vm.ImportFolderAsync(paged);
                for (var i = 0; i < 500 && vm.TotalCount < 40; i++) await Task.Delay(10);
                Check(vm.IsBusy && vm.NextPageCommand.CanExecute(null), "预览生成中允许翻页");
                vm.NextPageCommand.Execute(null);
                Check(vm.PageIndex == 1 && vm.VisibleVideos.Count == 8 && vm.TotalCount == 44, "第二个文件夹累计导入且生成中可翻页");
                vm.Cancel();
                await import;
                Check(!vm.IsBusy && vm.VisibleVideos.All(v => !v.IsPreviewLoading), "取消后清除加载状态");
                await vm.RetryPreviewsAsync();
                Check(vm.VisibleVideos.All(v => v.HasPreview), "取消后继续生成预览");
                vm.PreviousPageCommand.Execute(null);
                vm.VisibleVideos[0].CategoryId = category.Id;
                await Task.Delay(100);
                Capture(window, Path.Combine(outputDirectory, "ui-gallery-full.png"));
                Check(ProjectStore.Load(projectA).SourceFolders.Count == 3, "项目记录所有逐次导入的文件夹");
                if (!inspect) window.Close();
                File.WriteAllText(Path.Combine(outputDirectory, "result.txt"), $"UI PASS: {_checks} 项检查");
                Console.WriteLine($"UI PASS: {_checks} 项检查");
            }
            catch (Exception ex)
            {
                File.WriteAllText(Path.Combine(outputDirectory, "result.txt"), ex.ToString());
                Console.Error.WriteLine(ex);
                app.Shutdown(1);
            }
        };
        Environment.ExitCode = app.Run(window);
    }

    private static async Task TestCategoryDialogAsync(Window owner, MainViewModel vm, string output)
    {
        var dialog = new FramePick.CategoryManagerWindow(vm.Categories, vm.PreviewConcurrency) { Owner = owner, ShowActivated = false };
        dialog.Loaded += async (_, _) =>
        {
            try
            {
                var add = (Button)dialog.FindName("AddCategoryButton");
                for (var i = 0; i < 12; i++) add.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                await Task.Delay(120);
                dialog.UpdateLayout();
                Check(ReadableTitle(dialog, "管理你的分类"), "分类窗口标题具有可读的浅色前景");
                var categories = dialog.CategoriesResult.ToList();
                Check(categories.Count == 14, "连续添加12个分类无异常");
                var inputs = Descendants<TextBox>(dialog).ToList();
                Check(inputs.Count == 28, "所有分类编辑控件均能创建");
                categories[2].Name = "旅行 片段";
                categories[2].Destination = Path.Combine(output, "travel");
                var save = (Button)dialog.FindName("SaveCategoriesButton");
                categories[3].Name = "缺少地址";
                save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                Check(((TextBlock)dialog.FindName("ValidationMessage")).Text.Length > 0, "缺失路径使用内联提示");
                categories[3].Destination = Path.Combine(output, "inbox");
                Capture(dialog, Path.Combine(output, "ui-categories.png"));
                save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            }
            catch { dialog.Close(); throw; }
        };
        Check(dialog.ShowDialog() == true, "分类可以保存并关闭");
        vm.ApplyCategories(dialog.CategoriesResult, dialog.PreviewConcurrency);
        var reopened = new FramePick.CategoryManagerWindow(vm.Categories, vm.PreviewConcurrency) { Owner = owner };
        reopened.Show();
        await Task.Delay(80);
        Check(reopened.CategoriesResult.Count() == 4, "保存后重新打开分类数量正确");
        reopened.Close();
    }

    private static IEnumerable<T> Descendants<T>(DependencyObject root) where T : DependencyObject
    {
        for (var i = 0; i < VisualTreeHelper.GetChildrenCount(root); i++)
        {
            var child = VisualTreeHelper.GetChild(root, i);
            if (child is T match) yield return match;
            foreach (var nested in Descendants<T>(child)) yield return nested;
        }
    }

    private static void Check(bool condition, string name)
    {
        if (!condition) throw new InvalidOperationException("FAIL: " + name);
        _checks++;
        Console.WriteLine("OK: " + name);
    }

    private static bool ReadableTitle(Window window, string title)
    {
        var label = Descendants<TextBlock>(window).First(t => t.Text == title);
        return label.Foreground is SolidColorBrush brush && brush.Color.R > 200 && brush.Color.G > 200 && brush.Color.B > 200;
    }

    private static void Capture(Window window, string path)
    {
        window.UpdateLayout();
        var width = (int)Math.Ceiling(window.ActualWidth);
        var height = (int)Math.Ceiling(window.ActualHeight);
        var bitmap = new RenderTargetBitmap(width, height, 96, 96, PixelFormats.Pbgra32);
        bitmap.Render(window);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bitmap));
        using var stream = File.Create(path);
        encoder.Save(stream);
    }
}
