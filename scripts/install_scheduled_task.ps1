<#
.SYNOPSIS
    Register (or replace) a Windows Scheduled Task that runs all
    TrendRadarCN boards once a day at a fixed local time.

.PARAMETER Time
    Daily run time in HH:mm (24-hour). Default: 07:30

.PARAMETER TaskName
    Name to register under in Task Scheduler.
    Default: TrendRadarCN-DailyBoards

.EXAMPLE
    .\scripts\install_scheduled_task.ps1
    Schedule daily run at 07:30

.EXAMPLE
    .\scripts\install_scheduled_task.ps1 -Time 08:15
    Schedule daily run at 08:15

.NOTES
    - Runs as YOU (no admin required, no password stored)
    - "Only when logged in" - if you're not signed in at 07:30,
      catches up as soon as you log in
    - Allowed on battery / will not stop if you unplug mid-run
    - Auto-retries up to 2 times if first run fails
#>
param(
    [string]$Time = "07:30",
    [string]$TaskName = "TrendRadarCN-DailyBoards"
)

$RepoRoot     = Split-Path -Parent $PSScriptRoot
$RunnerScript = Join-Path $PSScriptRoot "run_boards.ps1"

if (-not (Test-Path $RunnerScript)) {
    Write-Error "Runner script not found at $RunnerScript"
    exit 1
}

# Action: powershell.exe runs the wrapper, with hidden window so it doesn't
# pop up over your desktop every morning. -NoProfile speeds startup.
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerScript`"" `
    -WorkingDirectory $RepoRoot

# Trigger: daily at $Time
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time

# Settings: catch-up if missed, OK on battery, hard cap at 1 hour,
# retry 2 times spaced 10 min apart if it fails
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

# Principal: run as the current interactive user, no elevation, no
# password storage. This means the task only fires while you are
# logged in (or catches up on next login via StartWhenAvailable).
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Replace if exists (idempotent install)
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task '$TaskName'" -ForegroundColor Yellow
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "TrendRadarCN: fetch + LLM-tag all topic boards (daily $Time)" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal | Out-Null

Write-Host ""
Write-Host "OK: scheduled task '$TaskName' installed" -ForegroundColor Green
Write-Host ""
Write-Host "  trigger      : daily at $Time"
Write-Host "  user         : $env:USERDOMAIN\$env:USERNAME (no admin needed)"
Write-Host "  missed run   : auto catch-up on next login"
Write-Host "  on battery   : allowed"
Write-Host "  retry        : 2 attempts, 10 min apart"
Write-Host "  log file     : $RepoRoot\logs\scheduled.log"
Write-Host ""
Write-Host "Quick commands:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName       # last run time + result"
Write-Host "  Start-ScheduledTask    -TaskName $TaskName       # trigger NOW to test"
Write-Host "  Get-Content logs\scheduled.log -Tail 80          # see last run output"
Write-Host "  .\scripts\uninstall_scheduled_task.ps1           # remove the task"
Write-Host ""
