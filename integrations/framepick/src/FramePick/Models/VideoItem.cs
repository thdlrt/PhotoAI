using System.IO;
using System.Windows.Media.Imaging;
using FramePick.Core;
using FramePick.ViewModels;

namespace FramePick.Models;

public enum PreviewState
{
    Waiting,
    Loading,
    Ready,
    Failed
}

public sealed class VideoItem : ObservableObject
{
    private string _categoryId = string.Empty;
    private string _categoryName = string.Empty;
    private bool _isMarkedForDeletion;
    private PreviewState _previewState = PreviewState.Waiting;
    private BitmapImage? _previewImage;
    private BitmapImage? _detailImage;
    private string _error = string.Empty;
    private double _durationSeconds;

    public VideoItem(VideoDescriptor descriptor, string previewPath)
    {
        Descriptor = descriptor;
        PreviewPath = previewPath;
    }

    public VideoDescriptor Descriptor { get; }
    public string FullPath => Descriptor.FullPath;
    public string Title => Path.GetFileNameWithoutExtension(FullPath);
    public string PreviewPath { get; private set; }

    public void ChangePreviewPath(string path)
    {
        if (PreviewPath == path) return;
        PreviewPath = path;
        PreviewImage = null;
        DetailImage = null;
        Error = string.Empty;
        PreviewState = PreviewState.Waiting;
        OnPropertyChanged(nameof(PreviewPath));
    }

    public string CategoryId
    {
        get => _categoryId;
        set
        {
            value ??= string.Empty;
            if (!SetProperty(ref _categoryId, value)) return;
            if (value.Length > 0) IsMarkedForDeletion = false;
            OnPropertyChanged(nameof(HasCategory));
            OnPropertyChanged(nameof(HasDecision));
            OnPropertyChanged(nameof(DecisionText));
        }
    }

    public string CategoryName
    {
        get => _categoryName;
        set { if (SetProperty(ref _categoryName, value)) OnPropertyChanged(nameof(DecisionText)); }
    }

    public bool HasCategory => CategoryId.Length > 0;

    public bool IsMarkedForDeletion
    {
        get => _isMarkedForDeletion;
        set
        {
            if (!SetProperty(ref _isMarkedForDeletion, value)) return;
            if (value)
            {
                CategoryId = string.Empty;
                CategoryName = string.Empty;
            }
            OnPropertyChanged(nameof(HasDecision));
            OnPropertyChanged(nameof(DecisionText));
        }
    }

    public bool HasDecision => IsMarkedForDeletion || HasCategory;
    public string DecisionText => IsMarkedForDeletion ? "待删除" : HasCategory ? CategoryName : "未分类";
    public string FileName => Path.GetFileName(FullPath);
    public string FileSizeText => Descriptor.Length >= 1024L * 1024 * 1024
        ? $"{Descriptor.Length / (1024d * 1024 * 1024):0.00} GB"
        : $"{Descriptor.Length / (1024d * 1024):0.0} MB";
    public string ExtensionText => Path.GetExtension(FullPath).TrimStart('.').ToUpperInvariant();

    public PreviewState PreviewState
    {
        get => _previewState;
        set
        {
            if (!SetProperty(ref _previewState, value)) return;
            OnPropertyChanged(nameof(HasPreview));
            OnPropertyChanged(nameof(IsPreviewLoading));
            OnPropertyChanged(nameof(HasPreviewError));
        }
    }

    public bool HasPreview => PreviewState == PreviewState.Ready && PreviewImage is not null;
    public bool IsPreviewLoading => PreviewState == PreviewState.Loading;
    public bool HasPreviewError => PreviewState == PreviewState.Failed;

    public BitmapImage? PreviewImage
    {
        get => _previewImage;
        set
        {
            if (SetProperty(ref _previewImage, value)) OnPropertyChanged(nameof(HasPreview));
        }
    }

    public BitmapImage? DetailImage
    {
        get => _detailImage;
        set => SetProperty(ref _detailImage, value);
    }

    public string Error
    {
        get => _error;
        set => SetProperty(ref _error, value);
    }

    public double DurationSeconds
    {
        get => _durationSeconds;
        set
        {
            if (SetProperty(ref _durationSeconds, value)) OnPropertyChanged(nameof(DurationText));
        }
    }

    public string DurationText => DurationSeconds <= 0 ? string.Empty : DurationSeconds >= 3600
        ? $"{(long)(DurationSeconds / 3600)}:{TimeSpan.FromSeconds(DurationSeconds):mm\\:ss}"
        : TimeSpan.FromSeconds(DurationSeconds).ToString(@"m\:ss");
}
