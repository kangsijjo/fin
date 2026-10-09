@echo off
:: run_kis_trader.bat [am|pm]
:: KIS mock trading (Task Scheduler + at logon). ASCII-only.
:: arg1 "am" (09:01): stop-sell + buy  /  "pm" (15:21): expiry-sell at close auction.
:: no arg = legacy single-trigger daily (expire+stop at 09:01) for backward compat.
:: [2026-07-17] split so expiry sells at CLOSE (matches backtest) instead of open.
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

REM -- weekend skip --
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" echo [SKIP] Sunday & endlocal & exit /b 0
if "!DOW!"=="6" echo [SKIP] Saturday & endlocal & exit /b 0
REM -- [2026-10-09] KRX holiday skip (krx_holidays.txt via is_krx_holiday.ps1; fail-open) --
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0is_krx_holiday.ps1" >nul 2>&1
if errorlevel 10 echo [SKIP] KRX holiday & endlocal & exit /b 0

REM -- log setup --
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\kis_trader_%DT%.log"

echo [%date% %time%] KIS trader starting >> "%LOGFILE%"

REM -- python --
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
    goto :run
)
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    set "PYEXE=py"
    goto :run
)
echo [ERROR] python not found >> "%LOGFILE%"
endlocal & exit /b 1

:run
set "CMD=daily"
if /i "%1"=="am" set "CMD=daily-am"
if /i "%1"=="pm" set "CMD=daily-pm"

REM Retry loop (2026-07-21): transient API failures (429/500/ReadTimeout) killed runs
REM (real cases: 07-17 15:21 PM ReadTimeout, 07-21 10:51 balance 500). Re-run is SAFE:
REM buys are idempotent via kis_orders_YYYYMMDD.csv (same-day same-code skip), sells
REM recompute from broker balance+ledger. 2 retries, 5 min apart - a PM crash at 15:22
REM retries 15:27, still inside the 15:20-15:30 closing auction.
set "TRIES=0"
:attempt
set /a TRIES+=1
!PYEXE! -u kis_trader.py !CMD! >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
echo [%date% %time%] KIS trader attempt !TRIES! done. ExitCode=!EC! >> "%LOGFILE%"
REM [2026-08-26] Two hard lessons are baked into the block below.
REM  1) NEVER put a REM with an odd number of double quotes inside a ( ) block.
REM     A comment added on 08-21 contained an unbalanced quote and cmd lost track
REM     of the closing paren - the bat then died before writing a single log line
REM     ('/b' is not recognized ...). KisTraderAM ran with result=1 and produced
REM     no log at all on 08-24..08-26. Keep comments OUTSIDE the block.
REM  2) The post-wait marker exists because a retry that never starts and a retry
REM     that starts and dies look identical without it.
if !EC! NEQ 0 if !TRIES! LSS 3 (
    echo [%date% %time%] retry in 300s ^(attempt !TRIES!/3 failed^) >> "%LOGFILE%"
    ping 127.0.0.1 -n 301 > nul
    echo [%date% %time%] wait done - starting attempt !TRIES! >> "%LOGFILE%"
    goto :attempt
)

Forfiles /P "C:\fin\logs" /M kis_trader_*.log /D -30 /C "cmd /c del @file" 2>nul
endlocal & exit /b %EC%

