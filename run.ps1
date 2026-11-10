# Launcher for the PsygaBio Production System (PowerShell).
# Bypasses execution-policy issues by invoking the venv Python directly.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    Write-Host "[ERROR] Virtual environment not found at $VenvPy" -ForegroundColor Red
    Write-Host "Run:  uv venv && uv pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

& $VenvPy (Join-Path $ScriptDir "app.py") @args
