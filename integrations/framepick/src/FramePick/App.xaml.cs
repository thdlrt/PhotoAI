using System.IO;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using FramePick.Services;

namespace FramePick;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        if (e.Args.Contains("--software-rendering", StringComparer.OrdinalIgnoreCase))
            RenderOptions.ProcessRenderMode = RenderMode.SoftwareOnly;

        if (e.Args.Contains("--self-test", StringComparer.OrdinalIgnoreCase))
        {
            try
            {
                var window = new MainWindow();
                var categories = new CategoryManagerWindow(Array.Empty<Models.CategoryDefinition>(), 2);
                if (window.FindName("GalleryScrollViewer") is null || window.FindName("RegenerateButton") is null
                    || categories.FindName("AddCategoryButton") is null || categories.FindName("ProjectNameInput") is null
                    || categories.FindName("PreviewFrameCountInput") is null)
                    throw new InvalidDataException("帧选界面资源不完整。");
                Shutdown(0);
            }
            catch { Shutdown(1); }
            return;
        }

        DispatcherUnhandledException += (_, args) =>
        {
            try
            {
                Directory.CreateDirectory(StateStore.DataDirectory);
                File.AppendAllText(Path.Combine(StateStore.DataDirectory, "error.log"),
                    $"{DateTime.Now:O}\r\n{args.Exception}\r\n\r\n");
            }
            catch { }
            MessageBox.Show(args.Exception.Message, "帧选", MessageBoxButton.OK, MessageBoxImage.Error);
            args.Handled = true;
            Shutdown(1);
        };
        base.OnStartup(e);
        if (!Properties.Contains("UiSmoke")) new MainWindow().Show();
    }
}
