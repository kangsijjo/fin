@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Paper Trading daily pipeline
REM  steps: pykrx_collector -> update_macro
REM         -> paper_tracker
REM         -> ai_training(Friday only) -> dashboard_generator
REM  [2026-07-07] live_signal step REMOVED: signals are generated at 18:30
REM         by StockAI\LiveSignal on finalized EOD data. Running it here
REM         (15:50) could record signals from incomplete data permanently
REM         (dedup key (date,code,strategy) blocks later correction).
REM  [2026-07-07] exit_rule_engine step REMOVED: file no longer exists,
REM         caused daily exit code 1 (KIS_Paper task shown as failed).
REM  NOTE: ASCII-only comments. cmd reads .bat in OEM codepage,
REM        so UTF-8 Korean here would corrupt parsing (col-lector bug).
REM  Run with "auto" arg from scheduler so it does not pause.
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
set "LOGFILE=!LOGDIR!\paper_!TODAY!.log"

Forfiles /P "!LOGDIR!" /M paper_*.log /D -30 /C "cmd /c del @file" 2>nul

echo. >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"
echo  Start: %DATE% %TIME% >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"

if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
    goto :runsteps
)
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    set "PYEXE=py -3.11"
    goto :runsteps
)
echo [ERROR] python not found >> "!LOGFILE!"
set "EXITCODE=2"
goto :report

:runsteps
REM pykrx_collector writes stock.db (korea_stocks, supply_demand).
REM The scheduled owner of those tables is Stock_AI_Project (06:30 main_collector).
REM This call is manual backfill only. Keep schema in sync with Stock_AI.
echo === pykrx_collector === >> "!LOGFILE!"
!PYEXE! pykrx_collector.py >> "!LOGFILE!" 2>&1
if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
echo. >> "!LOGFILE!"
echo === update_macro === >> "!LOGFILE!"
!PYEXE! update_macro_daily.py >> "!LOGFILE!" 2>&1
if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
echo. >> "!LOGFILE!"
echo === paper_tracker === >> "!LOGFILE!"
!PYEXE! paper_tracker.py >> "!LOGFILE!" 2>&1
if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
echo. >> "!LOGFILE!"
REM AI training: weekly only (Friday DOW=5). Daily retrain adds noise.
if "!DOW!"=="5" (
    echo === ai_training weekly v3+v4 === >> "!LOGFILE!"
    !PYEXE! make_trades_history_v3.py >> "!LOGFILE!" 2>&1
    if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
    REM 2026-10-30 system doc 85: old meta-model ai_trainer_v4 retired - improved AI ai_model.py
    !PYEXE! ai_model.py --train >> "!LOGFILE!" 2>&1
    if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
) else (
    echo === ai_training skipped - weekly Friday only === >> "!LOGFILE!"
)
echo. >> "!LOGFILE!"
echo === dashboard === >> "!LOGFILE!"
!PYEXE! dashboard_generator.py >> "!LOGFILE!" 2>&1
if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"

:report
echo. >> "!LOGFILE!"
echo  Exit code: !EXITCODE! (time: %TIME%) >> "!LOGFILE!"
echo Exit code: !EXITCODE!
echo Log file: !LOGFILE!

if /i not "%1"=="auto" pause
set "RC=!EXITCODE!"
endlocal & exit /b %RC%

