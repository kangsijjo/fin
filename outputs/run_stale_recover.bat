@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Stale-universe recovery (StockAI\StaleRecover, weekdays 13:00, every 30 min until 14:00)
REM  Only acts when this morning's buy was skipped because the universe lagged
REM  (db\stale_skip.json written by the traders). Then: collect missing days ->
REM  regenerate signals -> buy (before 15:00). See system doc 81.
REM ============================================================
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM -- weekend / KRX holiday skip (same as the other trading bats) --
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" echo [SKIP] Sunday & endlocal & exit /b 0
if "!DOW!"=="6" echo [SKIP] Saturday & endlocal & exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0is_krx_holiday.ps1" >nul 2>&1
if errorlevel 10 echo [SKIP] KRX holiday & endlocal & exit /b 0

if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\stale_recover_%DT%.log"

if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    set "PYEXE=python"
)

echo [%date% %time%] stale recover starting >> "%LOGFILE%"
!PYEXE! -u recover_stale.py >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
echo [%date% %time%] stale recover done. ExitCode=!EC! >> "%LOGFILE%"

Forfiles /P "C:\fin\logs" /M stale_recover_*.log /D -30 /C "cmd /c del @file" 2>nul
REM exit 2 = collection still failing, will retry on the next 30-min trigger (not a crash)
if "!EC!"=="2" endlocal & exit /b 0
endlocal & exit /b %EC%
