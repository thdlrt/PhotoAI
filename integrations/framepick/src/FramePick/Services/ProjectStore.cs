using System.IO;
using System.Text;
using System.Text.Json;
using FramePick.Models;

namespace FramePick.Services;

public static class ProjectStore
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };

    public static FramePickProject Load(string path)
    {
        try
        {
            var json = File.ReadAllText(path);
            using var document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind != JsonValueKind.Object
                || !document.RootElement.TryGetProperty("Format", out var format) || format.ValueKind != JsonValueKind.String || format.GetString() != "FramePick.Project"
                || !document.RootElement.TryGetProperty("Version", out var version) || version.ValueKind != JsonValueKind.Number || !version.TryGetInt32(out var number) || number != 1)
                throw new InvalidDataException("此文件不是受支持的 FramePick 项目，请选择 .framepick 文件。");
            var project = JsonSerializer.Deserialize<FramePickProject>(json)
                ?? throw new InvalidDataException("项目内容为空。");
            Normalize(project);
            return project;
        }
        catch (JsonException ex) { throw new InvalidDataException("项目文件损坏或格式不正确，未修改现有项目。", ex); }
    }

    public static void Save(string path, FramePickProject project, bool createNew = false)
    {
        Normalize(project);
        var target = Path.GetFullPath(path);
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        var temporary = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(project, JsonOptions), new UTF8Encoding(false));
            if (!createNew && File.Exists(target)) File.Replace(temporary, target, target + ".bak");
            else File.Move(temporary, target, overwrite: false);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    private static void Normalize(FramePickProject project)
    {
        if (project.Format != "FramePick.Project" || project.Version != 1)
            throw new InvalidDataException("此文件不是受支持的 FramePick 项目，请选择 .framepick 文件。");
        if (string.IsNullOrWhiteSpace(project.Id) || string.IsNullOrWhiteSpace(project.Name))
            throw new InvalidDataException("项目缺少名称或标识。");
        project.SourceFolders = (project.SourceFolders ?? new()).Select(NormalizeFolder)
            .Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        project.Categories ??= new();
        if (project.Categories.Any(c => c is null || string.IsNullOrWhiteSpace(c.Id) || string.IsNullOrWhiteSpace(c.Name))
            || project.Categories.GroupBy(c => c.Id).Any(g => g.Count() > 1))
            throw new InvalidDataException("项目分类无效或标识重复。");
        foreach (var category in project.Categories) category.Destination = NormalizeFolder(category.Destination);
        project.PreviewConcurrency = Math.Clamp(project.PreviewConcurrency, 1, 4);
        project.Decisions = (project.Decisions ?? new()).Where(d => d is not null && !string.IsNullOrWhiteSpace(d.Path))
            .GroupBy(d => d.Path, StringComparer.OrdinalIgnoreCase).Select(g => g.Last()).ToList();
    }

    private static string NormalizeFolder(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path))
            throw new InvalidDataException("项目中的文件夹路径不是完整路径。");
        return Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
    }
}
