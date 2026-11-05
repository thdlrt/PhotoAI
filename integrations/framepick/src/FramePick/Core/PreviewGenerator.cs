using System.Globalization;
using System.Runtime.ExceptionServices;

namespace FramePick.Core;

/// <summary>使用 FFmpeg 截取 16 或 25 帧，合成带时间戳的方形网格预览。</summary>
public sealed class PreviewGenerator
{
    public const int PreviewFrameCount = 16;

    private const int FrameWidth = 320;
    private const int FrameHeight = 180;
    private const int CellHeight = 200;
    private static readonly TimeSpan ProbeTimeout = TimeSpan.FromSeconds(20);
    private static readonly TimeSpan FrameTimeout = TimeSpan.FromSeconds(45);

    /// <summary>
    /// 生成 4×4（1280×800）或 5×5（1600×1000）预览；可复用有效缓存或强制重做。
    /// 普通失败通过 PreviewResult 返回；取消会结束 FFmpeg 进程树并抛出 OperationCanceledException。
    /// </summary>
    public async Task<PreviewResult> GenerateAsync(
        VideoDescriptor video,
        string outputJpegPath,
        CancellationToken cancellationToken = default,
        int frameCount = PreviewFrameCount,
        bool forceRegenerate = false)
    {
        ArgumentNullException.ThrowIfNull(video);
        ArgumentException.ThrowIfNullOrWhiteSpace(outputJpegPath);
        if (frameCount is not (16 or 25)) throw new ArgumentOutOfRangeException(nameof(frameCount), "预览帧数仅支持 16 或 25。");

        double duration = 0;
        var completedFrames = 0;
        string? temporaryDirectory = null;

        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            var fullOutputPath = Path.GetFullPath(outputJpegPath);
            var outputDirectory = Path.GetDirectoryName(fullOutputPath)
                ?? throw new IOException("无法确定预览输出目录。");
            Directory.CreateDirectory(outputDirectory);

            if (!forceRegenerate && HasUsablePreview(fullOutputPath, frameCount))
            {
                try
                {
                    duration = await ProbeDurationAsync(video.FullPath, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    throw;
                }
                catch
                {
                    // 缓存有效时，时长读取失败不应阻止直接使用预览。
                }

                return new PreviewResult(true, null, duration, frameCount);
            }

            if (string.IsNullOrWhiteSpace(video.FullPath) || !File.Exists(video.FullPath))
            {
                return new PreviewResult(false, "视频文件不存在或不可访问。", 0, 0);
            }

            duration = await ProbeDurationAsync(video.FullPath, cancellationToken).ConfigureAwait(false);
            var timestamps = CreateTimestamps(duration, frameCount);

            temporaryDirectory = Path.Combine(
                outputDirectory,
                $".{Path.GetFileName(fullOutputPath)}.{Guid.NewGuid():N}.frames");
            Directory.CreateDirectory(temporaryDirectory);

            var framePaths = Enumerable.Range(0, frameCount)
                .Select(index => Path.Combine(temporaryDirectory, $"frame-{index:00}.jpg"))
                .ToArray();

            await ExtractFramesAsync(
                    video.FullPath,
                    framePaths,
                    timestamps,
                    count => completedFrames = count,
                    cancellationToken)
                .ConfigureAwait(false);

            cancellationToken.ThrowIfCancellationRequested();
            var renderedPreviewPath = Path.Combine(temporaryDirectory, "preview.jpg");
            await ComposePreviewAsync(
                    framePaths,
                    timestamps,
                    renderedPreviewPath,
                    cancellationToken)
                .ConfigureAwait(false);

            cancellationToken.ThrowIfCancellationRequested();
            CommitPreview(renderedPreviewPath, fullOutputPath, frameCount, forceRegenerate);
            return new PreviewResult(true, null, duration, frameCount);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            return new PreviewResult(false, GetFriendlyError(ex), duration, completedFrames);
        }
        finally
        {
            if (temporaryDirectory is not null)
            {
                TryDeleteDirectory(temporaryDirectory);
            }
        }
    }

    private static async Task<double> ProbeDurationAsync(
        string videoPath,
        CancellationToken cancellationToken)
    {
        var output = await ProcessRunner.RunAsync(
                FFmpegLocator.GetFFprobePath(),
                new[]
                {
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    videoPath
                },
                ProbeTimeout,
                cancellationToken)
            .ConfigureAwait(false);

        if (output.ExitCode != 0)
        {
            throw new InvalidDataException($"ffprobe 无法读取视频时长：{output.GetErrorSummary()}");
        }

        var value = output.StandardOutput
            .Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .FirstOrDefault();

        if (!double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var duration)
            || !double.IsFinite(duration)
            || duration <= 0)
        {
            throw new InvalidDataException("ffprobe 未返回有效的视频时长（时长为空、N/A 或不大于 0）。");
        }

        return duration;
    }

    private static double[] CreateTimestamps(double duration, int frameCount)
    {
        var timestamps = new double[frameCount];
        for (var index = 0; index < timestamps.Length; index++)
        {
            // 每帧位于对应时间区间的中点，最后一帧严格小于 duration。
            var timestamp = duration * (index + 0.5) / frameCount;
            timestamps[index] = Math.Max(0, Math.Min(timestamp, duration - double.Epsilon));
        }

        return timestamps;
    }

    private static async Task ExtractFramesAsync(
        string videoPath,
        IReadOnlyList<string> framePaths,
        IReadOnlyList<double> timestamps,
        Action<int> reportCompleted,
        CancellationToken cancellationToken)
    {
        var ffmpegPath = FFmpegLocator.GetFFmpegPath();
        using var semaphore = new SemaphoreSlim(2, 2);
        using var linkedCts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        var completed = 0;
        Exception? firstFailure = null;

        var tasks = new Task[framePaths.Count];
        for (var index = 0; index < tasks.Length; index++)
        {
            var frameIndex = index;
            tasks[index] = Task.Run(async () =>
            {
                try
                {
                    await semaphore.WaitAsync(linkedCts.Token).ConfigureAwait(false);
                    try
                    {
                        var arguments = new[]
                                {
                                    "-hide_banner", "-loglevel", "error",
                                    "-ss", timestamps[frameIndex].ToString("R", CultureInfo.InvariantCulture),
                                    "-i", videoPath,
                                    "-map", "0:v:0",
                                    "-frames:v", "1",
                                    "-an", "-sn", "-dn",
                                    "-vf", "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2:black",
                                    "-threads", "2",
                                    "-pix_fmt", "yuvj420p",
                                    "-q:v", "2",
                                    "-y", framePaths[frameIndex]
                                };
                        var output = await ProcessRunner.RunAsync(ffmpegPath, arguments, FrameTimeout, linkedCts.Token).ConfigureAwait(false);
                        // 单帧或极短视频在精确定位后可能已无下一帧，回退到首帧。
                        if (output.ExitCode == 0 && !HasUsableCache(framePaths[frameIndex]))
                        {
                            arguments[Array.IndexOf(arguments, "-ss") + 1] = "0";
                            output = await ProcessRunner.RunAsync(ffmpegPath, arguments, FrameTimeout, linkedCts.Token).ConfigureAwait(false);
                        }

                        if (output.ExitCode != 0)
                        {
                            throw new InvalidOperationException(
                                $"ffmpeg 截取第 {frameIndex + 1} 帧失败（退出码 {output.ExitCode}）：{output.GetErrorSummary()}");
                        }

                        if (!HasUsableCache(framePaths[frameIndex]))
                        {
                            throw new InvalidDataException($"ffmpeg 未生成有效的第 {frameIndex + 1} 帧图片。");
                        }

                        var current = Interlocked.Increment(ref completed);
                        reportCompleted(current);
                    }
                    finally
                    {
                        semaphore.Release();
                    }
                }
                catch (Exception ex)
                {
                    Interlocked.CompareExchange(ref firstFailure, ex, null);
                    linkedCts.Cancel();
                    throw;
                }
            });
        }

        try
        {
            await Task.WhenAll(tasks).ConfigureAwait(false);
        }
        catch
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (firstFailure is not null)
            {
                ExceptionDispatchInfo.Capture(firstFailure).Throw();
            }

            throw;
        }
    }

    private static async Task ComposePreviewAsync(
        IReadOnlyList<string> framePaths,
        IReadOnlyList<double> timestamps,
        string outputPath,
        CancellationToken cancellationToken)
    {
        // 直接由 FFmpeg 拼图，避免 WPF RenderTargetBitmap 在后台线程上产生黑图。
        var arguments = new List<string>
        {
            "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "1"
        };
        foreach (var framePath in framePaths)
        {
            arguments.Add("-i");
            arguments.Add(framePath);
        }

        var filter = new System.Text.StringBuilder(8192);
        var columns = (int)Math.Sqrt(framePaths.Count);
        for (var index = 0; index < framePaths.Count; index++)
        {
            var timestamp = FormatTimestamp(timestamps[index]).Replace(":", "\\:");
            filter.Append('[').Append(index).Append(":v]pad=320:200:0:0:0x18181C,")
                .Append("drawtext=fontfile='C\\:/Windows/Fonts/segoeui.ttf':text='")
                .Append(timestamp)
                .Append("':x=6:y=182:fontsize=13:fontcolor=white[v")
                .Append(index).Append("];");
        }
        for (var index = 0; index < framePaths.Count; index++) filter.Append("[v").Append(index).Append(']');
        filter.Append("xstack=inputs=").Append(framePaths.Count).Append(":layout=");
        for (var index = 0; index < framePaths.Count; index++)
        {
            if (index > 0) filter.Append('|');
            filter.Append((index % columns) * FrameWidth).Append('_').Append((index / columns) * CellHeight);
        }
        filter.Append(":fill=0x18181C[sheet]");

        arguments.Add("-filter_complex");
        arguments.Add(filter.ToString());
        arguments.Add("-map");
        arguments.Add("[sheet]");
        arguments.Add("-frames:v");
        arguments.Add("1");
        arguments.Add("-q:v");
        arguments.Add("2");
        arguments.Add("-pix_fmt");
        arguments.Add("yuvj420p");
        arguments.Add("-update");
        arguments.Add("1");
        arguments.Add("-y");
        arguments.Add(outputPath);

        var result = await ProcessRunner.RunAsync(
            FFmpegLocator.GetFFmpegPath(), arguments, FrameTimeout, cancellationToken).ConfigureAwait(false);
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"ffmpeg 合成预览失败：{result.GetErrorSummary()}");
        if (!HasUsableCache(outputPath)) throw new InvalidDataException("ffmpeg 未生成有效的预览图片。");
    }

    private static string FormatTimestamp(double seconds)
    {
        var value = TimeSpan.FromSeconds(Math.Max(0, seconds));
        return $"{(long)value.TotalHours:00}:{value.Minutes:00}:{value.Seconds:00}";
    }

    private static bool HasUsableCache(string path)
    {
        try
        {
            using var stream = File.OpenRead(path);
            var decoder = System.Windows.Media.Imaging.BitmapDecoder.Create(stream,
                System.Windows.Media.Imaging.BitmapCreateOptions.IgnoreColorProfile,
                System.Windows.Media.Imaging.BitmapCacheOption.OnLoad);
            return decoder.Frames.Count > 0 && decoder.Frames[0].PixelWidth > 0 && decoder.Frames[0].PixelHeight > 0;
        }
        catch { return false; }
    }

    private static bool HasUsablePreview(string path, int frameCount)
    {
        try
        {
            using var stream = File.OpenRead(path);
            var decoder = System.Windows.Media.Imaging.BitmapDecoder.Create(stream,
                System.Windows.Media.Imaging.BitmapCreateOptions.IgnoreColorProfile,
                System.Windows.Media.Imaging.BitmapCacheOption.OnLoad);
            var columns = (int)Math.Sqrt(frameCount);
            return decoder.Frames.Count > 0 && decoder.Frames[0].PixelWidth == columns * FrameWidth
                && decoder.Frames[0].PixelHeight == columns * CellHeight;
        }
        catch { return false; }
    }

    private static void CommitPreview(string renderedPreviewPath, string outputPath, int frameCount, bool forceRegenerate)
    {
        if (forceRegenerate)
        {
            File.Move(renderedPreviewPath, outputPath, overwrite: true);
            return;
        }
        if (File.Exists(outputPath))
        {
            if (HasUsablePreview(outputPath, frameCount))
            {
                return;
            }

            File.Delete(outputPath);
        }

        try
        {
            File.Move(renderedPreviewPath, outputPath, overwrite: false);
        }
        catch (IOException) when (HasUsablePreview(outputPath, frameCount))
        {
            // 并发生成已先写入有效缓存，保留先完成的文件。
        }
    }

    private static string GetFriendlyError(Exception ex) =>
        ex switch
        {
            FileNotFoundException => ex.Message,
            InvalidDataException => ex.Message,
            TimeoutException => ex.Message,
            UnauthorizedAccessException => $"没有足够权限生成预览：{ex.Message}",
            IOException => $"预览文件读写失败：{ex.Message}",
            InvalidOperationException => ex.Message,
            _ => $"生成预览失败：{ex.Message}"
        };

    private static void TryDeleteDirectory(string path)
    {
        try
        {
            if (Directory.Exists(path))
            {
                Directory.Delete(path, recursive: true);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // 临时文件清理失败不影响已生成的预览，也不覆盖原始错误。
        }
    }
}
