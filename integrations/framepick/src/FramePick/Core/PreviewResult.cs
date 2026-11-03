namespace FramePick.Core;

/// <summary>预览生成结果。取消操作通过 OperationCanceledException 传递。</summary>
public sealed record PreviewResult(
    bool Success,
    string? Error,
    double DurationSeconds,
    int FrameCount);
