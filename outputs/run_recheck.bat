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

REM [2026-09-19] Phase-1 per-strategy threshold monitor. Aggregation only.
REM   Never changes EXITCODE - this is a reference tally, not a watched job.
REM   Writes db/strength_cut_monitor.csv, shown on the dashboard cutmon tab.
!PYEXE! strength_cut_monitor.py >> "!LOGFILE!" 2>&1
echo [cutmon] exit=!ERRORLEVEL! (monitor only) >> "!LOGFILE!"

REM [2026-10-12] KOSDAQ short-selling volume share + fundamentals -> macro_data\shortfund_kosdaq
REM   (input of strategy h52w_short_dec). Last 30 trading days, existing days skipped, so a
REM   missed evening self-heals. Up to yesterday only (today may be unpublished at 20:00).
REM   pykrx/KRX web path - independent of KIS/Kiwoom keys. Never changes EXITCODE.
!PYEXE! -u backfill_shorting_fundamental.py --market KOSDAQ --days 30 --until-yesterday >> "!LOGFILE!" 2>&1
echo [shortfund] exit=!ERRORLEVEL! (data refresh only) >> "!LOGFILE!"

REM [2026-10-18] Individual (retail) net buying -> stock.db supply_demand.individual_net_value
REM   (input of the 6 supply_* strategies). It was backfilled once on 2026-09-08 and never
REM   scheduled, so it silently stopped there. Window = last 45 calendar days; only dates with
REM   no individual value at all are fetched, so a missed evening self-heals. KRX login (.env).
REM   Never changes EXITCODE.
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).AddDays(-45).ToString('yyyyMMdd')"') do set "IND_LO=%%I"
!PYEXE! -u backfill_individual.py --backfill !IND_LO! !TODAY! >> "!LOGFILE!" 2>&1
echo [individual] exit=!ERRORLEVEL! (data refresh only) >> "!LOGFILE!"

:report
echo  Exit code: !EXITCODE! (time: %TIME%) >> "!LOGFILE!"
echo Exit code: !EXITCODE!
echo Log file: !LOGFILE!
if /i not "%1"=="auto" pause
set "RC=!EXITCODE!"
endlocal & exit /b %RC%
