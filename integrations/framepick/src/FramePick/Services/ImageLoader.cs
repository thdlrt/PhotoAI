using System.IO;
using System.Windows.Media.Imaging;

namespace FramePick.Services;

public static class ImageLoader
{
    public static Task<BitmapImage?> LoadAsync(string path, int decodeWidth = 640, CancellationToken cancellationToken = default) =>
        Task.Run<BitmapImage?>(() =>
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (!File.Exists(path)) return null;
            try
            {
                var image = new BitmapImage();
                image.BeginInit();
                image.CacheOption = BitmapCacheOption.OnLoad;
                image.CreateOptions = BitmapCreateOptions.IgnoreColorProfile | BitmapCreateOptions.IgnoreImageCache;
                image.DecodePixelWidth = decodeWidth;
                image.UriSource = new Uri(path, UriKind.Absolute);
                image.EndInit();
                image.Freeze();
                cancellationToken.ThrowIfCancellationRequested();
                return image;
            }
            catch (OperationCanceledException) { throw; }
            catch
            {
                return null;
            }
        }, cancellationToken);
}
