@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  20:00 collection verify + re-collect guard (recollect_guard.py)
REM ============================================================
set PYTHONIOENCODING=utf-8

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
set "LOGFILE=!LOGDIR!\recheck_!TODAY!.log"
Forfiles /P "!LOGDIR!" /M recheck_*.log /D -30 /C "cmd /c del @file" 2>nul

echo ============================================================ >> "!LOGFILE!"
echo  Recheck start: %DATE% %TIME% >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"

if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
    goto :run
)
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    set "PYEXE=py -3.11"
    goto :run
)
echo [ERROR] python not found >> "!LOGFILE!"
set "EXITCODE=2"
goto :report

:run
!PYEXE! recollect_guard.py >> "!LOGFILE!" 2>&1
set "EXITCODE=!ERRORLEVEL!"

:report
echo  Exit code: !EXITCODE! (time: %TIME%) >> "!LOGFILE!"
echo Exit code: !EXITCODE!
echo Log file: !LOGFILE!
if /i not "%1"=="auto" pause
set "RC=!EXITCODE!"
endlocal & exit /b %RC%
