@echo off
REM Launcher for the PsygaBio Production System.
REM Bypasses PowerShell execution-policy issues by invoking the venv
REM Python directly - no `activate` step required.

setlocal
set "VENV=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV%" (
    echo [ERROR] Virtual environment not found at %VENV%
    echo Run:  uv venv ^&^& uv pip install -r requirements.txt
    exit /b 1
)
"%VENV%" "%~dp0app.py" %*
endlocal
