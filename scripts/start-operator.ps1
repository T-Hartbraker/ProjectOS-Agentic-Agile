param(
    [switch]$Wait,
    [switch]$Detach
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

. (Join-Path $PSScriptRoot "resolve-python.ps1")
$python = Resolve-ProjectOSPython
if (-not $python) {
    Write-Error "A real Python 3 install was not found. Disable Settings > Apps > Advanced app settings > App execution aliases for python.exe, or install Python from python.org."
    exit 1
}

$src = Join-Path $Root "src"
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$src;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $src
}

$pythonArgs = @("-m", "projectos", "start")
if ($Wait -and -not $Detach) {
    $pythonArgs += "--wait"
}

Write-Host "Starting ProjectOS operator from $Root with $python"
& $python @pythonArgs
exit $LASTEXITCODE
