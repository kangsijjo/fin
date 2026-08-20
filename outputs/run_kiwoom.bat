@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Kiwoom mock-trading executor (StockAI KiwoomTraderAM/PM tasks)
REM  arg1: "auto" = no pause (scheduler). arg2: "am" | "pm" (explicit mode).
REM  [2026-07-12] mode is now passed per-trigger (daily-am/daily-pm) instead of
REM  clock-splitting inside python - a StartWhenAvailable delayed recovery after
REM  noon used to run the SELL path for the 09:03 BUY trigger (silent lost buys
REM  + midday market sells). No arg2 = legacy clock split (manual runs).
REM ============================================================
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"
set "EXITCODE=0"
set "CMD=daily"
if /i "%2"=="am" set "CMD=daily-am"
if /i "%2"=="pm" set "CMD=daily-pm"

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
set "LOGFILE=!LOGDIR!\kiwoom_!TODAY!.log"
Forfiles /P "!LOGDIR!" /M kiwoom_*.log /D -30 /C "cmd /c del @file" 2>nul

REM [2026-08-20] AM and PM append to the SAME daily log. A delayed AM recovery can
REM overlap the PM run, so "attempt 2" from one instance appeared right after
REM "attempt 1 exit=0" of the other and read like a broken retry (08-14 confusion).
REM Stamping the mode on every marker line makes interleaved runs readable.
echo ============================================================ >> "!LOGFILE!"
echo  Kiwoom start [!CMD!]: %DATE% %TIME% >> "!LOGFILE!"
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
REM Retry loop (2026-07-21): transient API failures (token 429 on 07-21 09:03 etc.)
REM killed runs. Re-run is SAFE: buys idempotent via db/kiwoom/orders_YYYYMMDD.csv
REM (same-day same-code skip), sells recompute from broker positions.
REM 2 retries, 5 min apart.
set "TRIES=0"
:attempt
set /a TRIES+=1
!PYEXE! kiwoom_trader.py !CMD! >> "!LOGFILE!" 2>&1
set "EXITCODE=!ERRORLEVEL!"
echo  [!CMD!] attempt !TRIES! exit=!EXITCODE! (time: %TIME%) >> "!LOGFILE!"
if !EXITCODE! NEQ 0 if !TRIES! LSS 3 (
    echo  [!CMD!] retry in 300s >> "!LOGFILE!"
    ping 127.0.0.1 -n 301 > nul
    goto :attempt
)

:report
echo  [!CMD!] Exit code: !EXITCODE! (time: %TIME%) >> "!LOGFILE!"
echo Exit code: !EXITCODE!
echo Log file: !LOGFILE!
if /i not "%1"=="auto" pause
set "RC=!EXITCODE!"
endlocal & exit /b %RC%
