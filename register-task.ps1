<#
Registers a Windows Scheduled Task that runs collect.py every day at noon.

Usage (from this folder):
    powershell -ExecutionPolicy Bypass -File .\register-task.ps1
    powershell -ExecutionPolicy Bypass -File .\register-task.ps1 -Time "09:30"

Uses pythonw.exe so no console window appears. Re-running updates the task.
To remove it:  schtasks /Delete /TN "SteamReviewCollector" /F
#>

param(
    [string]$Time = "12:00",
    [string]$TaskName = "SteamReviewCollector"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$collect = Join-Path $scriptDir "collect.py"

# Prefer pythonw.exe (no console). Fall back to python.exe if not found.
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) {
    $pyw = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pyw) {
    Write-Error "Could not find pythonw.exe or python.exe on PATH."
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
