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

Write-Host "Stopping ProjectOS operator with $python"
& $python -m projectos stop
exit $LASTEXITCODE
