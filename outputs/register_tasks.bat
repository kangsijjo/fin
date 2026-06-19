@echo off
:: register_tasks.bat
:: Right-click -> Run as Administrator
setlocal
echo ============================================
echo   Stock AI Task Scheduler Registration
echo   (Must run as Administrator)
echo ============================================
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Not running as Administrator.
    echo Right-click this file and select "Run as administrator".
    pause
    exit /b 1
)

powershell -ExecutionPolicy Bypass -File "C:\fin\outputs\register_tasks.ps1"

echo.
pause
endlocal
