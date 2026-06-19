@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Paper Trading daily 15:50
REM  steps: update_macro -> live_signal -> paper_tracker
REM         -> exit_rule_engine -> ai_training(Friday only) -> dashboard
REM  [2026-06-18] us_market_collector.py 제거됨 — macro_collector가 indicators.csv에 미국 데이터 수집
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
REM [2026-06-19 가드] pykrx_collector 는 stock.db(korea_stocks, supply_demand)에 쓴다.
REM   이 테이블의 정기 수집 소유자는 Stock_AI_Project(06:30 main_collector)다.
REM   여기서의 호출은 수동 백필/보강용. 스키마가 Stock_AI 와 갈라지면 drift 위험 →
REM   Stock_AI 쪽 스키마 변경 시 pykrx_collector 도 같이 맞출 것. 정기 자동수집을 천억이로 되돌리지 말 것.
echo === pykrx_collector === >> "!LOGFILE!"
!PYEXE! pykrx_collector.py >> "!LOGFILE!" 2>&1
if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
echo. >> "!LOGFILE!"
echo === update_macro === >> "!LOGFILE!"
!PYEXE! update_macro_daily.py >> "!LOGFILE!" 2>&1
if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
echo. >> "!LOGFILE!"
REM us_market_collector.py 제거됨(2026-06-18) — macro_collector.py가 indicators.csv에 미국 데이터 수집
echo === live_signal === >> "!LOGFILE!"
!PYEXE! live_signal.py >> "!LOGFILE!" 2>&1
if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
echo. >> "!LOGFILE!"
echo === paper_tracker === >> "!LOGFILE!"
!PYEXE! paper_tracker.py >> "!LOGFILE!" 2>&1
if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
echo. >> "!LOGFILE!"
echo === exit_rule_engine === >> "!LOGFILE!"
!PYEXE! exit_rule_engine.py >> "!LOGFILE!" 2>&1
if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
echo. >> "!LOGFILE!"
REM AI training: weekly (Friday DOW=5). Daily retrain adds noise, not signal.
if "!DOW!"=="5" (
    echo === ai_training weekly v3+v4 === >> "!LOGFILE!"
    !PYEXE! make_trades_history_v3.py >> "!LOGFILE!" 2>&1
    if !ERRORLEVEL! NEQ 0 set "EXITCODE=1"
    !PYEXE! ai_trainer_v4.py >> "!LOGFILE!" 2>&1
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
