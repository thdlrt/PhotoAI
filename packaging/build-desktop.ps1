[CmdletBinding()]
param(
    [string]$ServiceOnedir,
    [string]$CoreWorkerOnedir,
    [string]$ResourceStage,
    [switch]$SkipNpmInstall,
    [switch]$NoBundle
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$DesktopRoot = Join-Path $ProjectRoot 'desktop'
$TauriRoot = Join-Path $DesktopRoot 'src-tauri'
$CoreResourceRoot = Join-Path $TauriRoot 'resources\core'

if (-not $ServiceOnedir) {
    $ServiceOnedir = Join-Path $ProjectRoot 'packaging\dist\core\PhotoAI.Service'
}
if (-not $CoreWorkerOnedir) {
    $CoreWorkerOnedir = Join-Path $ProjectRoot 'packaging\dist\core\PhotoAI.CoreWorker'
}
if (-not $ResourceStage) {
    $ResourceStage = Join-Path $ProjectRoot 'packaging\stage'
}

function Resolve-Onedir {
    param(
        [Parameter(Mandatory)][string]$Candidate,
        [Parameter(Mandatory)][string]$ExecutableName
    )

    if (-not (Test-Path -LiteralPath $Candidate)) {
        throw "缺少 $ExecutableName 的 onedir 目录：$Candidate"
    }
    $Resolved = (Resolve-Path -LiteralPath $Candidate).Path
    if (Test-Path -LiteralPath $Resolved -PathType Leaf) {
        if ((Split-Path -Leaf $Resolved) -ne $ExecutableName) {
            throw "sidecar 文件名必须为 $ExecutableName：$Resolved"
        }
        return Split-Path -Parent $Resolved
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Resolved $ExecutableName) -PathType Leaf)) {
        throw "onedir 根目录中缺少 $ExecutableName：$Resolved"
    }
    return $Resolved
}

function Assert-CoreBoundary {
    param([Parameter(Mandatory)][string]$Directory)

    $Forbidden = '(?i)(^|[\\/])(torch|torchvision|transformers|pyiqa|opencv|cv2|open(?:color)?io|pyopencolorio|pyvips|rawpy|ollama|models?|huggingface)([\\/.]|$)'
    $Unexpected = Get-ChildItem -LiteralPath $Directory -Recurse -Force |
        Where-Object { $_.FullName.Substring($Directory.Length) -match $Forbidden } |
        Select-Object -First 1
    if ($Unexpected) {
        throw "轻量 sidecar 意外包含 AI/图像推理依赖：$($Unexpected.FullName)"
    }
}

function Reset-StagingDirectory {
    param([Parameter(Mandatory)][string]$Target)

    $ExpectedParent = (Resolve-Path -LiteralPath $CoreResourceRoot).Path
    $AbsoluteTarget = [IO.Path]::GetFullPath($Target)
    if (-not $AbsoluteTarget.StartsWith($ExpectedParent + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理意外的 staging 路径：$AbsoluteTarget"
    }
    if (Test-Path -LiteralPath $AbsoluteTarget) {
        Remove-Item -LiteralPath $AbsoluteTarget -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $AbsoluteTarget | Out-Null
}

function Copy-DirectoryContents {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )

    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
}

foreach ($Tool in @('node', 'npm', 'cargo', 'rustc')) {
    if (-not (Get-Command $Tool -ErrorAction SilentlyContinue)) {
        throw "发布构建机缺少 $Tool；它不是最终用户依赖。"
    }
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw '首版仅构建 Windows x64 安装器。'
}
$RustHost = (& rustc -vV | Select-String '^host:' | ForEach-Object { $_.Line.Split(':', 2)[1].Trim() })
if ($RustHost -ne 'x86_64-pc-windows-msvc') {
    throw "发布构建目标必须为 x86_64-pc-windows-msvc，当前为 $RustHost"
}

$ServiceSource = Resolve-Onedir -Candidate $ServiceOnedir -ExecutableName 'PhotoAI.Service.exe'
$WorkerSource = Resolve-Onedir -Candidate $CoreWorkerOnedir -ExecutableName 'PhotoAI.CoreWorker.exe'
Assert-CoreBoundary -Directory $ServiceSource
Assert-CoreBoundary -Directory $WorkerSource

$ServiceTarget = Join-Path $CoreResourceRoot 'service'
$WorkerTarget = Join-Path $CoreResourceRoot 'worker'
Reset-StagingDirectory -Target $ServiceTarget
Reset-StagingDirectory -Target $WorkerTarget
Copy-DirectoryContents -Source $ServiceSource -Destination $ServiceTarget
Copy-DirectoryContents -Source $WorkerSource -Destination $WorkerTarget

foreach ($Name in @('tools', 'integrations', 'manifests')) {
    $Source = Join-Path $ResourceStage $Name
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "正式安装器缺少已审核的 $Name 资源目录：$Source"
    }
    if (-not (Get-ChildItem -LiteralPath $Source -Recurse -File | Select-Object -First 1)) {
        throw "正式安装器的 $Name 资源目录为空：$Source"
    }

    $Destination = Join-Path $TauriRoot "resources\$Name"
    $ResolvedResources = (Resolve-Path -LiteralPath (Join-Path $TauriRoot 'resources')).Path
    $AbsoluteDestination = [IO.Path]::GetFullPath($Destination)
    if (-not $AbsoluteDestination.StartsWith($ResolvedResources + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理意外的资源 staging 路径：$AbsoluteDestination"
    }
    if (Test-Path -LiteralPath $AbsoluteDestination) {
        Remove-Item -LiteralPath $AbsoluteDestination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $AbsoluteDestination | Out-Null
    Copy-DirectoryContents -Source $Source -Destination $AbsoluteDestination
}

# Tauri copies resources into target/release but does not remove resources
# deleted or renamed since the previous build. Clear only the four owned
# payload directories so a development-machine path or obsolete plug-in
# template cannot survive into a later installer.
$ReleasePayloadRoot = Join-Path $TauriRoot 'target\release'
if (Test-Path -LiteralPath $ReleasePayloadRoot -PathType Container) {
    $ResolvedReleasePayload = (Resolve-Path -LiteralPath $ReleasePayloadRoot).Path
    foreach ($Name in @('core', 'tools', 'integrations', 'manifests')) {
        $StalePayload = [IO.Path]::GetFullPath((Join-Path $ResolvedReleasePayload $Name))
        if (-not $StalePayload.StartsWith(
            $ResolvedReleasePayload + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "拒绝清理意外的 Tauri 载荷路径：$StalePayload"
        }
        if (Test-Path -LiteralPath $StalePayload) {
            Remove-Item -LiteralPath $StalePayload -Recurse -Force
        }
    }
}

$Config = Get-Content -LiteralPath (Join-Path $TauriRoot 'tauri.conf.json') -Raw | ConvertFrom-Json
$Package = Get-Content -LiteralPath (Join-Path $DesktopRoot 'package.json') -Raw | ConvertFrom-Json
$CargoVersionMatch = Select-String -LiteralPath (Join-Path $TauriRoot 'Cargo.toml') -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $CargoVersionMatch) {
    throw '无法读取桌面 Cargo 版本。'
}
$CargoVersion = $CargoVersionMatch.Matches[0].Groups[1].Value
if ($Config.productName -ne '照片选片' -or $Config.mainBinaryName -ne 'PhotoAI') {
    throw 'Tauri 产品名或主程序名已偏离发布约定。'
}
if ($Config.version -ne $Package.version -or $Config.version -ne $CargoVersion) {
    throw "桌面版本不一致：Tauri=$($Config.version)，npm=$($Package.version)，Cargo=$CargoVersion"
}
if ($Config.bundle.windows.nsis.installMode -ne 'currentUser') {
    throw 'NSIS 必须保持当前用户安装模式。'
}
if ($Config.bundle.windows.webviewInstallMode.type -ne 'offlineInstaller') {
    throw '基础安装器必须携带离线 WebView2。'
}

$NsisDirectory = Join-Path $TauriRoot 'target\release\bundle\nsis'
$FinalInstallerPath = Join-Path $NsisDirectory 'PhotoAI-Setup-x64.exe'
if (-not $NoBundle -and (Test-Path -LiteralPath $NsisDirectory -PathType Container)) {
    $ResolvedNsis = (Resolve-Path -LiteralPath $NsisDirectory).Path
    foreach ($OldArtifact in @($FinalInstallerPath, ($FinalInstallerPath + '.sha256'))) {
        $AbsoluteArtifact = [IO.Path]::GetFullPath($OldArtifact)
        if (-not $AbsoluteArtifact.StartsWith($ResolvedNsis + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "拒绝清理意外的安装器路径：$AbsoluteArtifact"
        }
        if (Test-Path -LiteralPath $AbsoluteArtifact -PathType Leaf) {
            Remove-Item -LiteralPath $AbsoluteArtifact -Force
        }
    }
}

Push-Location $DesktopRoot
try {
    if (-not $SkipNpmInstall) {
        & npm ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) { throw "npm ci 失败，退出码：$LASTEXITCODE" }
    }

    if ($NoBundle) {
        & npm run tauri -- build --ci --no-sign --no-bundle
    } else {
        & npm run tauri -- build --ci --no-sign --bundles nsis
    }
    if ($LASTEXITCODE -ne 0) { throw "Tauri 构建失败，退出码：$LASTEXITCODE" }
} finally {
    Pop-Location
}

$ApplicationExePath = Join-Path $TauriRoot 'target\release\PhotoAI.exe'
$SelfTestParent = Join-Path $ProjectRoot 'packaging\build'
New-Item -ItemType Directory -Force -Path $SelfTestParent | Out-Null
$ResolvedSelfTestParent = (Resolve-Path -LiteralPath $SelfTestParent).Path
$SelfTestRoot = [IO.Path]::GetFullPath((Join-Path $ResolvedSelfTestParent 'desktop-self-test'))
if (-not $SelfTestRoot.StartsWith(
    $ResolvedSelfTestParent + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "拒绝准备意外的桌面自检路径：$SelfTestRoot"
}
if (Test-Path -LiteralPath $SelfTestRoot) {
    Remove-Item -LiteralPath $SelfTestRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $SelfTestRoot | Out-Null
try {
    Copy-Item -LiteralPath $ApplicationExePath -Destination $SelfTestRoot -Force
    foreach ($Name in @('core', 'tools', 'integrations', 'manifests')) {
        Copy-Item `
            -LiteralPath (Join-Path $TauriRoot "resources\$Name") `
            -Destination $SelfTestRoot `
            -Recurse `
            -Force
    }
    $SelfTest = Start-Process `
        -FilePath (Join-Path $SelfTestRoot 'PhotoAI.exe') `
        -ArgumentList '--self-test' `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($SelfTest.ExitCode -ne 0) {
        throw "PhotoAI 桌面载荷自检失败，退出码：$($SelfTest.ExitCode)"
    }
} finally {
    if (Test-Path -LiteralPath $SelfTestRoot) {
        Remove-Item -LiteralPath $SelfTestRoot -Recurse -Force
    }
}

if ($NoBundle) {
    Write-Host 'PhotoAI 桌面程序编译完成（未生成安装器）。'
    exit 0
}

$ApplicationExe = Get-Item -LiteralPath $ApplicationExePath
$ResourceBytes = (Get-ChildItem -LiteralPath (Join-Path $TauriRoot 'resources') -Recurse -File |
    Measure-Object -Property Length -Sum).Sum
$InstalledPayloadBytes = $ApplicationExe.Length + $ResourceBytes
if ($InstalledPayloadBytes -gt 700MB) {
    throw "基础程序载荷超过 700MB 门禁：$([Math]::Round($InstalledPayloadBytes / 1MB, 1))MB"
}

$Installers = @(Get-ChildItem -LiteralPath $NsisDirectory -Filter '*.exe' -File)
if ($Installers.Count -ne 1) {
    throw "预期生成一个 NSIS 安装器，实际为 $($Installers.Count) 个：$NsisDirectory"
}
$GeneratedInstaller = $Installers[0]
if ($GeneratedInstaller.FullName -ne $FinalInstallerPath) {
    Move-Item -LiteralPath $GeneratedInstaller.FullName -Destination $FinalInstallerPath
}
$Installer = Get-Item -LiteralPath $FinalInstallerPath
$Limit = 350MB
if ($Installer.Length -gt $Limit) {
    throw "安装器超过 350MB 门禁：$([Math]::Round($Installer.Length / 1MB, 1))MB"
}
$Digest = (Get-FileHash -LiteralPath $Installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$DigestLine = "$Digest *$($Installer.Name)"
Set-Content -LiteralPath ($Installer.FullName + '.sha256') -Value $DigestLine -Encoding ascii

Write-Host "安装器：$($Installer.FullName)"
Write-Host "大小：$([Math]::Round($Installer.Length / 1MB, 1)) MB"
Write-Host "SHA-256：$Digest"
