@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Data collection batch (KIS_EOD 15:40 / KIS_Ranking 14:30)
REM  Runs data_collector.py and appends output to logs\collect_YYYYMMDD.log
REM
REM  [2026-07-21] PYTHONIOENCODING: without it the collector wrote CP949 logs
REM  and the dashboard tail (byte-sliced) could not decode them.
REM  [2026-08-09] ASCII-ONLY. This file previously carried Korean REM comments
REM  stored as CP949; re-saving it as UTF-8 made cmd.exe mis-parse whole lines
REM  ("'ate).DayOfWeek...' is not recognized") so the weekend guard and the log
REM  redirect silently broke. Keep every line ASCII - no Korean in .bat files.
REM ============================================================
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"
set "EXITCODE=0"

REM --- day-of-week and date via two PowerShell calls (escape-safe) ---
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd')"') do set "TODAY=%%I"

REM --- skip weekends (Sat=6, Sun=0) ---
if "!DOW!"=="0" goto :weekend
if "!DOW!"=="6" goto :weekend
goto :weekday

:weekend
echo [SKIP] Weekend (DOW=!DOW!). Market closed.
if /i not "%1"=="auto" pause
endlocal
exit /b 0

:weekday
set "LOGDIR=logs"
if not exist "!LOGDIR!" mkdir "!LOGDIR!"
set "LOGFILE=!LOGDIR!\collect_!TODAY!.log"

REM --- delete log files older than 30 days (maintenance-free) ---
Forfiles /P "!LOGDIR!" /M *.log /D -30 /C "cmd /c del @file" 2>nul

echo. >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"
echo  Start: %DATE% %TIME% >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"

REM Check .env exists
if not exist ".env" (
    echo [ERROR] .env file not found in %CD%
    echo [ERROR] .env file not found in %CD% >> "!LOGFILE!"
    set "EXITCODE=1"
    goto :end
)

REM Prefer venv python (where project deps are installed)
if exist ".venv\Scripts\python.exe" (
    echo [INFO] running with: .venv\Scripts\python.exe
    ".venv\Scripts\python.exe" data_collector.py today >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)

where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    echo [INFO] running with: py -3.11
    py -3.11 data_collector.py today >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)

where python >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    echo [INFO] running with: python
    python data_collector.py today >> "!LOGFILE!" 2>&1
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
