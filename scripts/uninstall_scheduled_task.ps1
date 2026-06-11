<#
.SYNOPSIS
    Remove the TrendRadarCN scheduled task.

.PARAMETER TaskName
    Name of the task to remove. Default: TrendRadarCN-DailyBoards
#>
param(
    [string]$TaskName = "TrendRadarCN-DailyBoards"
)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Task '$TaskName' not found - nothing to do"
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'" -ForegroundColor Green
