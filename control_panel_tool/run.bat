@echo off
REM Launcher for the Server Control Panel (Windows).
REM Bypasses PowerShell execution-policy issues by invoking the venv
REM Python directly - no `activate` step required.

setlocal
set "VENV=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV%" (
    echo [ERROR] Virtual environment not found at %VENV%
    echo Run:  uv venv ^&^& uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    exit /b 1
)
if "%PANEL_PASSWORD%"=="" (
    echo [INFO] PANEL_PASSWORD is not set - a password will be generated
    echo        and stored in instance\panel_password.txt
)
"%VENV%" "%~dp0dashboard.py" %*
endlocal
