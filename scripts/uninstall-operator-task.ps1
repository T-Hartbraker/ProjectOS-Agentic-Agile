param(
    [string]$TaskName = "ProjectOS-Operator"
)

$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task $TaskName."
} else {
    Write-Host "Scheduled task $TaskName was not registered."
}
