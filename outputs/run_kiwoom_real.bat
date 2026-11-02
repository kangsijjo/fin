@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Kiwoom REAL account executor (StockAI KiwoomRealAM/PM/Status tasks)  [2026-11-02 sec.96]
REM  Same logic as run_kiwoom.bat (mock) but KIWOOM_PROFILE=real:
REM    - real-server app key (.env KIWOOM_PROD_APP_KEY), folder db\kiwoom_real,
REM      settings = dashboard "kiwoom_real" card.
REM    - REAL ORDERS ONLY when mode = live (db\kiwoom_real\mode.json, set by the USER
REM      on the dashboard with a double confirmation). Default/missing = dryrun:
REM      everything is computed and the would-be orders are logged, nothing is sent.
REM  arg1: "auto" = no pause (scheduler). arg2: "am" | "pm" | "status".
REM  Log: logs\real_kiwoom_YYYYMMDD.log (name must NOT start with "kiwoom_" -
REM  watchdog globs kiwoom_*.log for the mock account). Logs are kept (no auto-delete).
REM ============================================================
set PYTHONIOENCODING=utf-8
set "KIWOOM_PROFILE=real"

cd /d "%~dp0"
set "EXITCODE=0"
set "CMD=status"
if /i "%2"=="am" set "CMD=daily-am"
if /i "%2"=="pm" set "CMD=daily-pm"
if /i "%2"=="status" set "CMD=status"

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
REM -- KRX holiday skip (krx_holidays.txt via is_krx_holiday.ps1; fail-open) --
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0is_krx_holiday.ps1" >nul 2>&1
if errorlevel 10 (
    echo [SKIP] KRX holiday.
    if /i not "%1"=="auto" pause
    endlocal
    exit /b 0
)
set "LOGDIR=logs"
if not exist "!LOGDIR!" mkdir "!LOGDIR!"
set "LOGFILE=!LOGDIR!\real_kiwoom_!TODAY!.log"

echo ============================================================ >> "!LOGFILE!"
echo  Kiwoom REAL start [!CMD!]: %DATE% %TIME% >> "!LOGFILE!"
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
REM Retry loop (same policy as run_kiwoom.bat): 2 retries, 5 min apart. Re-run is SAFE:
REM buys/sells are idempotent via db\kiwoom_real\orders_YYYYMMDD.csv AND the
REM pre-send record inflight_YYYYMMDD.csv (a crash between "order sent" and
REM "order logged" cannot cause a duplicate order on retry).
set "TRIES=0"
:attempt
set /a TRIES+=1
!PYEXE! kiwoom_trader.py !CMD! >> "!LOGFILE!" 2>&1
set "EXITCODE=!ERRORLEVEL!"
echo  [!CMD!] attempt !TRIES! exit=!EXITCODE! (time: %TIME%) >> "!LOGFILE!"
if !EXITCODE! NEQ 0 if !TRIES! LSS 3 (
    echo  [!CMD!] retry in 300s >> "!LOGFILE!"
    ping 127.0.0.1 -n 301 > nul
    echo  [!CMD!] wait done - starting attempt >> "!LOGFILE!"
    goto :attempt
)

:report
echo  [!CMD!] Exit code: !EXITCODE! (time: %TIME%) >> "!LOGFILE!"
echo Exit code: !EXITCODE!
echo Log file: !LOGFILE!
if /i not "%1"=="auto" pause
set "RC=!EXITCODE!"
endlocal & exit /b %RC%
