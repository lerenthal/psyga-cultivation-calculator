@echo off
REM Hidden background launcher for the PsygaBio Production System.
REM Started by run_hidden.vbs (no console window). Output goes to
REM app.log in the project directory. Stop via Task Manager
REM (end python.exe) or by rebooting.
setlocal
cd /d "%~dp0"
set "VENV=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV%" (
    echo [%date% %time%] [ERROR] venv not found at %VENV% >> app.log
    exit /b 1
)
if "%PORT%"=="" set "PORT=5000"
"%VENV%" "%~dp0app.py" >> app.log 2>&1
endlocal
