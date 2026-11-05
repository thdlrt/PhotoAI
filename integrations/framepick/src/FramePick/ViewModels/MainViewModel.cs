using System.Collections.Concurrent;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Windows;
using System.Windows.Threading;
using FramePick.Core;
using FramePick.Models;
using FramePick.Services;

namespace FramePick.ViewModels;

public sealed record FilterOption(string Key, string Name);

public sealed class MainViewModel : ObservableObject
{
    private const int PageSize = 36;
    private readonly Dispatcher _dispatcher;
    private readonly VideoScanner _scanner = new();
    private readonly PreviewGenerator _previewGenerator = new();
    private readonly BatchExecutor _batchExecutor = new();
    private readonly ConcurrentDictionary<string, byte> _imageLoads = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<VideoItem> _allVideos = new();
    private List<VideoItem> _filteredVideos = new();
    private AppSettings _settings;
    private FramePickProject? _project;
    private string _projectFilePath = string.Empty;
    private CancellationTokenSource? _workCancellation;
    private CancellationTokenSource? _detailLoadCancellation;
    private string _sourceFolder = string.Empty;
    private FilterOption? _selectedFilter;
    private VideoItem? _selectedVideo;
    private bool _isDetailOpen;
    private bool _isBusy;
    private bool _isExecuting;
    private string _operationText = string.Empty;
    private double _operationProgress;
    private bool _isProgressIndeterminate;
    private int _pageIndex;
    private bool _restoring;
    private bool _refreshQueued;
    private bool _forceRegenerating;
    private bool _workspaceDirty;
    private string _searchText = string.Empty;
    private string _selectedSort = "名称 A → Z";
    private string _statusText = "创建或打开项目，开始整理";
    private readonly List<VideoItem> _detailVideos = new();
    private readonly DispatcherTimer _saveTimer;

    public MainViewModel()
    {
        _dispatcher = Application.Current.Dispatcher;
        _settings = StateStore.LoadSettings();
        _saveTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(350) };
        _saveTimer.Tick += (_, _) => FlushWorkspace();
        Categories = new ObservableCollection<CategoryDefinition>();
        ImportedFolders = new ObservableCollection<SourceFolderItem>();
        FilterOptions = new ObservableCollection<FilterOption>();
        CategoryOptions = new ObservableCollection<CategoryDefinition>();
        VisibleVideos = new ObservableCollection<VideoItem>();
        RebuildCategoryOptions();

        OpenDetailCommand = new RelayCommand(p => OpenDetail(p as VideoItem));
        CloseDetailCommand = new RelayCommand(_ => CloseDetail());
        ToggleDeleteCommand = new RelayCommand(p =>
        {
            if (p is VideoItem item) item.IsMarkedForDeletion = !item.IsMarkedForDeletion;
        }, _ => !IsExecuting);
        PreviousDetailCommand = new RelayCommand(_ => MoveDetail(-1), _ => CanMoveDetail(-1));
        NextDetailCommand = new RelayCommand(_ => MoveDetail(1), _ => CanMoveDetail(1));
        PreviousPageCommand = new RelayCommand(_ => GoToPage(PageIndex - 1), _ => PageIndex > 0 && !IsExecuting);
        NextPageCommand = new RelayCommand(_ => GoToPage(PageIndex + 1), _ => PageIndex + 1 < PageCount && !IsExecuting);
        CancelCommand = new RelayCommand(_ => Cancel());
        BuildFilters();
    }

    public ObservableCollection<CategoryDefinition> Categories { get; }
    public ObservableCollection<CategoryDefinition> CategoryOptions { get; }
    public ObservableCollection<FilterOption> FilterOptions { get; }
    public ObservableCollection<VideoItem> VisibleVideos { get; }
    public ObservableCollection<SourceFolderItem> ImportedFolders { get; }
    public bool HasProject => _project is not null;
    public bool CanImport => HasProject && !IsBusy;
    public bool CanChangeProject => !IsBusy;
    public bool CanManageCategories => HasProject && !IsBusy;
    public bool HasLegacySettings => HasProject && _settings.Categories.Count > 0 && !IsBusy;
    public string ProjectName => _project?.Name ?? "尚未打开项目";
    public string ProjectFilePath => _projectFilePath;
    public string WindowTitle => HasProject ? $"{ProjectName} · 帧选 FramePick" : "帧选 FramePick";
    public string WorkspaceTitle => HasProject ? ProjectName : "视频工作台";
    public string FolderSummary => HasProject ? $"{ImportedFolders.Count} 个文件夹 · 分类与进度自动保存到项目" : "先创建或打开一个项目";
    public string EmptyTitle => HasProject ? "导入文件夹，开始整理" : "为你的视频创建一个项目";
    public string EmptyDescription => HasProject ? "每次导入一个文件夹，可累计添加；重复导入会刷新。" : "项目保存素材目录、分类与整理进度，随时打开继续。";
    public string InitialProjectDirectory => Path.GetDirectoryName(_settings.LastProjectPath) is { } directory && Directory.Exists(directory) ? directory
        : Environment.GetEnvironmentVariable("FRAMEPICK_PROJECT_DIRECTORY") is { } managed && Directory.Exists(managed) ? managed : Environment.CurrentDirectory;
    public string LastProjectPath => _settings.LastProjectPath;

    public RelayCommand OpenDetailCommand { get; }
    public RelayCommand CloseDetailCommand { get; }
    public RelayCommand ToggleDeleteCommand { get; }
    public RelayCommand PreviousDetailCommand { get; }
    public RelayCommand NextDetailCommand { get; }
    public RelayCommand PreviousPageCommand { get; }
    public RelayCommand NextPageCommand { get; }
    public RelayCommand CancelCommand { get; }
    public IReadOnlyList<string> SortOptions { get; } = new[] { "名称 A → Z", "名称 Z → A", "文件最大优先", "最近修改优先" };
    public string SearchText
    {
        get => _searchText;
        set { if (SetProperty(ref _searchText, value)) { PageIndex = 0; CloseDetail(); RefreshView(); } }
    }
    public string SelectedSort
    {
        get => _selectedSort;
        set { if (SetProperty(ref _selectedSort, value)) { PageIndex = 0; RefreshView(); } }
    }
    public string StatusText { get => _statusText; private set => SetProperty(ref _statusText, value); }
    public int TotalCount => _allVideos.Count;
    public int UndecidedCount => _allVideos.Count(v => !v.HasDecision);
    public int PreviewFailedCount => _allVideos.Count(v => v.HasPreviewError);
    public bool HasPreviewFailures => PreviewFailedCount > 0;
    public bool CanGeneratePreviews => !IsBusy && HasProject && ImportedFolders.Count > 0;
    public string PreviewLoadingText => $"正在生成 {PreviewFrameCount} 帧预览";
    public string DecisionSummary => $"待移动 {MoveCount} 项   ·   待删除 {DeleteCount} 项";
    public string DetailPosition => SelectedVideo is null ? "" : $"{_detailVideos.IndexOf(SelectedVideo) + 1} / {_detailVideos.Count}";
    public string InitialFolder => ImportedFolders.LastOrDefault(f => Directory.Exists(f.FullPath))?.FullPath ?? (HasProject ? Path.GetDirectoryName(ProjectFilePath)! : string.Empty);

    public string SourceFolder
    {
        get => _sourceFolder;
        private set
        {
            if (SetProperty(ref _sourceFolder, value)) OnPropertyChanged(nameof(FolderName));
        }
    }

    public string FolderName => string.IsNullOrEmpty(SourceFolder) ? string.Empty : Path.GetFileName(SourceFolder.TrimEnd(Path.DirectorySeparatorChar));

    public FilterOption? SelectedFilter
    {
        get => _selectedFilter;
        set
        {
            if (SetProperty(ref _selectedFilter, value) && value is not null)
            {
                CloseDetail();
                PageIndex = 0;
                RefreshView();
            }
        }
    }

    public VideoItem? SelectedVideo
    {
        get => _selectedVideo;
        private set
        {
            if (SetProperty(ref _selectedVideo, value))
            {
                PreviousDetailCommand.RaiseCanExecuteChanged();
                NextDetailCommand.RaiseCanExecuteChanged();
            }
        }
    }

    public bool IsDetailOpen
    {
        get => _isDetailOpen;
        private set => SetProperty(ref _isDetailOpen, value);
    }

    public bool IsBusy
    {
        get => _isBusy;
        private set
        {
            if (SetProperty(ref _isBusy, value))
            {
                OnPropertyChanged(nameof(ShowEmpty));
                OnPropertyChanged(nameof(CanExecute));
                OnPropertyChanged(nameof(CanGeneratePreviews));
                RefreshProjectProperties();
                PreviousPageCommand.RaiseCanExecuteChanged();
                NextPageCommand.RaiseCanExecuteChanged();
                OnPropertyChanged(nameof(CanManageCategories));
            }
        }
    }

    public bool IsExecuting
    {
        get => _isExecuting;
        private set
        {
            if (SetProperty(ref _isExecuting, value))
            {
                ToggleDeleteCommand.RaiseCanExecuteChanged();
                PreviousPageCommand.RaiseCanExecuteChanged();
                NextPageCommand.RaiseCanExecuteChanged();
                OnPropertyChanged(nameof(CanManageCategories));
            }
        }
    }

    public string OperationText
    {
        get => _operationText;
        private set => SetProperty(ref _operationText, value);
    }

    public double OperationProgress
    {
        get => _operationProgress;
        private set => SetProperty(ref _operationProgress, value);
    }

    public bool IsProgressIndeterminate
    {
        get => _isProgressIndeterminate;
        private set => SetProperty(ref _isProgressIndeterminate, value);
    }

    public int ExecuteCount => _allVideos.Count(v => v.HasDecision);
    public int DeleteCount => _allVideos.Count(v => v.IsMarkedForDeletion);
    public int MoveCount => _allVideos.Count(v => v.HasCategory && !v.IsMarkedForDeletion);
    public bool CanExecute => ExecuteCount > 0 && !IsBusy;
    public bool ShowEmpty => _allVideos.Count == 0 && !IsBusy;
    public bool HasSourceVideos => _allVideos.Count > 0;
    public bool HasFilteredVideos => _filteredVideos.Count > 0;
    public bool ShowNoResults => _allVideos.Count > 0 && _filteredVideos.Count == 0;
    public string CountText => $"{_filteredVideos.Count} 项";
    public int PageCount => Math.Max(1, (int)Math.Ceiling(_filteredVideos.Count / (double)PageSize));
    public string PageText => $"{Math.Min(PageIndex + 1, PageCount)} / {PageCount}";
    public bool HasMultiplePages => PageCount > 1;
    public int PreviewConcurrency => Math.Clamp(_project?.PreviewConcurrency ?? 2, 1, 4);
    public int PreviewFrameCount => _project?.PreviewFrameCount ?? 16;

    public int PageIndex
    {
        get => _pageIndex;
        private set
        {
            if (SetProperty(ref _pageIndex, value))
            {
                OnPropertyChanged(nameof(PageText));
                PreviousPageCommand.RaiseCanExecuteChanged();
                NextPageCommand.RaiseCanExecuteChanged();
            }
        }
    }

    public void CreateProject(string path)
    {
        if (IsBusy) throw new InvalidOperationException("请先停止当前任务，再切换项目。");
        FlushWorkspace(throwOnError: true);
        var project = new FramePickProject { Name = Path.GetFileNameWithoutExtension(path) };
        ProjectStore.Save(path, project, createNew: true);
        ActivateProject(project, path);
        StatusText = "项目已创建，导入一个文件夹开始整理";
    }

    public async Task OpenProjectAsync(string path)
    {
        if (IsBusy) throw new InvalidOperationException("请先停止当前任务，再切换项目。");
        var project = ProjectStore.Load(path);
        FlushWorkspace(throwOnError: true);
        ActivateProject(project, path);
        await RefreshProjectVideosAsync();
    }

    private void ActivateProject(FramePickProject project, string path)
    {
        _saveTimer.Stop();
        CloseDetail();
        ClearVideos();
        _project = project;
        _workspaceDirty = false;
        _projectFilePath = Path.GetFullPath(path);
        _restoring = true;
        Categories.Clear();
        foreach (var category in project.Categories) Categories.Add(category.Clone());
        RebuildCategoryOptions();
        ImportedFolders.Clear();
        foreach (var folder in project.SourceFolders) ImportedFolders.Add(new SourceFolderItem(folder));
        SourceFolder = project.SourceFolders.LastOrDefault() ?? string.Empty;
        SearchText = string.Empty;
        BuildFilters();
        SelectedFilter = FilterOptions.First();
        _restoring = false;
        _settings.LastProjectPath = _projectFilePath;
        try { StateStore.SaveSettings(_settings); }
        catch (IOException) { /* 项目独立保存，不依赖最近项目记录可写。 */ }
        catch (UnauthorizedAccessException) { }
        RefreshProjectProperties();
        RefreshDecisionProperties();
        RefreshView();
    }

    public async Task ImportFolderAsync(string folder)
    {
        if (!HasProject) throw new InvalidOperationException("请先创建或打开项目，再导入文件夹。");
        if (IsBusy) return;
        if (!Directory.Exists(folder)) throw new DirectoryNotFoundException("文件夹不存在或不可访问。");
        FlushWorkspace(throwOnError: true);
        var normalized = Path.TrimEndingDirectorySeparator(Path.GetFullPath(folder));
        if (!_project!.SourceFolders.Contains(normalized, StringComparer.OrdinalIgnoreCase))
        {
            _project.SourceFolders.Add(normalized);
            _workspaceDirty = true;
            try { FlushWorkspace(throwOnError: true); }
            catch { _project.SourceFolders.Remove(normalized); throw; }
            ImportedFolders.Add(new SourceFolderItem(normalized));
        }
        SourceFolder = normalized;
        SearchText = string.Empty;
        SelectedFilter = FilterOptions.First();
        RefreshProjectProperties();
        await RefreshProjectVideosAsync();
    }

    public async Task ImportLegacySettingsAsync()
    {
        if (!HasLegacySettings) return;
        ApplyCategories(_settings.Categories, _settings.PreviewConcurrency);
        var current = _project!.Decisions.ToDictionary(d => d.Path, StringComparer.OrdinalIgnoreCase);
        foreach (var folder in _project.SourceFolders)
            foreach (var decision in StateStore.LoadWorkspace(folder).Decisions)
                current.TryAdd(decision.Path, decision);
        _project.Decisions = current.Values.ToList();
        ProjectStore.Save(ProjectFilePath, _project);
        ClearVideos();
        await RefreshProjectVideosAsync();
    }

    private async Task RefreshProjectVideosAsync(bool forceRegenerate = false)
    {
        if (_project is null || IsBusy) return;
        CloseDetail();
        _forceRegenerating = forceRegenerate;

        IsBusy = true;
        IsProgressIndeterminate = true;
        OperationText = "扫描中";
        OperationProgress = 0;
        _workCancellation = new CancellationTokenSource();
        var token = _workCancellation.Token;

        try
        {
            var discovered = new Dictionary<string, VideoDescriptor>(StringComparer.OrdinalIgnoreCase);
            var unavailable = 0;
            foreach (var folder in _project.SourceFolders)
            {
                token.ThrowIfCancellationRequested();
                if (!Directory.Exists(folder)) { unavailable++; continue; }
                var progress = new Progress<int>(count => OperationText = $"扫描 {Path.GetFileName(folder)} · {count} 项");
                try
                {
                    foreach (var descriptor in await _scanner.ScanAsync(folder, progress, token))
                        discovered.TryAdd(descriptor.FullPath, descriptor);
                }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { unavailable++; }
            }
            FlushWorkspace(throwOnError: true);
            var decisions = _project.Decisions.ToDictionary(d => d.Path, StringComparer.OrdinalIgnoreCase);
            ClearVideos();

            _restoring = true;
            foreach (var descriptor in discovered.Values.OrderBy(d => d.FullPath, StringComparer.CurrentCultureIgnoreCase))
            {
                var item = new VideoItem(descriptor, StateStore.PreviewPathFor(descriptor.FullPath, descriptor.Length, descriptor.LastWriteTimeUtc, PreviewFrameCount));
                if (decisions.TryGetValue(item.FullPath, out var decision)
                    && (decision.Length is null || decision.Length == descriptor.Length)
                    && (decision.LastWriteTimeUtc is null || decision.LastWriteTimeUtc == descriptor.LastWriteTimeUtc))
                {
                    if (decision.Delete) item.IsMarkedForDeletion = true;
                    else if (Categories.Any(c => c.Id == decision.CategoryId)) item.CategoryId = decision.CategoryId;
                }
                item.CategoryName = Categories.FirstOrDefault(c => c.Id == item.CategoryId)?.Name ?? string.Empty;
                item.PropertyChanged += OnVideoPropertyChanged;
                _allVideos.Add(item);
            }
            _restoring = false;
            PageIndex = 0;
            RefreshDecisionProperties();
            RefreshView();
            await GeneratePreviewsAsync(token, forceRegenerate);
            StatusText = $"已载入 {TotalCount} 个视频" + (PreviewFailedCount > 0 ? $"，{PreviewFailedCount} 项预览失败，可重试" : "，分类决定自动保存");
            if (unavailable > 0) StatusText += $"；{unavailable} 个文件夹不可访问，已保留其整理进度";
        }
        catch (OperationCanceledException)
        {
            StatusText = "已停止生成预览；已完成的预览和分类已保留";
        }
        finally
        {
            _restoring = false;
            _forceRegenerating = false;
            foreach (var item in _allVideos.Where(v => v.IsPreviewLoading)) item.PreviewState = PreviewState.Waiting;
            _workCancellation?.Dispose();
            _workCancellation = null;
            IsProgressIndeterminate = false;
            IsBusy = false;
            OperationText = string.Empty;
            RefreshDecisionProperties();
            RefreshView();
        }
    }

    public async Task GeneratePreviewsAsync(CancellationToken cancellationToken = default, bool forceRegenerate = false)
    {
        if (_allVideos.Count == 0 || IsExecuting) return;
        if (IsBusy && _workCancellation is null) return;

        var ownCancellation = _workCancellation is null;
        if (ownCancellation)
        {
            _workCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            IsBusy = true;
        }
        var token = _workCancellation!.Token;
        IsProgressIndeterminate = false;
        OperationProgress = 0;
        var pending = _allVideos.Where(v => forceRegenerate || v.PreviewState != PreviewState.Ready).ToList();
        var frameCount = PreviewFrameCount;
        var total = pending.Count;
        var completed = 0;
        OperationText = $"预览 0/{total}";

        try
        {
            await Parallel.ForEachAsync(pending, new ParallelOptions
            {
                MaxDegreeOfParallelism = PreviewConcurrency,
                CancellationToken = token
            }, async (item, ct) =>
            {
                await _dispatcher.InvokeAsync(() =>
                {
                    item.Error = string.Empty;
                    item.PreviewState = PreviewState.Loading;
                });
                try
                {
                    var result = await _previewGenerator.GenerateAsync(item.Descriptor, item.PreviewPath, ct, frameCount, forceRegenerate);
                    await _dispatcher.InvokeAsync(() =>
                    {
                        if (result.Success)
                        {
                            item.DurationSeconds = result.DurationSeconds;
                            item.PreviewState = PreviewState.Ready;
                        }
                        else
                        {
                            item.Error = result.Error ?? "预览失败";
                            item.PreviewState = PreviewState.Failed;
                        }
                    });
                    if (result.Success)
                    {
                        var isVisible = await _dispatcher.InvokeAsync(() => VisibleVideos.Contains(item));
                        if (isVisible) await LoadPreviewImageAsync(item);
                        await _dispatcher.InvokeAsync(() =>
                        {
                            if (SelectedVideo == item) _ = LoadDetailImageAsync(item);
                        });
                    }
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception ex)
                {
                    await _dispatcher.InvokeAsync(() =>
                    {
                        item.Error = ex.Message;
                        item.PreviewState = PreviewState.Failed;
                    });
                }
                finally
                {
                    var done = Interlocked.Increment(ref completed);
                    await _dispatcher.InvokeAsync(() =>
                    {
                        OperationProgress = done * 100d / total;
                        OperationText = $"预览 {done}/{total}";
                    });
                }
            });
        }
        finally
        {
            foreach (var item in pending.Where(v => v.IsPreviewLoading)) item.PreviewState = PreviewState.Waiting;
            if (ownCancellation)
            {
                IsBusy = false;
                OperationText = string.Empty;
                _workCancellation.Dispose();
                _workCancellation = null;
            }
            RefreshView();
            RefreshDecisionProperties();
        }
    }

    public Task RetryPreviewsAsync() => RegeneratePreviewsAsync(false);

    public Task RegeneratePreviewsAsync(bool regenerateAll) => RefreshProjectVideosAsync(regenerateAll);

    public async Task<BatchResult> ExecuteAsync()
    {
        if (!CanExecute) return new BatchResult();
        CloseDetail();
        IsBusy = true;
        IsExecuting = true;
        IsProgressIndeterminate = false;
        OperationProgress = 0;
        var operations = _allVideos.Where(v => v.HasDecision).Select(v => new BatchOperation
        {
            VideoPath = v.FullPath,
            Delete = v.IsMarkedForDeletion,
            DestinationDirectory = v.IsMarkedForDeletion
                ? null
                : Categories.FirstOrDefault(c => c.Id == v.CategoryId)?.Destination
        }).ToList();
        OperationText = $"执行 0/{operations.Count}";
        _workCancellation = new CancellationTokenSource();
        var token = _workCancellation.Token;

        try
        {
            var progress = new Progress<BatchProgress>(p =>
            {
                OperationProgress = p.Total == 0 ? 0 : p.Completed * 100d / p.Total;
                OperationText = $"执行 {p.Completed}/{p.Total}";
            });
            var result = await _batchExecutor.ExecuteAsync(operations, progress, token);
            var succeeded = new HashSet<string>(result.SucceededPaths, StringComparer.OrdinalIgnoreCase);
            foreach (var item in _allVideos.Where(v => succeeded.Contains(v.FullPath)).ToList())
            {
                item.PropertyChanged -= OnVideoPropertyChanged;
                item.PreviewImage = null;
                item.DetailImage = null;
                _allVideos.Remove(item);
            }
            _project!.Decisions.RemoveAll(d => succeeded.Contains(d.Path));
            RefreshDecisionProperties();
            SaveWorkspace();
            FlushWorkspace();
            RefreshView();
            StatusText = $"{(result.Cancelled ? "已停止" : "整理完成")}：移动 {result.Moved} 项，删除 {result.Deleted} 项，失败 {result.Failed} 项";
            return result;
        }
        finally
        {
            IsBusy = false;
            IsExecuting = false;
            IsProgressIndeterminate = false;
            OperationText = string.Empty;
            _workCancellation.Dispose();
            _workCancellation = null;
            RefreshDecisionProperties();
        }
    }

    public void ApplyCategories(IEnumerable<CategoryDefinition> categories, int previewConcurrency)
        => ApplyProjectSettings(ProjectName, categories, previewConcurrency, PreviewFrameCount);

    public void ApplyProjectSettings(string name, IEnumerable<CategoryDefinition> categories, int previewConcurrency, int frameCount)
    {
        if (!HasProject) throw new InvalidOperationException("请先创建或打开项目。");
        if (IsBusy) throw new InvalidOperationException("请先停止当前任务，再修改项目设置。");
        name = name.Trim();
        if (name.Length == 0 || name.Length > 100) throw new ArgumentException("项目名称需为 1–100 个字符。", nameof(name));
        if (frameCount is not (16 or 25)) throw new ArgumentOutOfRangeException(nameof(frameCount));
        var frameCountChanged = frameCount != PreviewFrameCount;
        var list = categories.Select(c => c.Clone()).ToList();
        FlushWorkspace(throwOnError: true);
        var updatedProject = new FramePickProject
        {
            Id = _project!.Id,
            Name = name,
            SourceFolders = _project.SourceFolders.ToList(),
            Categories = list.Select(c => c.Clone()).ToList(),
            PreviewConcurrency = Math.Clamp(previewConcurrency, 1, 4),
            PreviewFrameCount = frameCount,
            Decisions = _project.Decisions
        };
        ProjectStore.Save(ProjectFilePath, updatedProject);
        _project = updatedProject;
        if (frameCountChanged) CloseDetail();
        _restoring = true;
        var validIds = list.Select(c => c.Id).ToHashSet();
        var decisions = _allVideos.ToDictionary(v => v, v => v.CategoryId);
        foreach (var item in _allVideos)
        {
            if (frameCountChanged) item.ChangePreviewPath(StateStore.PreviewPathFor(item.FullPath, item.Descriptor.Length, item.Descriptor.LastWriteTimeUtc, frameCount));
            if (item.CategoryId.Length > 0 && !validIds.Contains(item.CategoryId)) item.CategoryId = string.Empty;
            item.CategoryName = list.FirstOrDefault(c => c.Id == item.CategoryId)?.Name ?? string.Empty;
        }
        Categories.Clear();
        foreach (var category in list) Categories.Add(category);
        RebuildCategoryOptions();
        foreach (var (item, categoryId) in decisions)
        {
            item.CategoryId = validIds.Contains(categoryId) ? categoryId : string.Empty;
            item.CategoryName = list.FirstOrDefault(c => c.Id == item.CategoryId)?.Name ?? string.Empty;
        }
        _restoring = false;
        BuildFilters();
        SaveWorkspace();
        RefreshProjectProperties();
        RefreshDecisionProperties();
        RefreshView();
        StatusText = frameCountChanged ? $"项目设置已保存，点击“重新生成”生成 {frameCount} 帧预览" : "项目设置已保存";
    }

    public void Cancel()
    {
        _workCancellation?.Cancel();
        _detailLoadCancellation?.Cancel();
    }

    private void OpenDetail(VideoItem? item)
    {
        if (item is null || IsExecuting) return;
        if (!IsDetailOpen)
        {
            _detailVideos.Clear();
            _detailVideos.AddRange(_filteredVideos);
        }
        _detailLoadCancellation?.Cancel();
        if (SelectedVideo is not null) SelectedVideo.DetailImage = null;
        SelectedVideo = item;
        IsDetailOpen = true;
        OnPropertyChanged(nameof(DetailPosition));
        _ = LoadDetailImageAsync(item);
    }

    private void CloseDetail()
    {
        _detailLoadCancellation?.Cancel();
        if (SelectedVideo is not null) SelectedVideo.DetailImage = null;
        IsDetailOpen = false;
        SelectedVideo = null;
    }

    private bool CanMoveDetail(int delta)
    {
        if (SelectedVideo is null || _detailVideos.Count == 0) return false;
        var index = _detailVideos.IndexOf(SelectedVideo);
        return index >= 0 && index + delta >= 0 && index + delta < _detailVideos.Count;
    }

    private void MoveDetail(int delta)
    {
        if (!CanMoveDetail(delta) || SelectedVideo is null) return;
        var index = _detailVideos.IndexOf(SelectedVideo) + delta;
        OpenDetail(_detailVideos[index]);
    }

    private async Task LoadDetailImageAsync(VideoItem item)
    {
        _detailLoadCancellation?.Cancel();
        _detailLoadCancellation?.Dispose();
        _detailLoadCancellation = new CancellationTokenSource();
        var token = _detailLoadCancellation.Token;
        try
        {
            var image = await ImageLoader.LoadAsync(item.PreviewPath, 1600, token);
            if (token.IsCancellationRequested || SelectedVideo != item) return;
            item.DetailImage = image;
        }
        catch (OperationCanceledException) { }
    }

    private void GoToPage(int page)
    {
        if (page < 0 || page >= PageCount || IsExecuting) return;
        PageIndex = page;
        RefreshView();
    }

    private void RefreshView()
    {
        if (!_dispatcher.CheckAccess())
        {
            _ = _dispatcher.InvokeAsync(RefreshView);
            return;
        }

        var key = SelectedFilter?.Key ?? "all";
        IEnumerable<VideoItem> query = key switch
        {
            "deleted" => _allVideos.Where(v => v.IsMarkedForDeletion).ToList(),
            "uncategorized" => _allVideos.Where(v => !v.IsMarkedForDeletion && !v.HasCategory).ToList(),
            _ when key.StartsWith("category:", StringComparison.Ordinal) => _allVideos.Where(v => v.CategoryId == key[9..]).ToList(),
            _ => _allVideos.ToList()
        };
        if (!string.IsNullOrWhiteSpace(SearchText))
            query = query.Where(v => v.FileName.Contains(SearchText.Trim(), StringComparison.CurrentCultureIgnoreCase));
        query = SelectedSort switch
        {
            "名称 Z → A" => query.OrderByDescending(v => v.FileName, StringComparer.CurrentCultureIgnoreCase),
            "文件最大优先" => query.OrderByDescending(v => v.Descriptor.Length),
            "最近修改优先" => query.OrderByDescending(v => v.Descriptor.LastWriteTimeUtc),
            _ => query.OrderBy(v => v.FileName, StringComparer.CurrentCultureIgnoreCase)
        };
        _filteredVideos = query.ToList();
        PageIndex = Math.Min(PageIndex, PageCount - 1);
        var page = _filteredVideos.Skip(PageIndex * PageSize).Take(PageSize).ToList();
        foreach (var old in VisibleVideos.Where(v => !page.Contains(v))) old.PreviewImage = null;
        if (!VisibleVideos.SequenceEqual(page))
        {
            VisibleVideos.Clear();
            foreach (var item in page) VisibleVideos.Add(item);
        }
        foreach (var item in page)
        {
            _ = LoadPreviewImageAsync(item);
        }
        OnPropertyChanged(nameof(CountText));
        OnPropertyChanged(nameof(PageCount));
        OnPropertyChanged(nameof(PageText));
        OnPropertyChanged(nameof(HasMultiplePages));
        OnPropertyChanged(nameof(HasSourceVideos));
        OnPropertyChanged(nameof(HasFilteredVideos));
        OnPropertyChanged(nameof(ShowNoResults));
        OnPropertyChanged(nameof(ShowEmpty));
        PreviousPageCommand.RaiseCanExecuteChanged();
        NextPageCommand.RaiseCanExecuteChanged();
    }

    private async Task LoadPreviewImageAsync(VideoItem item)
    {
        if (_forceRegenerating && item.PreviewState != PreviewState.Ready) return;
        if (!File.Exists(item.PreviewPath) || item.PreviewImage is not null || !_imageLoads.TryAdd(item.FullPath, 0)) return;
        var previewPath = item.PreviewPath;
        try
        {
            var image = await ImageLoader.LoadAsync(previewPath);
            await _dispatcher.InvokeAsync(() =>
            {
                if (item.PreviewPath != previewPath) return;
                if (image is not null && VisibleVideos.Contains(item))
                {
                    item.PreviewImage = image;
                    item.PreviewState = PreviewState.Ready;
                }
                else if (image is null && item.PreviewState == PreviewState.Ready)
                {
                    item.Error = "预览缓存无法读取，请点击“重新生成”并选择“只补充”";
                    item.PreviewState = PreviewState.Failed;
                }
            });
        }
        finally
        {
            _imageLoads.TryRemove(item.FullPath, out _);
        }
    }

    private void OnVideoPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (_restoring || sender is not VideoItem item) return;
        if (e.PropertyName == nameof(VideoItem.PreviewState))
        {
            OnPropertyChanged(nameof(CanGeneratePreviews));
            OnPropertyChanged(nameof(PreviewFailedCount));
            OnPropertyChanged(nameof(HasPreviewFailures));
        }
        if (e.PropertyName is nameof(VideoItem.CategoryId) or nameof(VideoItem.IsMarkedForDeletion))
        {
            item.CategoryName = Categories.FirstOrDefault(c => c.Id == item.CategoryId)?.Name ?? string.Empty;
            RefreshDecisionProperties();
            SaveWorkspace();
            if (SelectedFilter?.Key is not (null or "all") && !_refreshQueued)
            {
                _refreshQueued = true;
                _ = _dispatcher.BeginInvoke(() =>
                {
                    _refreshQueued = false;
                    RefreshView();
                });
            }
        }
    }

    private void RefreshDecisionProperties()
    {
        OnPropertyChanged(nameof(ExecuteCount));
        OnPropertyChanged(nameof(DeleteCount));
        OnPropertyChanged(nameof(MoveCount));
        OnPropertyChanged(nameof(CanExecute));
        OnPropertyChanged(nameof(TotalCount));
        OnPropertyChanged(nameof(UndecidedCount));
        OnPropertyChanged(nameof(DecisionSummary));
        OnPropertyChanged(nameof(PreviewFailedCount));
        OnPropertyChanged(nameof(HasPreviewFailures));
        OnPropertyChanged(nameof(CanGeneratePreviews));
    }

    private void RebuildCategoryOptions()
    {
        CategoryOptions.Clear();
        CategoryOptions.Add(new CategoryDefinition { Id = string.Empty, Name = "未分类" });
        foreach (var category in Categories) CategoryOptions.Add(category);
    }

    private void BuildFilters()
    {
        var previous = SelectedFilter?.Key ?? "all";
        FilterOptions.Clear();
        FilterOptions.Add(new FilterOption("all", "全部"));
        FilterOptions.Add(new FilterOption("uncategorized", "未分类"));
        FilterOptions.Add(new FilterOption("deleted", "待删除"));
        foreach (var category in Categories) FilterOptions.Add(new FilterOption($"category:{category.Id}", category.Name));
        SelectedFilter = FilterOptions.FirstOrDefault(f => f.Key == previous) ?? FilterOptions[0];
    }

    private void SaveWorkspace()
    {
        if (!_restoring && HasProject) { _workspaceDirty = true; _saveTimer.Stop(); _saveTimer.Start(); }
    }

    public void FlushWorkspace(bool throwOnError = false)
    {
        _saveTimer.Stop();
        if (_restoring || _project is null || !_workspaceDirty) return;
        try
        {
            var present = _allVideos.Select(v => v.FullPath).ToHashSet(StringComparer.OrdinalIgnoreCase);
            var saved = _project.Decisions.Where(d => !present.Contains(d.Path)).ToList();
            saved.AddRange(_allVideos.Where(v => v.HasDecision).Select(v => new VideoDecision
            {
                Path = v.FullPath, CategoryId = v.CategoryId, Delete = v.IsMarkedForDeletion,
                Length = v.Descriptor.Length, LastWriteTimeUtc = v.Descriptor.LastWriteTimeUtc
            }));
            _project.Decisions = saved;
            ProjectStore.Save(ProjectFilePath, _project);
            _workspaceDirty = false;
        }
        catch (Exception ex)
        {
            StatusText = $"项目保存失败：{ex.Message}";
            if (throwOnError) throw;
        }
    }

    private void RefreshProjectProperties()
    {
        foreach (var property in new[] { nameof(HasProject), nameof(CanImport), nameof(CanChangeProject), nameof(CanManageCategories),
            nameof(HasLegacySettings), nameof(ProjectName), nameof(ProjectFilePath), nameof(WindowTitle), nameof(WorkspaceTitle),
            nameof(FolderSummary), nameof(EmptyTitle), nameof(EmptyDescription), nameof(PreviewConcurrency), nameof(PreviewFrameCount),
            nameof(PreviewLoadingText), nameof(CanGeneratePreviews) }) OnPropertyChanged(property);
    }

    private void ClearVideos()
    {
        foreach (var item in _allVideos)
        {
            item.PropertyChanged -= OnVideoPropertyChanged;
            item.PreviewImage = null;
            item.DetailImage = null;
        }
        _allVideos.Clear();
        _filteredVideos.Clear();
        VisibleVideos.Clear();
        PageIndex = 0;
    }
}
