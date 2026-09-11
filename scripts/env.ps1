$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path $ProjectRoot '.runtime'

$env:UV_PROJECT_ENVIRONMENT = Join-Path $ProjectRoot '.venv'
$env:UV_CACHE_DIR = Join-Path $RuntimeRoot 'uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeRoot 'uv-python'
$env:UV_TOOL_DIR = Join-Path $RuntimeRoot 'uv-tools'
$env:PIP_CACHE_DIR = Join-Path $RuntimeRoot 'pip-cache'
$env:XDG_CACHE_HOME = Join-Path $RuntimeRoot 'xdg-cache'
$env:MPLCONFIGDIR = Join-Path $RuntimeRoot 'matplotlib'
$env:HF_HOME = Join-Path $RuntimeRoot 'huggingface'
$env:HF_HUB_CACHE = Join-Path $env:HF_HOME 'hub'
$env:TRANSFORMERS_CACHE = Join-Path $env:HF_HOME 'transformers'
$env:PHOTO_AI_CLIP_MODEL = Join-Path $RuntimeRoot 'models\clip-vit-base-patch32'
$env:TORCH_HOME = Join-Path $RuntimeRoot 'torch'
$env:OLLAMA_MODELS = Join-Path $RuntimeRoot 'ollama-models'
$env:PHOTO_AI_OLLAMA = Join-Path $RuntimeRoot 'tools\ollama-v0.33.2\ollama.exe'
$env:OLLAMA_HOST = '127.0.0.1:11435'
$env:OLLAMA_NO_CLOUD = 'true'
$env:PHOTO_AI_DATA_DIR = Join-Path $RuntimeRoot 'data'
$env:PHOTO_AI_EXIFTOOL = Join-Path $RuntimeRoot 'tools\exiftool-13.59\exiftool-13.59_64\exiftool.exe'
$env:TEMP = Join-Path $RuntimeRoot 'temp'
$env:TMP = $env:TEMP
$env:LC_ALL = 'C'
$env:LANG = 'C'

$directories = @(
    $RuntimeRoot,
    $env:UV_CACHE_DIR,
    $env:UV_PYTHON_INSTALL_DIR,
    $env:UV_TOOL_DIR,
    $env:PIP_CACHE_DIR,
    $env:XDG_CACHE_HOME,
    $env:MPLCONFIGDIR,
    $env:HF_HOME,
    (Split-Path -Parent $env:PHOTO_AI_CLIP_MODEL),
    $env:TORCH_HOME,
    $env:OLLAMA_MODELS,
    $env:PHOTO_AI_DATA_DIR,
    $env:TEMP
)
foreach ($directory in $directories) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

Write-Host "Photo AI runtime: $RuntimeRoot"
