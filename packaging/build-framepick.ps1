[CmdletBinding()]
param([Parameter(Mandatory)][string]$Destination)
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Source = Join-Path $Root 'integrations\framepick'
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { throw '帧选构建需要 .NET 8 SDK；最终用户不需要单独安装。' }
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
& dotnet publish (Join-Path $Source 'src\FramePick\FramePick.csproj') -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=false -p:EnableCompressionInSingleFile=true `
    -p:DebugType=None -p:DebugSymbols=false -o $Destination
if ($LASTEXITCODE -ne 0) { throw '帧选发布构建失败。' }
Copy-Item (Join-Path $Source 'LICENSE') -Destination (Join-Path $Destination 'LICENSE') -Force
Copy-Item (Join-Path $Source 'THIRD_PARTY_NOTICES.md') -Destination $Destination -Force
$LicenseRoot = Join-Path $Destination 'licenses'
New-Item -ItemType Directory -Force -Path $LicenseRoot | Out-Null
$Assets = Get-Content (Join-Path $Source 'src\FramePick\obj\project.assets.json') -Raw | ConvertFrom-Json
$PackageRoot = $Assets.packageFolders.PSObject.Properties.Name | Select-Object -First 1
foreach ($Framework in @(@('microsoft.netcore.app.runtime.win-x64', 'dotnet'), @('microsoft.windowsdesktop.app.runtime.win-x64', 'wpf'))) {
    $PackRoot = Join-Path $PackageRoot $Framework[0]
    $RuntimePack = Get-ChildItem -LiteralPath $PackRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $RuntimePack) { throw "缺少运行库许可证：$PackRoot" }
    Get-ChildItem -LiteralPath $RuntimePack.FullName -File | Where-Object { $_.Name -match '^(LICENSE(\.TXT)?|THIRD-PARTY-NOTICES\.TXT)$' } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $LicenseRoot ($Framework[1] + '-' + $_.Name)) -Force
    }
}
if (-not (Test-Path (Join-Path $LicenseRoot 'dotnet-LICENSE.TXT')) -or -not (Test-Path (Join-Path $LicenseRoot 'wpf-LICENSE'))) { throw '帧选缺少 .NET/WPF 原始许可。' }
$OriginalRoot = $env:FRAMEPICK_DATA_ROOT
try {
    $env:FRAMEPICK_DATA_ROOT = Join-Path $Root 'packaging\build\framepick-self-test'
    $Process = Start-Process -FilePath (Join-Path $Destination 'FramePick.exe') -ArgumentList '--self-test' -WindowStyle Hidden -Wait -PassThru
    if ($Process.ExitCode -ne 0) { throw '帧选界面资源自检失败。' }
} finally { $env:FRAMEPICK_DATA_ROOT = $OriginalRoot }
