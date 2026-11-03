using System.Diagnostics;
using System.IO;
using System.Text;

namespace FramePick.Core;

internal static class ProcessRunner
{
    private const int MaximumOutputCharacters = 16 * 1024;

    public static async Task<ProcessOutput> RunAsync(
        string executablePath,
        IEnumerable<string> arguments,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = executablePath,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };

        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        using var process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        try
        {
            if (!process.Start())
            {
                throw new InvalidOperationException($"无法启动进程：{Path.GetFileName(executablePath)}");
            }
        }
        catch (Exception ex) when (ex is not InvalidOperationException)
        {
            throw new InvalidOperationException(
                $"无法启动 {Path.GetFileName(executablePath)}：{ex.Message}", ex);
        }

        var standardOutputTask = ReadBoundedAsync(process.StandardOutput);
        var standardErrorTask = ReadBoundedAsync(process.StandardError);
        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeoutCts.CancelAfter(timeout);

        try
        {
            await process.WaitForExitAsync(timeoutCts.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            TryKillProcessTree(process);
            await IgnoreCancellationCleanupAsync(process, standardOutputTask, standardErrorTask)
                .ConfigureAwait(false);

            cancellationToken.ThrowIfCancellationRequested();
            throw new TimeoutException(
                $"{Path.GetFileName(executablePath)} 执行超时（{timeout.TotalSeconds:0} 秒）。");
        }

        var outputs = await Task.WhenAll(standardOutputTask, standardErrorTask).ConfigureAwait(false);
        return new ProcessOutput(process.ExitCode, outputs[0].Text, outputs[1].Text, outputs[1].Truncated);
    }

    private static async Task IgnoreCancellationCleanupAsync(
        Process process,
        params Task<BoundedText>[] readTasks)
    {
        try
        {
            await process.WaitForExitAsync(CancellationToken.None)
                .WaitAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false);
        }
        catch
        {
            // Kill 已请求；进程退出确认失败时也不能无限等待。
        }

        try
        {
            await Task.WhenAll(readTasks).WaitAsync(TimeSpan.FromSeconds(1)).ConfigureAwait(false);
        }
        catch
        {
            // 读取任务已被观察，句柄由 Process 释放。
        }
    }

    private static void TryKillProcessTree(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception ex) when (ex is InvalidOperationException or NotSupportedException or System.ComponentModel.Win32Exception)
        {
            // 进程可能已自行退出，或系统不支持结束整棵进程树。
        }
    }

    private static async Task<BoundedText> ReadBoundedAsync(TextReader reader)
    {
        var buffer = new char[4096];
        var text = new StringBuilder(MaximumOutputCharacters);
        var truncated = false;

        while (true)
        {
            var read = await reader.ReadAsync(buffer.AsMemory(), CancellationToken.None).ConfigureAwait(false);
            if (read == 0)
            {
                break;
            }

            if (text.Length + read <= MaximumOutputCharacters)
            {
                text.Append(buffer, 0, read);
            }
            else
            {
                // 保留末尾，FFmpeg 的具体错误通常位于 stderr 最后。
                var overflow = text.Length + read - MaximumOutputCharacters;
                if (overflow < text.Length)
                {
                    text.Remove(0, overflow);
                    text.Append(buffer, 0, read);
                }
                else
                {
                    text.Clear();
                    text.Append(buffer, read - MaximumOutputCharacters, MaximumOutputCharacters);
                }

                truncated = true;
            }
        }

        return new BoundedText(text.ToString(), truncated);
    }

    private sealed record BoundedText(string Text, bool Truncated);
}

internal sealed record ProcessOutput(
    int ExitCode,
    string StandardOutput,
    string StandardError,
    bool StandardErrorTruncated)
{
    public string GetErrorSummary()
    {
        var summary = string.IsNullOrWhiteSpace(StandardError)
            ? "未返回错误详情"
            : StandardError.Trim();
        return StandardErrorTruncated ? summary + "…[stderr 已截断]" : summary;
    }
}
