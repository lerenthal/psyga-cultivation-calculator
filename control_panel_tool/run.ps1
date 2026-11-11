# Launcher for the Server Control Panel (PowerShell).
# Bypasses execution-policy issues by invoking the venv Python directly.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    Write-Host "[ERROR] Virtual environment not found at $VenvPy" -ForegroundColor Red
    Write-Host "Run:  uv venv" -ForegroundColor Yellow
    exit 1
}

if (-not $env:PANEL_PASSWORD) {
    Write-Host "[INFO] PANEL_PASSWORD is not set - a password will be generated" -ForegroundColor Yellow
}

& $VenvPy (Join-Path $ScriptDir "dashboard.py") @args
