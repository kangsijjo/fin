@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Backtest auto-runner (mirrors run_collector.bat structure)
REM ============================================================

cd /d "%~dp0"
set "EXITCODE=0"

REM Get day-of-week and date via two PowerShell calls (escape-safe)
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd')"') do set "TODAY=%%I"

REM Skip weekends (Sat=6, Sun=0)
if "!DOW!"=="0" goto :weekend
if "!DOW!"=="6" goto :weekend
goto :weekday

:weekend
echo [SKIP] Weekend (DOW=!DOW!). Backtest skipped.
if /i not "%1"=="auto" pause
endlocal
exit /b 0

:weekday
set "LOGDIR=logs"
if not exist "!LOGDIR!" mkdir "!LOGDIR!"
set "LOGFILE=!LOGDIR!\backtest_!TODAY!.log"

REM Auto-delete log files older than 30 days
Forfiles /P "!LOGDIR!" /M backtest_*.log /D -30 /C "cmd /c del @file" 2>nul

echo. >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"
echo  Start: %DATE% %TIME% >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"

REM Prefer venv python (where project deps are installed)
if exist ".venv\Scripts\python.exe" (
    echo [INFO] running with: .venv\Scripts\python.exe
    ".venv\Scripts\python.exe" backtest.py >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)

where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    echo [INFO] running with: py -3.11
    py -3.11 backtest.py >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)

where python >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    echo [INFO] running with: python
    python backtest.py >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)

echo [ERROR] neither py nor python found on PATH
echo [ERROR] neither py nor python found on PATH >> "!LOGFILE!"
set "EXITCODE=2"
goto :end

:report
echo. >> "!LOGFILE!"
echo  Exit code: !EXITCODE! (time: %TIME%) >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"
echo.
echo Exit code: !EXITCODE!
echo Log file: !LOGFILE!

:end
if /i not "%1"=="auto" (
    echo.
    pause
)

set "RC=!EXITCODE!"
endlocal & exit /b %RC%
