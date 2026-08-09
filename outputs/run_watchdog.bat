@echo off
REM run_watchdog.bat [--daily] - silent-failure detector -> Telegram.
REM   Args pass through to watchdog.py. --daily = end-of-day heartbeat (sent even when healthy).
REM   Schedule: 3x daily (no args) + 23:00 (--daily). See the watchdog.py header for details.
setlocal EnableExtensions
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOG=C:\fin\logs\watchdog_%DT%.log"

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

"%PYEXE%" -u watchdog.py %* >> "%LOG%" 2>&1
set "EC=%errorlevel%"
echo [%date% %time%] watchdog done args=%* EC=%EC% >> "%LOG%"
Forfiles /P "C:\fin\logs" /M watchdog_*.log /D -30 /C "cmd /c del @file" 2>nul
REM [2026-07-12] propagate watchdog exit code (was: last command = Forfiles -> always 0,
REM so a crashed watchdog looked successful in Task Scheduler history)
endlocal & exit /b %EC%
