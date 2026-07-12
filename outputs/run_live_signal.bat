@echo off
:: run_live_signal.bat
:: Weekday 18:30 live signal detection (called by Task Scheduler).
:: ASCII-only: cmd reads .bat in OEM codepage (cp949 on KR Windows); UTF-8/Korean
:: comments here previously broke parsing (task returned 0xFF, no log). Keep this file ASCII.
setlocal
set PYTHONIOENCODING=utf-8
if not exist C:\fin\logs mkdir C:\fin\logs

cd /d C:\fin\outputs

for /f "tokens=*" %%i in ('powershell -Command "Get-Date -Format yyyyMMdd_HHmm"') do set DT=%%i
set LOGFILE=C:\fin\logs\live_signal_%DT%.log

echo [%date% %time%] Live signal starting >> %LOGFILE%

if not exist .venv\Scripts\python.exe (
    echo [ERROR] .venv not found in C:\fin\outputs >> %LOGFILE%
    exit /b 1
)

.venv\Scripts\python.exe -u live_signal.py >> %LOGFILE% 2>&1
set "EC=%errorlevel%"
echo [%date% %time%] Live signal done. ExitCode=%EC% >> %LOGFILE%

:: gating (2026-07-05): if live_signal failed, skip strategy_lab (same loader -> would
:: fail/waste ~10min on bad data). daily_audit + daily_summary ALWAYS run below --
:: they are the REPORTERS (audit scans Traceback and pushes telegram warning).
if not "%EC%"=="0" (
    echo [%date% %time%] live_signal FAILED - skipping strategy_lab >> %LOGFILE%
    goto :audit
)

:: strategy lab (forward accumulation): same data, 29-strategy backtest -> forward aggregate.
:: deterministic, so a daily re-run auto-accumulates trades completed after LAB_START.
echo [%date% %time%] strategy_lab starting >> %LOGFILE%
.venv\Scripts\python.exe -u strategy_lab.py >> %LOGFILE% 2>&1
echo [%date% %time%] strategy_lab done. ExitCode=%errorlevel% >> %LOGFILE%

:audit
:: daily self-audit: one-page summary of today's automation to logs\daily_audit_DATE.log
.venv\Scripts\python.exe -u daily_audit.py >> %LOGFILE% 2>&1

:: daily summary telegram push (signals / holdings / cash, one message)
.venv\Scripts\python.exe -u daily_summary.py >> %LOGFILE% 2>&1
:: [2026-07-12] propagate live_signal exit code - previously the bat returned the
:: LAST command's code (daily_summary), so Task Scheduler history showed 0x0 even
:: when signal detection crashed.
endlocal & exit /b %EC%
