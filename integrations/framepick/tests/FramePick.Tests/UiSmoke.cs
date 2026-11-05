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
    private static bool _headless;
    public static void Run(string sourceFolder, string outputDirectory, bool inspect = false, bool headless = false)
    {
        _headless = headless;
        Directory.CreateDirectory(outputDirectory);
        RenderOptions.ProcessRenderMode = RenderMode.SoftwareOnly;
        var app = new FramePick.App();
        app.InitializeComponent();
        app.Properties["UiSmoke"] = true;
        var window = new FramePick.MainWindow { ShowActivated = false };
        if (headless) { window.WindowStartupLocation = WindowStartupLocation.Manual; window.Left = -10000; window.Top = -10000; }
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
                await TestRegenerationAsync(window, vm, template, outputDirectory);
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
        var dialog = new FramePick.CategoryManagerWindow(vm.Categories, vm.PreviewConcurrency, vm.ProjectName, vm.PreviewFrameCount) { Owner = owner, ShowActivated = false };
        dialog.Loaded += async (_, _) =>
        {
            try
            {
                var add = (Button)dialog.FindName("AddCategoryButton");
                for (var i = 0; i < 12; i++) add.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                await Task.Delay(120);
                dialog.UpdateLayout();
                Check(ReadableTitle(dialog, "项目设置"), "项目设置标题具有可读的浅色前景");
                var categories = dialog.CategoriesResult.ToList();
                Check(categories.Count == 14, "连续添加12个分类无异常");
                var inputs = Descendants<TextBox>(dialog).ToList();
                Check(inputs.Count == 29, "所有项目与分类编辑控件均能创建");
                var name = (TextBox)dialog.FindName("ProjectNameInput");
                name.Text = "   ";
                var save = (Button)dialog.FindName("SaveCategoriesButton");
                save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                Check(((TextBlock)dialog.FindName("ValidationMessage")).Text == "请输入项目名称。", "项目空名称使用内联提示");
                name.Text = "旅行整理·改名";
                var frameCount = (ComboBox)dialog.FindName("PreviewFrameCountInput");
                frameCount.SelectedValue = 25;
                Check(dialog.PreviewFrameCount == 25, "项目设置可选择25帧");
                frameCount.SelectedValue = 16;
                categories[2].Name = "旅行 片段";
                categories[2].Destination = Path.Combine(output, "travel");
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
        vm.ApplyProjectSettings(dialog.ProjectName, dialog.CategoriesResult, dialog.PreviewConcurrency, dialog.PreviewFrameCount);
        Check(vm.ProjectName == "旅行整理·改名" && owner.Title.Contains(vm.ProjectName) && ProjectStore.Load(vm.ProjectFilePath).Name == vm.ProjectName, "修改名称同步窗口标题和项目文件");
        var reopened = new FramePick.CategoryManagerWindow(vm.Categories, vm.PreviewConcurrency, vm.ProjectName, vm.PreviewFrameCount) { Owner = owner };
        reopened.Show();
        await Task.Delay(80);
        Check(reopened.CategoriesResult.Count() == 4, "保存后重新打开分类数量正确");
        reopened.Close();
    }

    private static async Task TestRegenerationAsync(FramePick.MainWindow window, MainViewModel vm, string template, string output)
    {
        var root = Path.Combine(output, "refresh-input");
        Directory.CreateDirectory(root);
        var keep = Path.Combine(root, "keep.mp4");
        var removed = Path.Combine(root, "removed.mp4");
        var changed = Path.Combine(root, "changed.mp4");
        foreach (var path in new[] { keep, removed, changed }) File.Copy(template, path);
        var projectPath = Path.Combine(output, "refresh.framepick");
        vm.CreateProject(projectPath);
        await vm.ImportFolderAsync(root);
        Check(vm.CanGeneratePreviews && vm.VisibleVideos.All(v => v.HasPreview), "所有预览就绪仍可重新扫描生成");
        var category = new CategoryDefinition { Name = "保留分类", Destination = Path.Combine(output, "kept") };
        vm.ApplyCategories(new[] { category }, 2);
        vm.VisibleVideos.Single(v => v.FullPath == keep).CategoryId = category.Id;
        vm.VisibleVideos.Single(v => v.FullPath == changed).IsMarkedForDeletion = true;
        vm.FlushWorkspace(true);
        var oldBytes = File.ReadAllBytes(projectPath);
        await vm.OpenProjectAsync(projectPath);
        vm.FlushWorkspace(true);
        Check(File.ReadAllBytes(projectPath).SequenceEqual(oldBytes), "打开未修改的旧项目不重写文件");
        var cachePath = vm.VisibleVideos.Single(v => v.FullPath == keep).PreviewPath;
        var sentinel = new DateTime(2001, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        File.SetLastWriteTimeUtc(cachePath, sentinel);
        File.Delete(removed);
        File.Move(changed, Path.Combine(root, "renamed.mp4"));
        var added = Path.Combine(root, "new.mp4");
        File.Copy(template, added);
        await RunRegenerateMenuAsync(window, vm, false);
        Check(vm.TotalCount == 3 && vm.VisibleVideos.Any(v => v.FullPath == added) && vm.VisibleVideos.All(v => v.FullPath != removed && v.FullPath != changed), "只补充扫描新增、删除和重命名文件");
        Check(File.GetLastWriteTimeUtc(cachePath) == sentinel && vm.MoveCount == 1, "只补充保留已有缓存与分类");
        var current = vm.VisibleVideos.Single(v => v.FullPath == keep);
        File.SetLastWriteTimeUtc(keep, File.GetLastWriteTimeUtc(keep).AddMinutes(1));
        await vm.RegeneratePreviewsAsync(false);
        Check(vm.VisibleVideos.Single(v => v.FullPath == keep).PreviewPath != current.PreviewPath && vm.MoveCount == 0, "源文件变更刷新缓存且不沿用旧文件决定");
        vm.VisibleVideos.Single(v => v.FullPath == added).CategoryId = category.Id;
        vm.VisibleVideos.Single(v => v.FullPath.EndsWith("renamed.mp4")).IsMarkedForDeletion = true;
        var framePaths = vm.VisibleVideos.Select(v => v.PreviewPath).ToList();
        foreach (var path in framePaths) File.SetLastWriteTimeUtc(path, sentinel);
        File.Copy(template, Path.Combine(root, "all-new.mp4"));
        await RunRegenerateMenuAsync(window, vm, true);
        Check(vm.TotalCount == 4 && framePaths.All(p => File.GetLastWriteTimeUtc(p) > sentinel), "全部重新生成同时扫描并重做有效缓存");
        Check(vm.MoveCount == 1 && vm.DeleteCount == 1, "全部重新生成保留分类与删除决定");
        vm.ApplyProjectSettings("25帧项目", vm.Categories, 2, 25);
        Check(vm.VisibleVideos.All(v => !v.HasPreview) && vm.PreviewFrameCount == 25 && vm.MoveCount == 1 && vm.DeleteCount == 1, "修改帧数清除旧图且保留整理决定");
        await RunRegenerateMenuAsync(window, vm, false);
        using (var stream = File.OpenRead(vm.VisibleVideos.First().PreviewPath))
        {
            var bitmap = BitmapFrame.Create(stream, BitmapCreateOptions.None, BitmapCacheOption.OnLoad);
            Check(bitmap.PixelWidth == 1600 && bitmap.PixelHeight == 1000, "通过重新生成菜单得到实际25帧拼图");
        }
        Capture(window, Path.Combine(output, "ui-gallery-25.png"));
        vm.CreateProject(Path.Combine(output, "refresh-other.framepick"));
        await vm.OpenProjectAsync(projectPath);
        Check(vm.ProjectName == "25帧项目" && vm.PreviewFrameCount == 25 && vm.MoveCount == 1 && vm.DeleteCount == 1, "重开项目恢复名称帧数与决定");
        vm.CreateProject(Path.Combine(output, "legacy-open.framepick"));
        var legacyPath = Path.Combine(output, "legacy-framecount.framepick");
        File.WriteAllText(legacyPath, "{\"Format\":\"FramePick.Project\",\"Version\":1,\"Id\":\"old\",\"Name\":\"旧项目\"}");
        var legacyBytes = File.ReadAllBytes(legacyPath);
        await vm.OpenProjectAsync(legacyPath);
        vm.FlushWorkspace(true);
        Check(vm.PreviewFrameCount == 16 && File.ReadAllBytes(legacyPath).SequenceEqual(legacyBytes), "缺少帧数字段的旧项目打开保持原文件");
        await vm.ImportFolderAsync(Path.Combine(output, "refresh-input"));
        foreach (var file in Directory.GetFiles(root)) File.Delete(file);
        await vm.RegeneratePreviewsAsync(false);
        Check(vm.TotalCount == 0 && vm.CanGeneratePreviews, "文件夹变空后仍可扫描后续新增文件");
        File.Copy(template, Path.Combine(root, "returned.mp4"));
        await RunRegenerateMenuAsync(window, vm, false);
        Check(vm.TotalCount == 1 && vm.VisibleVideos[0].HasPreview, "空项目可用只补充找回新文件");
    }

    private static async Task RunRegenerateMenuAsync(FramePick.MainWindow window, MainViewModel vm, bool all)
    {
        var button = (Button)window.FindName("RegenerateButton");
        button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Check(button.ContextMenu!.IsOpen && button.ContextMenu.Items.Count == 2, "点击重新生成显示两种选项");
        await Task.Delay(40);
        var option = button.ContextMenu.Items.OfType<MenuItem>().Single(i => (string)i.Tag == (all ? "all" : "missing"));
        option.RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
        button.ContextMenu.IsOpen = false;
        for (var i = 0; vm.IsBusy && i < 6000; i++) await Task.Delay(20);
        Check(!vm.IsBusy && vm.VisibleVideos.All(v => v.HasPreview), "菜单操作完成并生成有效预览");
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
        if (_headless) return; // 离屏窗口不产生可用画面；无桌面模式只检查真实控件与流程。
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
