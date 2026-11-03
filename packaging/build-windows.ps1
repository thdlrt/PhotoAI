[CmdletBinding()]
param(
    [switch]$SkipDesktop,
    [switch]$SkipNpmInstall,
    [switch]$NoBundle,
    [switch]$Quick,
    [switch]$WithMediaTools,
    [string]$MediaToolsSource
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$BuildRoot = Join-Path $ProjectRoot 'packaging\build'
$CoreDistRoot = Join-Path $ProjectRoot 'packaging\dist\core'
$StageRoot = Join-Path $ProjectRoot 'packaging\stage'
$ServiceRoot = Join-Path $CoreDistRoot 'PhotoAI.Service'
$WorkerRoot = Join-Path $CoreDistRoot 'PhotoAI.CoreWorker'
$ResourceSource = Join-Path $ProjectRoot 'packaging\resources'
$ExifToolSource = Join-Path $ProjectRoot 'packaging\vendor\exiftool-13.59'
$ExifToolExe = Join-Path $ExifToolSource 'exiftool-13.59_64\exiftool.exe'
$LightroomSource = Join-Path $ProjectRoot 'integrations\photo-ai-lightroom.lrplugin'
if (-not $MediaToolsSource) {
    $MediaToolsSource = Join-Path $ProjectRoot 'packaging\vendor\ffmpeg-8.0.1'
}

# Release inputs are intentionally pinned here as well as in the staged
# manifest. A different builder-local executable must be reviewed explicitly.
$ExpectedUvVersion = 'uv 0.11.2 (02036a8ba 2026-03-26 x86_64-pc-windows-msvc)'
$ExpectedUvSha256 = '0548e585d7030d3be5e23cbf32da0d510a4225d15b72c28b15cc7ee7187dbb59'
$ExpectedExifToolSha256 = '68c079c32fdae0d6c7130e9a5fb73f8ac9dabdf9ab8da312da4f6c549d6d3385'
$ExpectedFfmpegSha256 = '74db6c184a03dba2bdfe23e1a1f41cf5a8385bc1de6a7a1b26db1dc541abef93'
$ExpectedFfprobeSha256 = '55bb6c6289367ae2383efa86b26bf2596f8adb72ac747360eb13df162354161c'
$ProductVersion = '0.10.0-beta.2'
$PythonPackageVersion = '0.10.0b2'

function Reset-OwnedDirectory {
    param(
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][string]$AllowedRoot
    )

    $AbsoluteTarget = [IO.Path]::GetFullPath($Target)
    $AbsoluteRoot = [IO.Path]::GetFullPath($AllowedRoot).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    if (-not $AbsoluteTarget.StartsWith(
        $AbsoluteRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "拒绝清理构建范围外的路径：$AbsoluteTarget"
    }
    if (Test-Path -LiteralPath $AbsoluteTarget) {
        Remove-Item -LiteralPath $AbsoluteTarget -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $AbsoluteTarget | Out-Null
}

function Assert-FileHash {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Expected,
        [Parameter(Mandatory)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "缺少已审核的 $Label：$Path"
    }
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected.ToLowerInvariant()) {
        throw "$Label SHA-256 不匹配。预期 $Expected，实际 $Actual"
    }
}

function Write-HashSidecar {
    param([Parameter(Mandatory)][string]$Path)

    $Item = Get-Item -LiteralPath $Path
    $Hash = (Get-FileHash -LiteralPath $Item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath ($Item.FullName + '.sha256') -Value "$Hash *$($Item.Name)" -Encoding ascii
    return $Hash
}

function Copy-TreeContents {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw '构建环境缺少项目 Python；它只属于发布构建机，不是最终用户依赖。'
}

& $Python -c 'import PyInstaller' 2>$null
if ($LASTEXITCODE -ne 0) {
    throw '构建环境缺少固定版本 PyInstaller；请运行 uv sync --extra dev。'
}

$UvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $UvCommand) {
    throw '构建机缺少固定版本 uv.exe。'
}
$Uv = $UvCommand.Source
$UvVersion = (& $Uv --version).Trim()
if ($UvVersion -ne $ExpectedUvVersion) {
    throw "uv 构建版本不匹配。预期 $ExpectedUvVersion，实际 $UvVersion"
}
Assert-FileHash -Path $Uv -Expected $ExpectedUvSha256 -Label 'uv.exe'
Assert-FileHash -Path $ExifToolExe -Expected $ExpectedExifToolSha256 -Label 'ExifTool 13.59'
if ($WithMediaTools) {
    Assert-FileHash -Path (Join-Path $MediaToolsSource 'ffmpeg.exe') -Expected $ExpectedFfmpegSha256 -Label 'FFmpeg 8.0.1 static'
    Assert-FileHash -Path (Join-Path $MediaToolsSource 'ffprobe.exe') -Expected $ExpectedFfprobeSha256 -Label 'FFprobe 8.0.1 static'
    foreach ($Notice in @('LICENSE', 'README.txt', 'manifest.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $MediaToolsSource $Notice) -PathType Leaf)) {
            throw "缺少 FFmpeg 来源或许可文件：$Notice"
        }
    }
    & $Python (Join-Path $PSScriptRoot 'verify-media-tools.py') --tools $MediaToolsSource
    if ($LASTEXITCODE -ne 0) {
        throw '便携媒体工具隔离验证失败。'
    }
}

foreach ($Required in @(
    (Join-Path $ResourceSource 'ai-requirements.lock'),
    (Join-Path $ResourceSource 'model-manifest.json'),
    (Join-Path $ResourceSource 'wheels'),
    $LightroomSource
)) {
    if (-not (Test-Path -LiteralPath $Required)) {
        throw "缺少发布资源：$Required"
    }
}

$DesktopConfig = Get-Content -LiteralPath (Join-Path $ProjectRoot 'desktop\src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
$NpmPackage = Get-Content -LiteralPath (Join-Path $ProjectRoot 'desktop\package.json') -Raw | ConvertFrom-Json
$PythonVersion = & $Python -c "from landscape_culler.version import PRODUCT_VERSION; print(PRODUCT_VERSION)"
$ApiVersion = & $Python -c "from landscape_culler.version import API_VERSION; print(API_VERSION)"
$ServiceProtocol = & $Python -c "from landscape_culler.version import SERVICE_PROTOCOL; print(SERVICE_PROTOCOL)"
$WorkerProtocol = & $Python -c "from landscape_culler.version import WORKER_PROTOCOL; print(WORKER_PROTOCOL)"
$AiEngineVersion = & $Python -c "from landscape_culler.version import AI_ENGINE_VERSION; print(AI_ENGINE_VERSION)"
$ManagedPythonVersion = & $Python -c "from landscape_culler.version import PYTHON_RUNTIME_VERSION; print(PYTHON_RUNTIME_VERSION)"
$ProjectMetadataVersion = & $Python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
if ($DesktopConfig.version -ne $ProductVersion -or $NpmPackage.version -ne $ProductVersion -or $PythonVersion.Trim() -ne $ProductVersion) {
    throw 'Tauri、npm 与 Python 产品版本不一致。'
}
if ($ProjectMetadataVersion.Trim() -ne $PythonPackageVersion) {
    throw "Python 包版本不一致：$($ProjectMetadataVersion.Trim())"
}

if ($Quick) {
    New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
    $QuickWheelRoot = Join-Path $BuildRoot 'ai-worker'
    Reset-OwnedDirectory -Target $QuickWheelRoot -AllowedRoot $BuildRoot
} else {
    Reset-OwnedDirectory -Target $BuildRoot -AllowedRoot (Join-Path $ProjectRoot 'packaging')
}
Reset-OwnedDirectory -Target $CoreDistRoot -AllowedRoot (Join-Path $ProjectRoot 'packaging\dist')
Reset-OwnedDirectory -Target $StageRoot -AllowedRoot (Join-Path $ProjectRoot 'packaging')

Push-Location $ProjectRoot
try {
    $PyInstallerArguments = @(
        '-m', 'PyInstaller',
        '--noconfirm',
        '--workpath', (Join-Path $BuildRoot 'pyinstaller'),
        '--distpath', $CoreDistRoot
    )
    if (-not $Quick) {
        $PyInstallerArguments += '--clean'
    }
    $PyInstallerArguments += (Join-Path $PSScriptRoot 'PhotoAI.spec')
    & $Python @PyInstallerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "轻量 sidecar 构建失败，退出码：$LASTEXITCODE"
    }

    $WheelBuildRoot = Join-Path $BuildRoot 'ai-worker'
    New-Item -ItemType Directory -Force -Path $WheelBuildRoot | Out-Null
    & $Uv build --wheel --out-dir $WheelBuildRoot $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "AI Worker wheel 构建失败，退出码：$LASTEXITCODE"
    }
} finally {
    Pop-Location
}

foreach ($Output in @(
    @{ Root = $ServiceRoot; Exe = 'PhotoAI.Service.exe' },
    @{ Root = $ServiceRoot; Exe = 'PhotoToolbox.CLI.exe' },
    @{ Root = $WorkerRoot; Exe = 'PhotoAI.CoreWorker.exe' }
)) {
    $Executable = Join-Path $Output.Root $Output.Exe
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "PyInstaller 输出不完整：$Executable"
    }
    Write-HashSidecar -Path $Executable | Out-Null
}

$BuiltWheels = @(Get-ChildItem -LiteralPath (Join-Path $BuildRoot 'ai-worker') -Filter '*.whl' -File)
if ($BuiltWheels.Count -ne 1) {
    throw "AI Worker 必须生成且只生成一个 wheel，实际为 $($BuiltWheels.Count) 个。"
}
if ($BuiltWheels[0].Name -ne "landscape_ai_culler-$PythonPackageVersion-py3-none-any.whl") {
    throw "AI Worker wheel 名称或版本不符合发布约定：$($BuiltWheels[0].Name)"
}

$StageTools = Join-Path $StageRoot 'tools'
$StageIntegrations = Join-Path $StageRoot 'integrations'
$StageManifests = Join-Path $StageRoot 'manifests'
$StageWheels = Join-Path $StageManifests 'wheels'
$StageAiWorker = Join-Path $StageManifests 'ai-worker'
foreach ($Directory in @($StageTools, $StageIntegrations, $StageManifests, $StageWheels, $StageAiWorker)) {
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
}

$StageUv = Join-Path $StageTools 'uv.exe'
& (Join-Path $PSScriptRoot 'build-framepick.ps1') -Destination (Join-Path $StageTools 'framepick')
if ($LASTEXITCODE -ne 0) { throw '帧选组件构建失败。' }
Copy-Item -LiteralPath $Uv -Destination $StageUv -Force
Write-HashSidecar -Path $StageUv | Out-Null
Copy-Item -LiteralPath $ExifToolSource -Destination $StageTools -Recurse -Force
$StageExifTool = Join-Path $StageTools 'exiftool-13.59\exiftool-13.59_64\exiftool.exe'
Assert-FileHash -Path $StageExifTool -Expected $ExpectedExifToolSha256 -Label 'staged ExifTool 13.59'
Write-HashSidecar -Path $StageExifTool | Out-Null
if ($WithMediaTools) {
    $StageMediaTools = Join-Path $StageTools 'ffmpeg'
    Copy-TreeContents -Source $MediaToolsSource -Destination $StageMediaTools
    Assert-FileHash -Path (Join-Path $StageMediaTools 'ffmpeg.exe') -Expected $ExpectedFfmpegSha256 -Label 'staged FFmpeg'
    Assert-FileHash -Path (Join-Path $StageMediaTools 'ffprobe.exe') -Expected $ExpectedFfprobeSha256 -Label 'staged FFprobe'
    Write-HashSidecar -Path (Join-Path $StageMediaTools 'ffmpeg.exe') | Out-Null
    Write-HashSidecar -Path (Join-Path $StageMediaTools 'ffprobe.exe') | Out-Null
}

$StagePlugin = Join-Path $StageIntegrations 'photo-ai-lightroom.lrplugin'
New-Item -ItemType Directory -Force -Path $StagePlugin | Out-Null
Get-ChildItem -LiteralPath $LightroomSource -File | Where-Object {
    $_.Name -ne 'bridge-path.txt'
} | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $StagePlugin -Force
}
if (Test-Path -LiteralPath (Join-Path $StagePlugin 'bridge-path.txt')) {
    throw 'Lightroom 发布模板不得携带开发机 bridge-path.txt。'
}

Copy-Item -LiteralPath (Join-Path $ResourceSource 'ai-requirements.lock') -Destination $StageManifests -Force
Copy-Item -LiteralPath (Join-Path $ResourceSource 'model-manifest.json') -Destination $StageManifests -Force
Copy-TreeContents -Source (Join-Path $ResourceSource 'wheels') -Destination $StageWheels
$StageWorkerWheel = Join-Path $StageAiWorker $BuiltWheels[0].Name
Copy-Item -LiteralPath $BuiltWheels[0].FullName -Destination $StageWorkerWheel -Force
Write-HashSidecar -Path $StageWorkerWheel | Out-Null

$ResourceFiles = @(Get-ChildItem -LiteralPath $StageRoot -Recurse -File | Sort-Object FullName)
$ResourceInventory = @($ResourceFiles | ForEach-Object {
    [ordered]@{
        path = [IO.Path]::GetRelativePath($StageRoot, $_.FullName).Replace('\', '/')
        size = $_.Length
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
})
$WorkerCodeVersion = & $Python -c 'from landscape_culler.constants import AI_WORKER_CODE_VERSION; print(AI_WORKER_CODE_VERSION)'
if ($LASTEXITCODE -ne 0) { throw '无法读取工作组件版本。' }
$ReleaseManifest = [ordered]@{
    schema_version = 1
    product = 'PhotoAI'
    product_name = '摄影后期工具箱'
    version = $ProductVersion
    python_package_version = $PythonPackageVersion
    worker_code_version = $WorkerCodeVersion.Trim()
    api_version = $ApiVersion.Trim()
    service_protocol = $ServiceProtocol.Trim()
    worker_protocol = $WorkerProtocol.Trim()
    ai_engine_version = $AiEngineVersion.Trim()
    managed_python_version = $ManagedPythonVersion.Trim()
    built_at = [DateTimeOffset]::UtcNow.ToString('o')
    architecture = 'x86_64-pc-windows-msvc'
    offline_base_install = $true
    model_weights_included = $false
    ai_environment_installed_post_setup = $true
    media_tools_included = [bool]$WithMediaTools
    framepick_included = $true
    framepick_version = '1.2.0'
    resources = $ResourceInventory
}
$ReleaseManifestPath = Join-Path $StageManifests 'release-manifest.json'
$ReleaseManifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReleaseManifestPath -Encoding utf8
Write-HashSidecar -Path $ReleaseManifestPath | Out-Null

& $Python (Join-Path $PSScriptRoot 'verify-release.py') `
    --service-dir $ServiceRoot `
    --worker-dir $WorkerRoot `
    --stage $StageRoot `
    --project-root $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    throw "正式发布门禁失败，退出码：$LASTEXITCODE"
}

if (-not $SkipDesktop) {
    $DesktopArguments = @{
        ServiceOnedir = $ServiceRoot
        CoreWorkerOnedir = $WorkerRoot
        ResourceStage = $StageRoot
        SkipNpmInstall = ($SkipNpmInstall -or $Quick)
        NoBundle = $NoBundle
        WithMediaTools = $WithMediaTools
    }
    & (Join-Path $PSScriptRoot 'build-desktop.ps1') @DesktopArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri/NSIS 构建失败，退出码：$LASTEXITCODE"
    }
}

Write-Host "轻量 Service：$ServiceRoot"
Write-Host "轻量 CoreWorker：$WorkerRoot"
Write-Host "受审核基础资源：$StageRoot"
