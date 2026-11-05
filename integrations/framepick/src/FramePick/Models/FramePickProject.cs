using System.IO;

namespace FramePick.Models;

public sealed class FramePickProject
{
    public string Format { get; set; } = "FramePick.Project";
    public int Version { get; set; } = 1;
    public string Id { get; set; } = Guid.NewGuid().ToString("N");
    public string Name { get; set; } = string.Empty;
    public List<string> SourceFolders { get; set; } = new();
    public List<CategoryDefinition> Categories { get; set; } = new();
    public int PreviewConcurrency { get; set; } = 2;
    public int PreviewFrameCount { get; set; } = 16;
    public List<VideoDecision> Decisions { get; set; } = new();
}

public sealed record SourceFolderItem(string FullPath)
{
    public string Name => Path.GetFileName(Path.TrimEndingDirectorySeparator(FullPath)) is { Length: > 0 } name ? name : FullPath;
}
