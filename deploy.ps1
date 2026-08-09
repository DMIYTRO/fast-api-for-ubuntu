param (
    [string]$msg = "auto sync: dev updates"
)

$pythonExe = "python"
$scriptPath = "$PSScriptRoot\deploy.py"

if (Test-Path $scriptPath) {
    & $pythonExe $scriptPath $msg
} else {
    Write-Host "Error: deploy.py not found in $PSScriptRoot" -ForegroundColor Red
}
