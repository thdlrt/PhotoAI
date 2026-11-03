namespace FramePick.Core;

/// <summary>定位 ffmpeg 与 ffprobe 可执行文件。</summary>
public static class FFmpegLocator
{
    /// <summary>优先从 PATH 查找 ffmpeg，然后查找应用 tools 目录。</summary>
    public static string GetFFmpegPath() => Locate("ffmpeg.exe");

    /// <summary>优先从 PATH 查找 ffprobe，然后查找应用 tools 目录。</summary>
    public static string GetFFprobePath() => Locate("ffprobe.exe");

    private static string Locate(string executableName)
    {
        var shared = Environment.GetEnvironmentVariable("FRAMEPICK_FFMPEG_DIRECTORY");
        if (!string.IsNullOrWhiteSpace(shared))
        {
            var candidate = Path.Combine(shared, executableName);
            if (File.Exists(candidate)) return Path.GetFullPath(candidate);
        }
        var pathValue = Environment.GetEnvironmentVariable("PATH");
        if (!string.IsNullOrWhiteSpace(pathValue))
        {
            foreach (var rawDirectory in pathValue.Split(Path.PathSeparator))
            {
                var directory = rawDirectory.Trim().Trim('"');
                if (directory.Length == 0)
                {
                    continue;
                }

                try
                {
                    var candidate = Path.Combine(directory, executableName);
                    if (File.Exists(candidate))
                    {
                        return Path.GetFullPath(candidate);
                    }
                }
                catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException)
                {
                    // PATH 中的无效项不应影响后续查找。
                }
            }
        }

        var toolsPath = Path.Combine(AppContext.BaseDirectory, "tools", executableName);
        if (File.Exists(toolsPath))
        {
            return toolsPath;
        }

        throw new FileNotFoundException(
            $"未找到 {executableName}。请安装 FFmpeg 并加入 PATH，或将其放到：{toolsPath}",
            toolsPath);
    }
}
