# Registers two Windows scheduled tasks for the current user (no admin rights needed):
#   TurbQC-Collector  every 5 minutes  python -m turbqc ingest   (poll the live feed, run all checks)
#   TurbQC-Daily      once a day       python -m turbqc daily    (backfill, nowcast verification, parallel run)
# pythonw.exe is used so no console window flashes up every 5 minutes.
#
# Run from the repo root in PowerShell:   .\scripts\schedule_windows.ps1
# Remove again with:                      Unregister-ScheduledTask -TaskName TurbQC-Collector, TurbQC-Daily
param(
    [string]$Python = (Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"),
    [int]$Minutes = 5,
    [string]$DailyAt = "06:30"
)
$repo = (Resolve-Path "$PSScriptRoot\..").Path
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$collector = New-ScheduledTaskAction -Execute $Python -Argument "-m turbqc ingest" -WorkingDirectory $repo
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings.ExecutionTimeLimit = "PT4M"
Register-ScheduledTask -TaskName "TurbQC-Collector" -Action $collector -Trigger $every -Settings $settings `
    -Description "TurbQC: poll aviationweather.gov aircraft reports and run all checks" -Force | Out-Null

$daily = New-ScheduledTaskAction -Execute $Python -Argument "-m turbqc daily" -WorkingDirectory $repo
$at = New-ScheduledTaskTrigger -Daily -At $DailyAt
$settings.ExecutionTimeLimit = "PT1H"
Register-ScheduledTask -TaskName "TurbQC-Daily" -Action $daily -Trigger $at -Settings $settings `
    -Description "TurbQC: back-fill PIREPs, verify the GTG Nowcast, run the v1/v2 parallel run" -Force | Out-Null

Get-ScheduledTask -TaskName TurbQC-* | Select-Object TaskName, State
Write-Host "python = $Python"
