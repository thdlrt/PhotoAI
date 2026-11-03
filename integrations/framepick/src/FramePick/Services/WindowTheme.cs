using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace FramePick.Services;

internal static class WindowTheme
{
    public static void Attach(Window window)
    {
        window.SourceInitialized += (_, _) =>
        {
            var enabled = 1;
            var handle = new WindowInteropHelper(window).Handle;
            // 仅设置当前应用窗口的标题栏，旧版 Windows 不支持时保留系统样式。
            _ = DwmSetWindowAttribute(handle, 20, ref enabled, sizeof(int));
        };
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);
}
