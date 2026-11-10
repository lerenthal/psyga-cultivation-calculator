@echo off
REM Test runner - bypasses the activate-script dance on Windows PowerShell.

setlocal
set "VENV=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV%" (
    echo [ERROR] Virtual environment not found at %VENV%
    exit /b 1
)
"%VENV%" -m pytest "%~dp0tests" -v %*
endlocal
