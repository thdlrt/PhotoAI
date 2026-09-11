$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'env.ps1')

$Url = 'http://127.0.0.1:8765/'
try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
    if ($response.StatusCode -eq 200) {
        Start-Process $Url
        exit 0
    }
} catch {
    # The service is not running yet.
}

$LogRoot = Join-Path $ProjectRoot '.runtime\logs'
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$Uv = (Get-Command uv -ErrorAction Stop).Source
$process = Start-Process `
    -FilePath $Uv `
    -ArgumentList @('run', '--extra', 'web', 'landscape-culler-web') `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogRoot 'web.stdout.log') `
    -RedirectStandardError (Join-Path $LogRoot 'web.stderr.log') `
    -PassThru

for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if ($process.HasExited) {
        throw "选片服务启动失败，请查看 $LogRoot\web.stderr.log"
    }
    Start-Sleep -Milliseconds 250
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Start-Process $Url
            exit 0
        }
    } catch {
        # Keep waiting until the local service is ready.
    }
}

throw "选片服务启动超时，请查看 $LogRoot\web.stderr.log"
