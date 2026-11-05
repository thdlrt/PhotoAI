using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using FramePick.Models;

namespace FramePick.Services;

public static class StateStore
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };
    private static readonly object Sync = new();

    private static string StorageRoot => Environment.GetEnvironmentVariable("FRAMEPICK_DATA_ROOT") is { Length: > 0 } root ? root : AppContext.BaseDirectory;
    public static string DataDirectory { get; } = Path.Combine(StorageRoot, "Data");
    public static string CacheDirectory { get; } = Path.Combine(StorageRoot, "Cache");

    public static AppSettings LoadSettings()
    {
        try
        {
            var path = Path.Combine(DataDirectory, "settings.json");
            var settings = File.Exists(path)
                ? JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(path)) ?? new AppSettings()
                : new AppSettings();
            settings.Categories = (settings.Categories ?? new()).Where(c => c is not null && !string.IsNullOrWhiteSpace(c.Id))
                .GroupBy(c => c.Id).Select(g => g.First()).ToList();
            settings.PreviewConcurrency = Math.Clamp(settings.PreviewConcurrency, 1, 4);
            return settings;
        }
        catch
        {
            return new AppSettings();
        }
    }

    public static void SaveSettings(AppSettings settings)
    {
        lock (Sync) WriteJson(Path.Combine(DataDirectory, "settings.json"), settings);
    }

    public static WorkspaceState LoadWorkspace(string sourceFolder)
    {
        try
        {
            var path = WorkspacePath(sourceFolder);
            var state = File.Exists(path)
                ? JsonSerializer.Deserialize<WorkspaceState>(File.ReadAllText(path)) ?? new WorkspaceState()
                : new WorkspaceState();
            state.Decisions = (state.Decisions ?? new()).Where(d => d is not null && !string.IsNullOrWhiteSpace(d.Path))
                .GroupBy(d => d.Path, StringComparer.OrdinalIgnoreCase).Select(g => g.Last()).ToList();
            return state;
        }
        catch
        {
            return new WorkspaceState();
        }
    }

    public static void SaveWorkspace(string sourceFolder, IEnumerable<VideoItem> videos)
    {
        if (string.IsNullOrWhiteSpace(sourceFolder)) return;
        var state = new WorkspaceState
        {
            Decisions = videos.Where(v => v.HasDecision).Select(v => new VideoDecision
            {
                Path = v.FullPath,
                CategoryId = v.CategoryId,
                Delete = v.IsMarkedForDeletion,
                Length = v.Descriptor.Length,
                LastWriteTimeUtc = v.Descriptor.LastWriteTimeUtc
            }).ToList()
        };
        lock (Sync) WriteJson(WorkspacePath(sourceFolder), state);
    }

    public static string PreviewPathFor(string fullPath, long length, DateTime lastWriteUtc, int frameCount = 16)
    {
        var identity = $"{fullPath}|{length}|{lastWriteUtc.Ticks}";
        if (frameCount is not (16 or 25)) throw new ArgumentOutOfRangeException(nameof(frameCount));
        if (frameCount != 16) identity += $"|frames={frameCount}";
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity))).ToLowerInvariant();
        return Path.Combine(CacheDirectory, hash[..2], hash + ".jpg");
    }

    private static string WorkspacePath(string sourceFolder)
    {
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Path.GetFullPath(sourceFolder)))).ToLowerInvariant();
        return Path.Combine(DataDirectory, "Workspaces", hash + ".json");
    }

    private static void WriteJson(string path, object value)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temp = path + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(value, JsonOptions), new UTF8Encoding(false));
        File.Move(temp, path, true);
    }
}
