param(
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$LogDir = Join-Path $Root "logs\operator"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LaunchLog = Join-Path $LogDir "launcher.log"

function Write-LaunchLog([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LaunchLog -Value $line
}

function Show-Error([string]$Message) {
    Write-LaunchLog "ERROR $Message"
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    [System.Windows.Forms.MessageBox]::Show(
        $Message,
        "ProjectOS",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
}

. (Join-Path $PSScriptRoot "resolve-python.ps1")
function Get-Python {
    return Resolve-ProjectOSPython
}

function Test-Url([string]$Url) {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Start-HiddenPython([string[]]$ArgumentList, [string]$OutLog) {
    $errLog = "$OutLog.err"
    foreach ($path in @($OutLog, $errLog)) {
        if (Test-Path $path) {
            Remove-Item $path -Force
        }
    }
    $arg = @("-m", "projectos") + $ArgumentList
    Start-Process -FilePath $script:Python `
        -ArgumentList $arg `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $errLog |
        Out-Null
}

Write-LaunchLog "Launch from $Root"

$python = Get-Python
if (-not $python) {
    Show-Error "A real Python 3 install was not found. Disable Settings > Apps > Advanced app settings > App execution aliases for python.exe, or install Python from python.org."
    exit 1
}
$script:Python = $python
Write-LaunchLog "Using Python $python"

$src = Join-Path $Root "src"
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$src;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $src
}

Write-LaunchLog "Ensuring Python HTTP dependencies for API and Slack"
$depsLog = Join-Path $LogDir "deps.log"
$depsErr = Join-Path $LogDir "deps.log.err"
foreach ($path in @($depsLog, $depsErr)) {
    if (Test-Path $path) {
        Remove-Item $path -Force
    }
}
Start-Process -FilePath $python `
    -ArgumentList @(
        "-m", "pip", "install", "--disable-pip-version-check", "-q",
        "fastapi>=0.111", "uvicorn>=0.30", "httpx>=0.27", "websocket-client>=1.8"
    ) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -Wait `
    -RedirectStandardOutput $depsLog `
    -RedirectStandardError $depsErr | Out-Null
if ($LASTEXITCODE -ne 0) {
    $tail = ""
    if (Test-Path $depsErr) { $tail += "`n`ndeps.log.err:`n" + ((Get-Content $depsErr -Tail 20) -join "`n") }
    if (Test-Path $depsLog) { $tail += "`n`ndeps.log:`n" + ((Get-Content $depsLog -Tail 20) -join "`n") }
    Show-Error "ProjectOS could not install required Python packages (websocket-client, fastapi, uvicorn, httpx).$tail"
    exit 1
}

$apiPort = 8787
$configPath = Join-Path $Root "config\operator.json"
if (Test-Path $configPath) {
    $cfg = Get-Content -Raw $configPath | ConvertFrom-Json
    if ($cfg.api.port) { $apiPort = [int]$cfg.api.port }
}

$apiUrl = "http://127.0.0.1:$apiPort/v1/health"
$dashUrl = "http://127.0.0.1:$apiPort/"

Write-LaunchLog "Building dashboard bundle when needed"
$buildLog = Join-Path $LogDir "dashboard-build.log"
$buildErr = Join-Path $LogDir "dashboard-build.log.err"
foreach ($path in @($buildLog, $buildErr)) {
    if (Test-Path $path) {
        Remove-Item $path -Force
    }
}
$buildArgs = @("-m", "projectos", "dashboard", "build")
Start-Process -FilePath $python `
    -ArgumentList $buildArgs `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -Wait `
    -RedirectStandardOutput $buildLog `
    -RedirectStandardError $buildErr | Out-Null
if ($LASTEXITCODE -ne 0) {
    $tail = ""
    if (Test-Path $buildErr) { $tail += "`n`ndashboard-build.log.err:`n" + ((Get-Content $buildErr -Tail 20) -join "`n") }
    if (Test-Path $buildLog) { $tail += "`n`ndashboard-build.log:`n" + ((Get-Content $buildLog -Tail 20) -join "`n") }
    Show-Error "ProjectOS dashboard build failed before startup.$tail"
    exit 1
}

Write-LaunchLog "Stopping existing ProjectOS operator before launch"
$stopLog = Join-Path $LogDir "stop.log"
$stopErr = Join-Path $LogDir "stop.log.err"
foreach ($path in @($stopLog, $stopErr)) {
    if (Test-Path $path) {
        Remove-Item $path -Force
    }
}
Start-Process -FilePath $python `
    -ArgumentList @("-m", "projectos", "stop") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -Wait `
    -RedirectStandardOutput $stopLog `
    -RedirectStandardError $stopErr | Out-Null

Write-LaunchLog "Starting ProjectOS operator"
$startLog = Join-Path $LogDir "start.log"
$startErr = Join-Path $LogDir "start.log.err"
foreach ($path in @($startLog, $startErr)) {
    if (Test-Path $path) {
        Remove-Item $path -Force
    }
}
Start-HiddenPython @("start") $startLog

if (-not (Test-Url $apiUrl)) {
    Write-LaunchLog "Operator start did not bring API up; starting API and daemon directly"
    $apiLog = Join-Path $LogDir "api.log"
    $daemonLog = Join-Path $LogDir "daemon.log"
    Start-HiddenPython @(
        "--config", (Join-Path $Root "config\projects.json"),
        "api", "--host", "127.0.0.1", "--port", "$apiPort",
        "--db", (Join-Path $Root "state\projectos.db")
    ) $apiLog
    Start-HiddenPython @(
        "--config", (Join-Path $Root "config\projects.json"),
        "daemon", "run",
        "--db", (Join-Path $Root "state\projectos.db")
    ) $daemonLog
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    if ((Test-Url $apiUrl) -and (Test-Url $dashUrl)) {
        Write-LaunchLog "Ready. Opening $dashUrl"
        Start-Process $dashUrl
        exit 0
    }
    Start-Sleep -Milliseconds 500
}

$apiLog = Join-Path $LogDir "api.log"
$apiErr = Join-Path $LogDir "api.log.err"
$tail = ""
if (Test-Path $apiLog) { $tail += "`n`napi.log:`n" + ((Get-Content $apiLog -Tail 20) -join "`n") }
if (Test-Path $apiErr) { $tail += "`n`napi.log.err:`n" + ((Get-Content $apiErr -Tail 20) -join "`n") }
Show-Error "ProjectOS did not become ready at $dashUrl within $TimeoutSeconds seconds.$tail"
exit 1
