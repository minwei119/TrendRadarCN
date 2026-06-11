<#
.SYNOPSIS
    One-shot setup for a fresh checkout of TrendRadarCN on Windows.

.DESCRIPTION
    Run this once after cloning the repo to a new machine. The script:

      1) Verifies Python 3.11+ is installed (gives a download link if not)
      2) Creates a virtual environment in .venv (skips if it already exists)
      3) Installs all dependencies from requirements.txt
      4) Creates .env from .env.example if missing, prompting for values
      5) Initializes the SQLite database
      6) Optionally installs the daily scheduled task

    Re-runnable: existing files are detected and reused, never overwritten
    silently. Use -Force to wipe and start fresh.

.PARAMETER Force
    Recreate .venv and overwrite .env even if they exist.

.PARAMETER SkipTask
    Don't ask about installing the scheduled task.

.PARAMETER PythonVersion
    Specific Python version to use, e.g. "3.13". Default: auto-pick latest
    installed 3.11+.

.EXAMPLE
    .\scripts\setup.ps1
    Standard interactive setup.

.EXAMPLE
    .\scripts\setup.ps1 -Force
    Wipe .venv and .env, start completely fresh.

.EXAMPLE
    .\scripts\setup.ps1 -PythonVersion 3.13 -SkipTask
    Use 3.13 explicitly, don't prompt about scheduled task.
#>
param(
    [switch]$Force,
    [switch]$SkipTask,
    [string]$PythonVersion
)

$ErrorActionPreference = 'Stop'

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$VenvDir    = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir  "Scripts\python.exe"
$EnvFile    = Join-Path $RepoRoot ".env"
$EnvSample  = Join-Path $RepoRoot ".env.example"
$ReqFile    = Join-Path $RepoRoot "requirements.txt"

Set-Location $RepoRoot

# ----------------------------------------------------------------------------
# UI helpers
# ----------------------------------------------------------------------------
function Write-Step($num, $title) {
    Write-Host ""
    Write-Host "==================================================================" -ForegroundColor Cyan
    Write-Host " STEP $num : $title" -ForegroundColor Cyan
    Write-Host "==================================================================" -ForegroundColor Cyan
}
function Write-OK($msg)    { Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Write-Skip($msg)  { Write-Host "  [--] $msg"   -ForegroundColor DarkGray }
function Write-Warn2($msg) { Write-Host "  [! ] $msg"   -ForegroundColor Yellow }
function Write-Bad($msg)   { Write-Host "  [X ] $msg"   -ForegroundColor Red }

# ----------------------------------------------------------------------------
# STEP 1 : Locate Python 3.11+
# ----------------------------------------------------------------------------
Write-Step 1 "Locate a working Python (>= 3.11)"

function Find-Python {
    param([string]$RequestedVersion)

    # If user named a specific version, use it
    if ($RequestedVersion) {
        $cmd = "py -$RequestedVersion -c `"import sys; print(sys.version.split()[0])`""
        try {
            $ver = Invoke-Expression $cmd 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                return @{ Launcher = "py -$RequestedVersion"; Version = $ver.Trim() }
            }
        } catch {}
        Write-Bad "Requested Python $RequestedVersion is not installed."
        Write-Host "  Install it from https://www.python.org/downloads/ and re-run." -ForegroundColor Yellow
        exit 1
    }

    # Else try 3.13 → 3.12 → 3.11 in order
    foreach ($v in @("3.13", "3.12", "3.11")) {
        $cmd = "py -$v -c `"import sys; print(sys.version.split()[0])`""
        try {
            $ver = Invoke-Expression $cmd 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                return @{ Launcher = "py -$v"; Version = $ver.Trim() }
            }
        } catch {}
    }
    return $null
}

$py = Find-Python -RequestedVersion $PythonVersion
if (-not $py) {
    Write-Bad "No Python 3.11+ found via the 'py' launcher."
    Write-Host ""
    Write-Host "  Install Python 3.13 from https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "  During install, check 'Add python.exe to PATH'." -ForegroundColor Yellow
    Write-Host "  Then reopen PowerShell and re-run this script." -ForegroundColor Yellow
    exit 1
}
Write-OK "Using $($py.Launcher) (Python $($py.Version))"

# ----------------------------------------------------------------------------
# STEP 2 : Create / reuse .venv
# ----------------------------------------------------------------------------
Write-Step 2 "Create virtual environment (.venv)"

if ((Test-Path $VenvDir) -and -not $Force) {
    if (Test-Path $VenvPython) {
        $existingVer = & $VenvPython -c "import sys; print(sys.version.split()[0])"
        Write-Skip ".venv already exists (Python $existingVer) - reusing. Pass -Force to recreate."
    } else {
        Write-Warn2 ".venv exists but Python binary missing - recreating."
        Remove-Item -Recurse -Force $VenvDir
        Invoke-Expression "$($py.Launcher) -m venv `"$VenvDir`""
        Write-OK "Recreated .venv with $($py.Launcher)"
    }
} else {
    if (Test-Path $VenvDir) {
        Write-Warn2 "-Force given: removing existing .venv"
        Remove-Item -Recurse -Force $VenvDir
    }
    Invoke-Expression "$($py.Launcher) -m venv `"$VenvDir`""
    Write-OK "Created .venv"
}

# Sanity check
if (-not (Test-Path $VenvPython)) {
    Write-Bad ".venv\Scripts\python.exe not found after venv creation - aborting."
    exit 1
}

# ----------------------------------------------------------------------------
# STEP 3 : Install dependencies
# ----------------------------------------------------------------------------
Write-Step 3 "Install Python dependencies"

if (-not (Test-Path $ReqFile)) {
    Write-Bad "requirements.txt not found at $ReqFile"
    exit 1
}

Write-Host "  Upgrading pip first..." -ForegroundColor DarkGray
& $VenvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Write-Bad "pip upgrade failed"; exit 1 }

Write-Host "  Installing requirements.txt..." -ForegroundColor DarkGray
& $VenvPython -m pip install -r $ReqFile
if ($LASTEXITCODE -ne 0) { Write-Bad "pip install failed"; exit 1 }
Write-OK "Dependencies installed"

# Dev deps - optional
$ReqDev = Join-Path $RepoRoot "requirements-dev.txt"
if (Test-Path $ReqDev) {
    $installDev = Read-Host "  Install dev dependencies (pytest etc)? [y/N]"
    if ($installDev -match '^[Yy]') {
        & $VenvPython -m pip install -r $ReqDev
        if ($LASTEXITCODE -eq 0) { Write-OK "Dev deps installed" }
    } else {
        Write-Skip "Dev deps skipped"
    }
}

# ----------------------------------------------------------------------------
# STEP 4 : .env file (interactive setup)
# ----------------------------------------------------------------------------
Write-Step 4 "Configure .env (secrets and per-machine settings)"

if (-not (Test-Path $EnvSample)) {
    Write-Bad ".env.example missing - cannot template .env"
    exit 1
}

$writeEnv = $true
if ((Test-Path $EnvFile) -and -not $Force) {
    Write-Skip ".env already exists - keeping it (pass -Force to overwrite)"
    $writeEnv = $false
}

if ($writeEnv) {
    Write-Host ""
    Write-Host "  Each value is optional - just press Enter to leave empty." -ForegroundColor DarkGray
    Write-Host "  You can also edit .env manually later." -ForegroundColor DarkGray
    Write-Host ""

    function Ask($label, $hint) {
        Write-Host "  > $label" -ForegroundColor White
        if ($hint) { Write-Host "    $hint" -ForegroundColor DarkGray }
        $val = Read-Host "    value (Enter to skip)"
        return $val
    }

    $proxy   = Ask "TRENDRADAR_PROXIES"   "HTTP proxy for Google News / arxiv / HF (e.g. http://127.0.0.1:7890)"
    $cookie  = Ask "TRENDRADAR_ZHIHU_COOKIE" "Zhihu cookie (paste full cookie string from browser DevTools)"
    $llmKey  = Ask "TRENDRADAR_LLM_API_KEY"  "DeepSeek API key for LLM auto-tagging (sk-...)"
    $secUA   = Ask "SEC_USER_AGENT"          "SEC EDGAR contact, format: 'YourProject your@email.com'"

    # Escape any literal double-quotes the user pasted (rare but possible)
    function Quote([string]$s) {
        if ($null -eq $s) { return '""' }
        $s = $s -replace '"', '\"'
        return "`"$s`""
    }

    $envContent = @"
# TrendRadarCN runtime config. Auto-loaded by run.py.
# DO NOT COMMIT - already in .gitignore.
# Generated by scripts/setup.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm')

# HTTP proxy for Google News / HuggingFace / arxiv
TRENDRADAR_PROXIES=$(Quote $proxy)

# Zhihu login cookie
TRENDRADAR_ZHIHU_COOKIE=$(Quote $cookie)

# DeepSeek LLM key for ai-cn / ai-frontier auto-tagging
TRENDRADAR_LLM_API_KEY=$(Quote $llmKey)
TRENDRADAR_LLM_BASE_URL="https://api.deepseek.com"
TRENDRADAR_LLM_MODEL="deepseek-chat"

# SEC EDGAR contact (required by SEC fair-use policy)
SEC_USER_AGENT=$(Quote $secUA)
"@

    Set-Content -Path $EnvFile -Value $envContent -Encoding UTF8
    Write-OK ".env written to $EnvFile"
}

# Quick parse-back sanity check
$verifyCmd = "from dotenv import dotenv_values; v=dotenv_values(r'$EnvFile'); print('keys=', len(v))"
$verifyOut = & $VenvPython -c $verifyCmd 2>$null
if ($verifyOut -match 'keys=\s*(\d+)') {
    Write-OK ".env parsed successfully ($($matches[1]) keys loaded)"
} else {
    Write-Warn2 "Could not verify .env parsing - check it manually with: type .env"
}

# ----------------------------------------------------------------------------
# STEP 5 : Initialize the SQLite database
# ----------------------------------------------------------------------------
Write-Step 5 "Initialize SQLite database"

$initOut = & $VenvPython -c "from app.db import init_db; init_db(); print('OK')" 2>&1
if ($LASTEXITCODE -eq 0 -and ($initOut -match 'OK')) {
    Write-OK "Database initialized (tables created / migrated)"
} else {
    Write-Warn2 "DB init returned non-zero. Output:"
    Write-Host $initOut -ForegroundColor DarkGray
    Write-Host "  This is often harmless (e.g. tables already exist)." -ForegroundColor DarkGray
}

# ----------------------------------------------------------------------------
# STEP 6 : Optional - install scheduled task
# ----------------------------------------------------------------------------
if (-not $SkipTask) {
    Write-Step 6 "Optional: install daily scheduled task (07:30)"

    $installTask = Read-Host "  Install Windows scheduled task that runs all boards daily at 07:30? [y/N]"
    if ($installTask -match '^[Yy]') {
        & (Join-Path $PSScriptRoot "install_scheduled_task.ps1")
    } else {
        Write-Skip "Scheduled task NOT installed. You can install it later with:"
        Write-Host "         .\scripts\install_scheduled_task.ps1" -ForegroundColor DarkGray
    }
}

# ----------------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------------
Write-Host ""
Write-Host "==================================================================" -ForegroundColor Green
Write-Host " Setup complete!" -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
Write-Host ""
Write-Host " Next steps:" -ForegroundColor White
Write-Host ""
Write-Host "   .\.venv\Scripts\Activate.ps1            # activate venv for this terminal"
Write-Host "   python run.py                           # start the web dashboard on :8001"
Write-Host "   python run.py --board all               # one-shot fetch all 4 boards"
Write-Host "   python run.py --board my-portfolio      # fetch only one board"
Write-Host ""
Write-Host " Then open  http://127.0.0.1:8001  in your browser." -ForegroundColor White
Write-Host ""
