namespace FramePick.Core;

/// <summary>在后台递归扫描常见视频文件。</summary>
public sealed class VideoScanner
{
    private static readonly HashSet<string> VideoExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".mp4", ".mov", ".mkv", ".kmv", ".avi", ".wmv", ".flv", ".webm",
        ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".vob"
    };

    /// <summary>
    /// 扫描目录。进度值为目前已发现的视频数量；无权限目录、失效目录及 reparse point 会被跳过。
    /// </summary>
    public Task<IReadOnlyList<VideoDescriptor>> ScanAsync(
        string root,
        IProgress<int>? progress = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(root);

        return Task.Run<IReadOnlyList<VideoDescriptor>>(
            () => ScanCore(root, progress, cancellationToken),
            cancellationToken);
    }

    private static List<VideoDescriptor> ScanCore(
        string root,
        IProgress<int>? progress,
        CancellationToken cancellationToken)
    {
        var fullRoot = Path.GetFullPath(root);
        if (!Directory.Exists(fullRoot))
        {
            throw new DirectoryNotFoundException($"目录不存在：{fullRoot}");
        }

        var results = new List<VideoDescriptor>();
        var pending = new Stack<string>();
        pending.Push(fullRoot);

        while (pending.Count > 0)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var directory = pending.Pop();

            foreach (var entry in EnumerateEntriesSafely(directory))
            {
                cancellationToken.ThrowIfCancellationRequested();

                FileAttributes attributes;
                try
                {
                    attributes = File.GetAttributes(entry);
                }
                catch (Exception ex) when (IsFileSystemSkippable(ex))
                {
                    continue;
                }

                if ((attributes & FileAttributes.ReparsePoint) != 0)
                {
                    continue;
                }

                if ((attributes & FileAttributes.Directory) != 0)
                {
                    pending.Push(entry);
                    continue;
                }

                if (!VideoExtensions.Contains(Path.GetExtension(entry)))
                {
                    continue;
                }

                try
                {
                    var info = new FileInfo(entry);
                    results.Add(new VideoDescriptor(info.FullName, info.Length, info.LastWriteTimeUtc));
                    progress?.Report(results.Count);
                }
                catch (Exception ex) when (IsFileSystemSkippable(ex))
                {
                    // 文件可能在扫描过程中被移动、删除或失去访问权限，忽略并继续。
                }
            }
        }

        results.Sort(static (left, right) =>
            StringComparer.OrdinalIgnoreCase.Compare(left.FullPath, right.FullPath));
        return results;
    }

    private static IEnumerable<string> EnumerateEntriesSafely(string directory)
    {
        IEnumerator<string>? enumerator = null;
        try
        {
            enumerator = Directory.EnumerateFileSystemEntries(directory).GetEnumerator();
        }
        catch (Exception ex) when (IsFileSystemSkippable(ex))
        {
            yield break;
        }

        using (enumerator)
        {
            while (true)
            {
                string? current = null;
                try
                {
                    if (!enumerator.MoveNext())
                    {
                        yield break;
                    }

                    current = enumerator.Current;
                }
                catch (Exception ex) when (IsFileSystemSkippable(ex))
                {
                    // 延迟枚举中途也可能遇到无权限或断开的目录。
                    yield break;
                }

                yield return current;
            }
        }
    }

    private static bool IsFileSystemSkippable(Exception ex) =>
        ex is UnauthorizedAccessException
            or IOException
            or DirectoryNotFoundException
            or FileNotFoundException
            or NotSupportedException
            or ArgumentException;
}
