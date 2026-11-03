namespace FramePick.Core;

/// <summary>描述一个待处理的视频文件。</summary>
public sealed record VideoDescriptor(
    string FullPath,
    long Length,
    DateTime LastWriteTimeUtc);
