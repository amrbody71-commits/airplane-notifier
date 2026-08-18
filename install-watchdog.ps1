<#
.SYNOPSIS
  Installs the resilience layer that keeps Airplane Notifier running.

.DESCRIPTION
  Registers a scheduled task, "AirplaneNotifier Watchdog", with two triggers:

    - At logon: starts the watchdog, which then loops internally every ~10
      seconds checking whether the main app is alive, and relaunches it the
      moment it isn't. This is what makes a crash or a killed process invisible
      for seconds rather than however long it takes you to notice.

    - Every 1 minute: also tries to start the watchdog. The watchdog's own
      single-instance mutex makes this a no-op almost every time -- it exists
      purely as a safety net for the one thing the fast internal loop cannot
      protect against: the watchdog process itself dying. That is a rare
      double-fault (the watchdog is a handful of lines with no real failure
      surface), bounded here to a one-minute worst case instead of being
      completely uncovered.

  Idempotent: re-running this updates the existing task rather than duplicating it.

.NOTES
  Requires the app to already be built (run build.bat first), so that
  dist\airplane-notifier\airplane-notifier-watchdog.exe exists.
#>

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$watchdogExe = Join-Path $root "dist\airplane-notifier\airplane-notifier-watchdog.exe"

if (-not (Test-Path $watchdogExe)) {
    Write-Error "Not found: $watchdogExe`nRun build.bat first."
    exit 1
}

$action = New-ScheduledTaskAction -Execute $watchdogExe

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logonTrigger.Delay = "PT15S"

$safetyNetTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

Register-ScheduledTask -TaskName "AirplaneNotifier Watchdog" `
    -Action $action -Trigger $logonTrigger, $safetyNetTrigger -Settings $settings `
    -Description "Keeps Airplane Notifier running: relaunches it within ~10-25s of a crash or kill, at login, and every minute as a safety net. See install-watchdog.ps1." `
    -Force | Out-Null

Write-Host "Installed. Starting it now so protection begins immediately..."
Start-ScheduledTask -TaskName "AirplaneNotifier Watchdog"
Start-Sleep -Seconds 2

$watchdogRunning = Get-Process -Name "airplane-notifier-watchdog" -ErrorAction SilentlyContinue
$appRunning = Get-Process -Name "airplane-notifier" -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Watchdog running : $(if ($watchdogRunning) { 'yes' } else { 'NO - check dist\airplane-notifier-watchdog.exe exists' })"
Write-Host "Main app running : $(if ($appRunning) { 'yes' } else { 'no (watchdog will start it within ~10s)' })"
