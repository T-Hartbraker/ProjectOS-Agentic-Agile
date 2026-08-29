$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LaunchVbs = Join-Path $PSScriptRoot "launch-projectos.vbs"
$StopPs1 = Join-Path $PSScriptRoot "stop-operator.ps1"
$Wsh = New-Object -ComObject WScript.Shell

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkDir, [string]$Description) {
    $dir = Split-Path $Path -Parent
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    $shortcut = $Wsh.CreateShortcut($Path)
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkDir
    $shortcut.WindowStyle = 7
    $shortcut.Description = $Description
    $shortcut.IconLocation = "$env:SystemRoot\System32\imageres.dll,109"
    $shortcut.Save()
}

$desktop = [Environment]::GetFolderPath("Desktop")
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\ProjectOS"
New-Shortcut `
    -Path (Join-Path $desktop "ProjectOS.lnk") `
    -Target "$env:SystemRoot\System32\wscript.exe" `
    -Arguments "`"$LaunchVbs`"" `
    -WorkDir $Root `
    -Description "Start ProjectOS and open the operator dashboard"

New-Shortcut `
    -Path (Join-Path $startMenu "ProjectOS.lnk") `
    -Target "$env:SystemRoot\System32\wscript.exe" `
    -Arguments "`"$LaunchVbs`"" `
    -WorkDir $Root `
    -Description "Start ProjectOS and open the operator dashboard"

New-Shortcut `
    -Path (Join-Path $startMenu "Stop ProjectOS.lnk") `
    -Target "powershell.exe" `
    -Arguments "-NoProfile -ExecutionPolicy Bypass -File `"$StopPs1`"" `
    -WorkDir $Root `
    -Description "Stop ProjectOS local services"

Write-Host "Created Desktop shortcut: $desktop\ProjectOS.lnk"
Write-Host "Created Start Menu folder: $startMenu"
Write-Host "Double-click ProjectOS to start services and open the dashboard."
