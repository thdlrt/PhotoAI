using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using FramePick.Core;
using FramePick.Models;
using FramePick.Services;

namespace FramePick.Tests;

internal static class Program
{
    private static string TestRoot = string.Empty;
    private static int _passed;

    [STAThread]
    private static int Main(string[] args)
    {
        var testBase = Path.GetFullPath(Environment.GetEnvironmentVariable("FRAMEPICK_TEST_ROOT") ?? Path.Combine(Path.GetTempPath(), "FramePick.Tests"));
        var rootIndex = Array.IndexOf(args, "--test-root");
        if (rootIndex >= 0 && rootIndex + 1 < args.Length) testBase = Path.GetFullPath(args[rootIndex + 1]);
        Directory.CreateDirectory(testBase);
        if (args.Contains("--ui"))
        {
            TestRoot = File.ReadAllText(Path.Combine(testBase, "latest-run.txt")).Trim();
            if (!Path.GetFullPath(TestRoot).StartsWith(testBase + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
                || !Directory.Exists(TestRoot)) throw new InvalidOperationException("请先运行核心测试生成样例。");
        }
        else
        {
            TestRoot = Path.Combine(testBase, "run-" + Guid.NewGuid().ToString("N"));
            File.WriteAllText(Path.Combine(testBase, "latest-run.txt"), TestRoot);
        }
        Environment.SetEnvironmentVariable("FRAMEPICK_DATA_ROOT", Path.Combine(TestRoot, args.Contains("--ui") ? "ui-state-" + DateTime.UtcNow.Ticks : "state"));
        if (args.Contains("--ui"))
        {
            var uiOutput = Path.Combine(TestRoot, "ui-" + DateTime.UtcNow.Ticks);
            File.WriteAllText(Path.Combine(testBase, "latest-ui.txt"), uiOutput);
            UiSmoke.Run(Path.Combine(TestRoot, "input"), uiOutput, args.Contains("--inspect"));
            return Environment.ExitCode;
        }
        return RunAsync(args).GetAwaiter().GetResult();
    }

    private static async Task<int> RunAsync(string[] args)
    {
        if (Path.GetFullPath(TestRoot) != TestRoot) throw new InvalidOperationException("测试目录校验失败。");
        Directory.CreateDirectory(TestRoot);

        try
        {
            var videos = await CreateVideosAsync();
            await TestScannerAsync(videos);
            await TestPreviewsAsync(videos);
            await TestBatchExecutorAsync();
            await TestCancelledBatchAsync();
            await TestInvalidPreviewAsync(videos[0]);
            await TestShortPreviewAsync();
            await TestProjectsAsync();
            if (args.Contains("--large")) await TestLargeFileAsync(videos[0]);
            Console.WriteLine($"PASS: {_passed} 项检查");
        }
        finally
        {
            var large = Path.Combine(TestRoot, "large-2gb.mp4");
            if (File.Exists(large)) File.Delete(large);
        }
        return 0;
    }

    private static async Task<List<VideoDescriptor>> CreateVideosAsync()
    {
        var specifications = new[]
        {
            (Relative: @"nested\a.mp4", Color: "red"),
            (Relative: @"nested\deeper\b.mov", Color: "green"),
            (Relative: @"c.mkv", Color: "blue"),
            (Relative: @"nested\d.KMV", Color: "yellow")
        };
        var result = new List<VideoDescriptor>();
        foreach (var spec in specifications)
        {
            var path = Path.Combine(TestRoot, "input", spec.Relative);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var format = Path.GetExtension(path).ToLowerInvariant() switch
            {
                ".mkv" => "matroska",
                ".mov" => "mov",
                _ => "mp4"
            };
            await RunFFmpegAsync(new[]
            {
                "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
                "-t", "6", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-f", format, path
            });
            result.Add(new VideoDescriptor(path, new FileInfo(path).Length, File.GetLastWriteTimeUtc(path)));
        }
        await File.WriteAllTextAsync(Path.Combine(TestRoot, "input", "ignore.txt"), "not a video");
        return result;
    }

    private static async Task TestScannerAsync(IReadOnlyList<VideoDescriptor> expected)
    {
        var scanner = new VideoScanner();
        var progressValues = new List<int>();
        var found = await scanner.ScanAsync(Path.Combine(TestRoot, "input"), new Progress<int>(progressValues.Add));
        Assert(found.Count == expected.Count, $"递归扫描应找到 {expected.Count} 个视频，实际 {found.Count}");
        Assert(found.Any(v => v.FullPath.EndsWith(".KMV", StringComparison.OrdinalIgnoreCase)), "KMV 扩展名兼容");
        Assert(!found.Any(v => v.FullPath.EndsWith(".txt")), "忽略非视频文件");
        using var cts = new CancellationTokenSource();
        cts.Cancel();
        await AssertThrows<OperationCanceledException>(() => scanner.ScanAsync(TestRoot, null, cts.Token), "扫描取消");
    }

    private static async Task TestPreviewsAsync(IReadOnlyList<VideoDescriptor> videos)
    {
        var generator = new PreviewGenerator();
        foreach (var video in videos)
        {
            var output = Path.Combine(TestRoot, "previews", Path.GetFileName(video.FullPath) + ".jpg");
            var watch = Stopwatch.StartNew();
            var result = await generator.GenerateAsync(video, output);
            watch.Stop();
            Assert(result.Success && result.FrameCount == 16, $"{Path.GetFileName(video.FullPath)} 生成16帧：{result.Error}");
            Assert(result.DurationSeconds is > 5.8 and < 6.2, "读取正确时长");
            Assert(File.Exists(output) && new FileInfo(output).Length > 10_000, "JPEG 预览非空");
            using (var stream = File.OpenRead(output))
            {
                var frame = BitmapFrame.Create(stream, BitmapCreateOptions.None, BitmapCacheOption.OnLoad);
                Assert(frame.PixelWidth == 1280 && frame.PixelHeight == 800, "预览尺寸1280×800");
                var converted = new FormatConvertedBitmap(frame, PixelFormats.Bgra32, null, 0);
                var pixels = new byte[1280 * 800 * 4];
                converted.CopyPixels(pixels, 1280 * 4, 0);
                var colorfulSamples = 0;
                for (var i = 0; i + 2 < pixels.Length; i += 64)
                {
                    var min = Math.Min(pixels[i], Math.Min(pixels[i + 1], pixels[i + 2]));
                    var max = Math.Max(pixels[i], Math.Max(pixels[i + 1], pixels[i + 2]));
                    if (max - min > 30 && max > 80) colorfulSamples++;
                }
                Assert(colorfulSamples > 10_000, "预览包含有效视频像素而非黑图");
            }
            var cached = await generator.GenerateAsync(video, output);
            Assert(cached.Success && cached.FrameCount == 16, "缓存复用");
            Console.WriteLine($"预览 {Path.GetFileName(video.FullPath)}: {watch.Elapsed.TotalSeconds:F2}s -> {output}");
        }
    }

    private static async Task TestBatchExecutorAsync()
    {
        var root = Path.Combine(TestRoot, "batch");
        var destination = Path.Combine(root, "destination");
        Directory.CreateDirectory(destination);
        var source = Path.Combine(root, "same.mp4");
        var existing = Path.Combine(destination, "same.mp4");
        await File.WriteAllTextAsync(source, "new");
        await File.WriteAllTextAsync(existing, "old");
        var executor = new BatchExecutor();
        var first = await executor.ExecuteAsync(new[] { new BatchOperation(source, false, destination) });
        Assert(first.Moved == 1 && !File.Exists(source), "移动源文件");
        Assert(await File.ReadAllTextAsync(existing) == "old", "重名不覆盖");
        Assert(await File.ReadAllTextAsync(Path.Combine(destination, "same (1).mp4")) == "new", "自动重名(1)");

        await File.WriteAllTextAsync(source, "new2");
        var second = await executor.ExecuteAsync(new[] { new BatchOperation(source, false, destination) });
        Assert(second.Moved == 1 && File.Exists(Path.Combine(destination, "same (2).mp4")), "自动重名(2)");

        var same = Path.Combine(destination, "already.mp4");
        await File.WriteAllTextAsync(same, "same");
        var sameResult = await executor.ExecuteAsync(new[] { new BatchOperation(same, false, destination) });
        Assert(sameResult.Moved == 1 && sameResult.SucceededPaths.Contains(same), "目标同目录安全跳过");

        var delete = Path.Combine(root, "delete.mp4");
        await File.WriteAllTextAsync(delete, "delete");
        var deleteResult = await executor.ExecuteAsync(new[] { new BatchOperation(delete, true, null) });
        Assert(deleteResult.Deleted == 1 && !File.Exists(delete), "删除到回收站");

        var missing = Path.Combine(root, "missing.mp4");
        var failed = await executor.ExecuteAsync(new[] { new BatchOperation(missing, false, destination) });
        Assert(failed.Failed == 1 && failed.Errors.Count == 1, "缺失文件返回错误");
    }

    private static async Task TestLargeFileAsync(VideoDescriptor source)
    {
        var large = Path.Combine(TestRoot, "large-2gb.mp4");
        File.Copy(source.FullPath, large);
        await using (var stream = new FileStream(large, FileMode.Open, FileAccess.Write, FileShare.Read))
            stream.SetLength(2L * 1024 * 1024 * 1024);
        var info = new FileInfo(large);
        var video = new VideoDescriptor(large, info.Length, info.LastWriteTimeUtc);
        var output = Path.Combine(TestRoot, "previews", "large-2gb.jpg");
        var watch = Stopwatch.StartNew();
        var result = await new PreviewGenerator().GenerateAsync(video, output);
        watch.Stop();
        Assert(result.Success, $"2GB文件预览：{result.Error}");
        Console.WriteLine($"2GB 文件 16 帧预览耗时: {watch.Elapsed.TotalSeconds:F2}s，大小: {new FileInfo(output).Length / 1024}KB");
    }

    private static async Task TestCancelledBatchAsync()
    {
        var root = Path.Combine(TestRoot, "cancelled-batch");
        Directory.CreateDirectory(root);
        var paths = Enumerable.Range(0, 3).Select(i => Path.Combine(root, $"{i}.mp4")).ToArray();
        foreach (var path in paths) await File.WriteAllTextAsync(path, "test");
        using var cts = new CancellationTokenSource();
        var progress = new InlineProgress<BatchProgress>(_ => cts.Cancel());
        var result = await new BatchExecutor().ExecuteAsync(paths.Select(p => new BatchOperation(p, false, Path.Combine(root, "out"))), progress, cts.Token);
        Assert(result.Cancelled && result.Moved == 1 && result.SucceededPaths.SequenceEqual(paths.Take(1)), "取消批处理保留已完成结果");
        Assert(paths.Skip(1).All(File.Exists), "取消后未处理文件保持原位");
        var preCancelled = await new BatchExecutor().ExecuteAsync(paths.Skip(1).Select(p => new BatchOperation(p, false, root)), null, cts.Token);
        Assert(preCancelled.Cancelled && preCancelled.Moved == 0, "执行前取消不触碰文件");
    }

    private static async Task TestInvalidPreviewAsync(VideoDescriptor video)
    {
        var output = Path.Combine(TestRoot, "previews", "corrupt.jpg");
        await File.WriteAllTextAsync(output, "invalid jpeg");
        var result = await new PreviewGenerator().GenerateAsync(video, output);
        Assert(result.Success && new FileInfo(output).Length > 10_000, "损坏缓存自动重建");
        var badVideo = Path.Combine(TestRoot, "broken.mp4");
        await File.WriteAllTextAsync(badVideo, "invalid video");
        var info = new FileInfo(badVideo);
        var invalid = await new PreviewGenerator().GenerateAsync(new VideoDescriptor(info.FullName, info.Length, info.LastWriteTimeUtc), Path.Combine(TestRoot, "previews", "broken.jpg"));
        Assert(!invalid.Success && !string.IsNullOrWhiteSpace(invalid.Error), "损坏视频提供错误原因");
    }

    private sealed class InlineProgress<T>(Action<T> action) : IProgress<T>
    {
        public void Report(T value) => action(value);
    }

    private static async Task TestShortPreviewAsync()
    {
        var path = Path.Combine(TestRoot, "one-frame.mp4");
        await RunFFmpegAsync(new[] { "-y", "-f", "lavfi", "-i", "color=c=blue:size=180x320:rate=25", "-frames:v", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", path });
        var info = new FileInfo(path);
        var result = await new PreviewGenerator().GenerateAsync(new VideoDescriptor(path, info.Length, info.LastWriteTimeUtc), Path.Combine(TestRoot, "previews", "one-frame.jpg"));
        Assert(result.Success && result.FrameCount == 16, $"极短竖屏视频预览：{result.Error}");
    }

    private static async Task TestProjectsAsync()
    {
        var root = Path.Combine(TestRoot, "projects");
        var path = Path.Combine(root, "one.framepick");
        var category = new CategoryDefinition { Name = "精选", Destination = Path.Combine(root, "selected") };
        var project = new FramePickProject { Name = "项目一", Categories = new() { category }, SourceFolders = new() { root, root.ToUpperInvariant() } };
        ProjectStore.Save(path, project, createNew: true);
        var loaded = ProjectStore.Load(path);
        Assert(loaded.Name == "项目一" && loaded.Categories.Single().Name == "精选", "项目保存分类与名称");
        Assert(loaded.SourceFolders.Count == 1, "项目导入目录去重");
        loaded.PreviewConcurrency = 3;
        ProjectStore.Save(path, loaded);
        Assert(ProjectStore.Load(path).PreviewConcurrency == 3 && ProjectStore.Load(path + ".bak").PreviewConcurrency == 2, "项目原子保存与上次版本备份");
        await AssertThrows<IOException>(() => Task.Run(() => ProjectStore.Save(path, new FramePickProject { Name = "不能覆盖" }, createNew: true)), "新建项目不覆盖已有项目");
        Assert(ProjectStore.Load(path).Name == "项目一", "新建失败后原项目保持完整");
        var second = Path.Combine(root, "two.framepick");
        ProjectStore.Save(second, new FramePickProject { Name = "项目二" }, createNew: true);
        Assert(ProjectStore.Load(second).Categories.Count == 0 && ProjectStore.Load(path).Categories.Count == 1, "项目分类独立保存");
        var invalid = Path.Combine(root, "invalid.framepick");
        await File.WriteAllTextAsync(invalid, "{\"Name\":\"not a project\"}");
        await AssertThrows<InvalidDataException>(() => Task.Run(() => ProjectStore.Load(invalid)), "拒绝伪项目格式");
        await File.WriteAllTextAsync(invalid, "broken json");
        await AssertThrows<InvalidDataException>(() => Task.Run(() => ProjectStore.Load(invalid)), "损坏项目返回明确错误");
    }

    private static async Task RunFFmpegAsync(IEnumerable<string> arguments)
    {
        var start = new ProcessStartInfo(FFmpegLocator.GetFFmpegPath())
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardError = true,
            StandardErrorEncoding = Encoding.UTF8
        };
        foreach (var argument in arguments) start.ArgumentList.Add(argument);
        using var process = Process.Start(start) ?? throw new InvalidOperationException("ffmpeg 启动失败");
        var error = await process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync();
        if (process.ExitCode != 0) throw new InvalidOperationException(error[^Math.Min(2000, error.Length)..]);
    }

    private static void Assert(bool condition, string name)
    {
        if (!condition) throw new InvalidOperationException("FAIL: " + name);
        _passed++;
        Console.WriteLine("OK: " + name);
    }

    private static async Task AssertThrows<T>(Func<Task> action, string name) where T : Exception
    {
        try { await action(); }
        catch (T) { Assert(true, name); return; }
        throw new InvalidOperationException("FAIL: " + name);
    }
}
