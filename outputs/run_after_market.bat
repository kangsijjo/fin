@echo off
REM run_after_market.bat [--probe] - after-hours single-price movers -> stock.db after_market.
REM Runs ~18:10, after the 16:00-18:00 after-hours session. Args pass through to after_market.py.
setlocal EnableExtensions
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOG=C:\fin\logs\after_market_%DT%.log"

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

"%PYEXE%" -u after_market.py %* >> "%LOG%" 2>&1
set "EC=%errorlevel%"
echo [%date% %time%] after_market done args=%* EC=%EC% >> "%LOG%"
Forfiles /P "C:\fin\logs" /M after_market_*.log /D -30 /C "cmd /c del @file" 2>nul
REM [2026-07-12] propagate exit code (was: Forfiles -> always 0 in scheduler history)
endlocal & exit /b %EC%
