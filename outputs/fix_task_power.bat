@echo off
:: fix_task_power.bat - allow scheduled tasks to start/continue on battery power.
:: Right-click -> Run as administrator. ASCII-only.
setlocal
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Not running as Administrator.
    echo Right-click this file and select "Run as administrator".
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\fin\outputs\fix_task_power.ps1"
echo.
pause
endlocal
