<#
.SYNOPSIS
    Run all topic boards once. Designed to be called by Windows Task
    Scheduler (or any cron-like trigger), but also safe to run manually.

.DESCRIPTION
    - Locates the project root from the script's own location
      (so the task can be invoked from anywhere)
    - Uses the project venv's python directly (.venv\Scripts\python.exe);
      no Activate.ps1 dance needed for non-interactive runs
    - Tees stdout+stderr to logs\scheduled.log with timestamped headers
    - Preserves the python process exit code so Task Scheduler reports
      pass/fail accurately

.EXAMPLE
    .\scripts\run_boards.ps1
    Run all boards once and append to logs\scheduled.log

.EXAMPLE
    .\scripts\run_boards.ps1 -Board my-portfolio
    Run only one specific board
#>
param(
    [string]$Board = "all"
)

$ErrorActionPreference = 'Continue'

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogDir     = Join-Path $RepoRoot "logs"
$LogFile    = Join-Path $LogDir "scheduled.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

if (-not (Test-Path $VenvPython)) {
    $msg = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ERROR: venv python not found at $VenvPython"
    Add-Content -Path $LogFile -Value $msg
    Write-Error $msg
    exit 1
}

Set-Location $RepoRoot

$Start = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $LogFile -Value ""
Add-Content -Path $LogFile -Value "=========================================================="
Add-Content -Path $LogFile -Value "=== [$Start] starting board run (--board $Board) ==="
Add-Content -Path $LogFile -Value "=========================================================="

& $VenvPython run.py --board $Board 2>&1 | Tee-Object -FilePath $LogFile -Append
$ExitCode = $LASTEXITCODE

$End = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $LogFile -Value "=== [$End] finished (exit=$ExitCode) ==="

exit $ExitCode
