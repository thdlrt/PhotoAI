using Microsoft.VisualBasic.FileIO;

namespace FramePick.Core;

/// <summary>逐项执行删除到回收站及不覆盖移动。</summary>
public sealed class BatchExecutor
{
    /// <summary>
    /// 在后台逐项执行操作。取消后返回已完成结果，尚未处理的项不会执行。
    /// </summary>
    public Task<BatchResult> ExecuteAsync(
        IEnumerable<BatchOperation> operations,
        IProgress<BatchProgress>? progress = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(operations);
        var pending = operations.ToList();
        return Task.Run(() => ExecuteCore(pending, progress, cancellationToken));
    }

    private static BatchResult ExecuteCore(
        IEnumerable<BatchOperation> operations,
        IProgress<BatchProgress>? progress,
        CancellationToken cancellationToken)
    {
        var pending = operations.ToList();
        var moved = 0;
        var deleted = 0;
        var failed = 0;
        var completed = 0;
        var errors = new List<string>();
        var succeededPaths = new List<string>();

        foreach (var operation in pending)
        {
            if (cancellationToken.IsCancellationRequested) break;
            var displayPath = operation?.VideoPath ?? "<空路径>";

            try
            {
                if (operation is null || string.IsNullOrWhiteSpace(operation.VideoPath))
                {
                    throw new ArgumentException("视频路径为空。");
                }

                if (operation.Delete)
                {
                    DeleteToRecycleBin(operation.VideoPath);
                    deleted++;
                }
                else
                {
                    MoveWithoutOverwrite(operation.VideoPath, operation.DestinationDirectory);
                    moved++;
                }

                succeededPaths.Add(operation.VideoPath);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                failed++;
                errors.Add($"{displayPath}：{GetConciseMessage(ex)}");
            }

            completed++;
            progress?.Report(new BatchProgress(completed, pending.Count, displayPath));
        }

        return new BatchResult(moved, deleted, failed, errors.AsReadOnly(), succeededPaths.AsReadOnly())
        {
            Cancelled = completed < pending.Count && cancellationToken.IsCancellationRequested
        };
    }

    private static void DeleteToRecycleBin(string videoPath)
    {
        var source = Path.GetFullPath(videoPath);
        EnsureSourceExists(source);
        FileSystem.DeleteFile(
            source,
            UIOption.OnlyErrorDialogs,
            RecycleOption.SendToRecycleBin,
            UICancelOption.ThrowException);
    }

    private static void MoveWithoutOverwrite(string videoPath, string? destinationDirectory)
    {
        if (string.IsNullOrWhiteSpace(destinationDirectory))
        {
            throw new ArgumentException("移动操作缺少目标目录。");
        }

        var source = Path.GetFullPath(videoPath);
        EnsureSourceExists(source);

        var destinationRoot = Path.GetFullPath(destinationDirectory);
        var fileName = Path.GetFileName(source);
        if (string.IsNullOrEmpty(fileName))
        {
            throw new ArgumentException("源路径不是有效文件路径。");
        }

        var intendedTarget = Path.Combine(destinationRoot, fileName);
        if (PathsEqual(source, intendedTarget))
        {
            // 文件已经位于目标目录，不执行移动，但计为成功。
            return;
        }

        Directory.CreateDirectory(destinationRoot);
        var nameWithoutExtension = Path.GetFileNameWithoutExtension(fileName);
        var extension = Path.GetExtension(fileName);

        for (var suffix = 0; ; suffix++)
        {
            var candidateName = suffix == 0
                ? fileName
                : $"{nameWithoutExtension} ({suffix}){extension}";
            var candidate = Path.Combine(destinationRoot, candidateName);

            if (File.Exists(candidate) || Directory.Exists(candidate))
            {
                continue;
            }

            try
            {
                // overwrite:false 防止检查路径后、移动前由其他进程创建同名文件而造成覆盖。
                File.Move(source, candidate, overwrite: false);
                return;
            }
            catch (IOException) when (File.Exists(candidate) || Directory.Exists(candidate))
            {
                // 并发情况下发生了重名，尝试下一个编号。
            }
        }
    }

    private static void EnsureSourceExists(string source)
    {
        if (!File.Exists(source))
        {
            throw new FileNotFoundException("源文件不存在或不可访问。", source);
        }
    }

    private static bool PathsEqual(string left, string right) =>
        string.Equals(
            Path.TrimEndingDirectorySeparator(left),
            Path.TrimEndingDirectorySeparator(right),
            StringComparison.OrdinalIgnoreCase);

    private static string GetConciseMessage(Exception ex) =>
        ex switch
        {
            FileNotFoundException => "源文件不存在或不可访问",
            UnauthorizedAccessException => "没有足够权限，操作被拒绝",
            IOException ioException => $"文件操作失败：{ioException.Message}",
            ArgumentException argumentException => argumentException.Message,
            _ => ex.Message
        };
}
