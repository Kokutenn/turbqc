# Registers a Windows scheduled task that polls the feed every 5 minutes.
# Run from the repo root in PowerShell:   .\scripts\schedule_windows.ps1
# Remove it again with:                   Unregister-ScheduledTask -TaskName TurbQC-Collector
param(
    [string]$Python = (Get-Command python).Source,
    [int]$Minutes = 5
)
$repo = (Resolve-Path "$PSScriptRoot\..").Path
$action = New-ScheduledTaskAction -Execute $Python -Argument "-m turbqc ingest" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
Register-ScheduledTask -TaskName "TurbQC-Collector" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Polls aviationweather.gov aircraft reports into TurbQC" -Force
Write-Host "TurbQC-Collector registered: every $Minutes min, python = $Python"
