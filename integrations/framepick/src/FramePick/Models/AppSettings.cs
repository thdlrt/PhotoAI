namespace FramePick.Models;

public sealed class AppSettings
{
    public string LastProjectPath { get; set; } = string.Empty;
    public List<CategoryDefinition> Categories { get; set; } = new();
    public string LastFolder { get; set; } = string.Empty;
    public int PreviewConcurrency { get; set; } = 2;
}

public sealed class VideoDecision
{
    public long? Length { get; set; }
    public DateTime? LastWriteTimeUtc { get; set; }
    public string Path { get; set; } = string.Empty;
    public string CategoryId { get; set; } = string.Empty;
    public bool Delete { get; set; }
}

public sealed class WorkspaceState
{
    public List<VideoDecision> Decisions { get; set; } = new();
}
