@echo off
:: run_kis_signal.bat
:: Weekday 18:31 KIS signal detection (Task Scheduler). ASCII-only (cmd reads bat in OEM cp).
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

REM -- [2026-10-09] weekend skip REMOVED. Triggers are weekdays-only, so a weekend run is
REM    always a StartWhenAvailable catch-up (PC off Fri 18:31) - skipping it lost Friday's
REM    KIS signals. Re-running on the same data is safe (dedup -> 0 new). live_signal has
REM    never had a weekend skip.

REM -- log setup --
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\kis_signal_%DT%.log"

echo [%date% %time%] KIS signal starting >> "%LOGFILE%"

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
REM -- [2026-10-09] wait (max 30 min) until the universe (macro_data/daily) has the
REM    expected date. After a PC-off period, StartWhenAvailable fires this job together
REM    with the KIS_Paper collector; on 10-08 11:20 signals ran 1 min BEFORE the data
REM    arrived -> '0 new' -> manual re-run. Fresh = returns at once. Timeout = telegram
REM    alert and signals still run (exit code ignored on purpose).
!PYEXE! -u market_calendar.py --wait-fresh 30 --label kis_signal >> "%LOGFILE%" 2>&1
!PYEXE! -u kis_live_signal.py >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
echo [%date% %time%] KIS signal done. ExitCode=!EC! >> "%LOGFILE%"

REM -- AI/strength virtual portfolios (ledger sim, deterministic recompute; both signal sets ready by 18:31) --
!PYEXE! -u ai_paper_trader.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] ai_paper done. ExitCode=!ERRORLEVEL! >> "%LOGFILE%"

Forfiles /P "C:\fin\logs" /M kis_signal_*.log /D -30 /C "cmd /c del @file" 2>nul
endlocal & exit /b %EC%

