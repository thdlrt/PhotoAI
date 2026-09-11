[CmdletBinding()]
param(
    [ValidateSet('8gb', '16gb')]
    [string]$Profile
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'env.ps1')

Push-Location $ProjectRoot
try {
    uv sync --extra web --extra dev
    if ($LASTEXITCODE -ne 0) {
        throw "Python AI 依赖安装失败，退出码：$LASTEXITCODE"
    }

    if ($Profile) {
        & (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -m landscape_culler.cli model-resources-configure `
            --profile $Profile `
            --runtime-root (Join-Path $ProjectRoot '.runtime') `
            --data-dir (Join-Path $ProjectRoot '.runtime\data')
        if ($LASTEXITCODE -ne 0) {
            throw "AI 模型配置失败，退出码：$LASTEXITCODE"
        }
        Write-Host "AI $Profile 档模型已准备完成；全部资源位于项目运行目录。"
    } else {
        Write-Host '开发环境已准备完成。Ollama 与模型会由“设置 → AI 模型”一键自动安装。'
    }
} finally {
    Pop-Location
}
