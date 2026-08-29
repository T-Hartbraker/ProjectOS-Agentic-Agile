# Resolve a real CPython. Skip the Microsoft Store python.exe alias.
function Resolve-ProjectOSPython {
    $seen = @{}
    $candidates = New-Object System.Collections.Generic.List[string]

    function Add-Candidate([string]$Path, [switch]$First) {
        if (-not $Path) { return }
        if ($Path -match "WindowsApps") { return }
        if (-not (Test-Path $Path)) { return }
        $key = $Path.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { return }
        $seen[$key] = $true
        if ($First) {
            $candidates.Insert(0, $Path)
        } else {
            $candidates.Add($Path)
        }
    }

    $root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    Add-Candidate (Join-Path $root ".venv\Scripts\python.exe") -First

    $local = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $local) {
        Get-ChildItem $local -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { Add-Candidate $_.FullName }
    }

    foreach ($root in @(${env:ProgramFiles}, ${env:ProgramFiles(x86)})) {
        if (-not $root) { continue }
        Get-ChildItem $root -Filter "Python*" -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Add-Candidate (Join-Path $_.FullName "python.exe") }
    }

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher -and $pyLauncher.Source -notmatch "WindowsApps") {
        try {
            $out = & $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($out) { Add-Candidate $out.Trim() }
        } catch { }
    }

    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { Add-Candidate $cmd.Source }
    }

    foreach ($exe in $candidates) {
        try {
            $out = & $exe -c "import sys; print(sys.version_info[0])" 2>$null
            if ("$out".Trim() -eq "3") {
                return $exe
            }
        } catch { }
    }
    return $null
}
