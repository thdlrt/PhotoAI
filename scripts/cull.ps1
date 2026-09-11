[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$InputPath,

    [ValidateRange(0.05, 1.0)]
    [double]$RetainRatio = 0.30,

    [ValidateSet('fast', 'deep')]
    [string]$Mode = 'deep'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')

uv run landscape-culler score `
    --input $InputPath `
    --data-dir $env:PHOTO_AI_DATA_DIR `
    --retain-ratio $RetainRatio `
    --mode $Mode
if ($LASTEXITCODE -ne 0) {
    throw "照片评分失败，退出码：$LASTEXITCODE"
}

$LatestResults = Join-Path $env:PHOTO_AI_DATA_DIR 'runs\latest\results.json'
uv run landscape-culler write-xmp `
    --results $LatestResults `
    --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "XMP dry-run 失败，退出码：$LASTEXITCODE"
}

$LatestReport = Join-Path $env:PHOTO_AI_DATA_DIR 'runs\latest\report.html'
Write-Host ""
Write-Host "评分与 dry-run 已完成。先审阅报告，再决定是否提交 XMP："
Write-Host $LatestReport
