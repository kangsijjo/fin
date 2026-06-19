@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  KRX credit + short balance auto-runner (T-1 data)
REM ============================================================

cd /d "%~dp0"
set "EXITCODE=0"

REM Get day-of-week (skip weekends since KRX data is T-1, run on weekday for yesterday)
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd')"') do set "TODAY=%%I"

REM Sat=6, Sun=0
if "!DOW!"=="0" goto :weekend
if "!DOW!"=="6" goto :weekend
goto :weekday

:weekend
echo [SKIP] Weekend.
if /i not "%1"=="auto" pause
endlocal
exit /b 0

:weekday
set "LOGDIR=logs"
if not exist "!LOGDIR!" mkdir "!LOGDIR!"
set "LOGFILE=!LOGDIR!\krx_!TODAY!.log"

Forfiles /P "!LOGDIR!" /M krx_*.log /D -30 /C "cmd /c del @file" 2>nul

echo. >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"
echo  Start: %DATE% %TIME% >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"

REM Prefer venv python if available (where pykrx etc. are installed)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" krx_collector.py both >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    py -3.11 krx_collector.py both >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)
where python >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    python krx_collector.py both >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)
set "EXITCODE=2"
echo [ERROR] python not found >> "!LOGFILE!"

:report
echo  Exit code: !EXITCODE! (time: %TIME%) >> "!LOGFILE!"

if /i not "%1"=="auto" pause
set "RC=!EXITCODE!"
endlocal & exit /b %RC%
