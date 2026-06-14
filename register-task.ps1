<#
Registers a Windows Scheduled Task that runs collect.py every day at noon.

Usage (from this folder):
    powershell -ExecutionPolicy Bypass -File .\register-task.ps1
    powershell -ExecutionPolicy Bypass -File .\register-task.ps1 -Time "09:30"

Uses pythonw.exe so no console window appears. Re-running updates the task.
To remove it:  schtasks /Delete /TN "SteamReviewCollector" /F

IMPORTANT: this prefers the project's 3.12 .venv interpreter. The daily run does
PICS public-build detection, which needs the `steam` package installed in that
venv (it only works on Python 3.12 — see SETUP.md). If the venv is missing it
falls back to whatever pythonw.exe is on PATH, and the PICS step simply no-ops.
#>

param(
    [string]$Time = "12:00",
    [string]$TaskName = "SteamReviewCollector"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$collect = Join-Path $scriptDir "collect.py"

# Prefer the 3.12 venv's pythonw.exe so the scheduled run has the `steam`
# package available for PICS. Fall back to PATH pythonw/python otherwise.
$venvPyw = Join-Path $scriptDir ".venv\Scripts\pythonw.exe"
if (Test-Path $venvPyw) {
    $pyw = $venvPyw
} else {
    Write-Warning "No .venv found at $venvPyw - the PICS public-build step will be skipped."
    Write-Warning "Create it with:  py -3.12 -m venv .venv  (see SETUP.md)"
    $pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $pyw) {
        $pyw = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    }
}
if (-not $pyw) {
    Write-Error "Could not find pythonw.exe or python.exe (no venv and none on PATH)."
    exit 1
}

Write-Host "Python:  $pyw"
Write-Host "Script:  $collect"
Write-Host "Time:    $Time daily"

$action  = New-ScheduledTaskAction -Execute $pyw -Argument "`"$collect`"" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -RunOnlyIfNetworkAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Daily Steam review collection" -Force | Out-Null

Write-Host ""
Write-Host "Registered scheduled task '$TaskName' (daily at $Time)."
Write-Host "Run now to test:   schtasks /Run /TN `"$TaskName`""
Write-Host "Remove it:         schtasks /Delete /TN `"$TaskName`" /F"
