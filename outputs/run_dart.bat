@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  DART disclosures auto-runner (today's filings)
REM ============================================================

cd /d "%~dp0"
set "EXITCODE=0"

for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd')"') do set "TODAY=%%I"

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
set "LOGFILE=!LOGDIR!\dart_!TODAY!.log"

Forfiles /P "!LOGDIR!" /M dart_*.log /D -30 /C "cmd /c del @file" 2>nul

echo. >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"
echo  Start: %DATE% %TIME% >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"

REM Prefer venv python if available
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" dart_collector.py today >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    py -3.11 dart_collector.py today >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)
where python >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    python dart_collector.py today >> "!LOGFILE!" 2>&1
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
