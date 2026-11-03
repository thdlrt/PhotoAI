namespace FramePick.Core;

/// <summary>一个删除或移动操作；Delete 为 true 时 DestinationDirectory 应为 null。</summary>
public sealed record BatchOperation
{
    public BatchOperation()
    {
    }

    public BatchOperation(string videoPath, bool delete, string? destinationDirectory)
    {
        VideoPath = videoPath;
        Delete = delete;
        DestinationDirectory = destinationDirectory;
    }

    public string VideoPath { get; init; } = string.Empty;

    public bool Delete { get; init; }

    public string? DestinationDirectory { get; init; }
}

/// <summary>已处理数量包含成功与失败项；CurrentPath 是刚处理完的原始路径。</summary>
public sealed record BatchProgress(
    int Completed,
    int Total,
    string CurrentPath);

/// <summary>批处理汇总；SucceededPaths 保留成功项的原始 VideoPath，供 UI 移除这些项。</summary>
public sealed record BatchResult(
    int Moved,
    int Deleted,
    int Failed,
    IReadOnlyList<string> Errors,
    IReadOnlyList<string> SucceededPaths)
{
    public bool Cancelled { get; init; }
    public BatchResult()
        : this(0, 0, 0, Array.Empty<string>(), Array.Empty<string>())
    {
    }
}
