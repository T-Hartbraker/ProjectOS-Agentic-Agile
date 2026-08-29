$ErrorActionPreference = "Stop"

function Assert-ExitCode {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Step
    )

    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

Set-Location C:\Dev\ProjectOS
. .\.venv\Scripts\Activate.ps1

Write-Host "=== ProjectOS full Python test suite ==="
pytest -q
Assert-ExitCode "ProjectOS pytest"

Write-Host "=== Registry validation ==="
python -m projectos registry validate
Assert-ExitCode "ProjectOS registry validation"

Write-Host "=== ProjectOS doctor ==="
python -m projectos doctor
Assert-ExitCode "ProjectOS doctor"

Write-Host "=== Dashboard tests ==="
Set-Location C:\Dev\ProjectOS\web
npm test --if-present
Assert-ExitCode "ProjectOS web tests"

Write-Host "=== Dashboard production build ==="
npm run build
Assert-ExitCode "ProjectOS web build"

Set-Location C:\Dev\ProjectOS

Write-Host ""
Write-Host "PROJECT OS PHASE 3 VERIFICATION PASS"
exit 0
