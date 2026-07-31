@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Pre-market warmup (StockAI\PretradeWarmup task, 08:50 weekdays)
REM  Verifies strength records (recomputes if missing) + warms API tokens
REM  so that the 09:01/09:03 traders fire immediately. Balance is NOT
REM  pre-fetched here - it must be read after the morning stop-loss sells.
REM ============================================================
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

REM -- weekend skip --
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" echo [SKIP] Sunday & endlocal & exit /b 0
if "!DOW!"=="6" echo [SKIP] Saturday & endlocal & exit /b 0

if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\pretrade_warmup_%DT%.log"

echo [%date% %time%] warmup starting >> "%LOGFILE%"

if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    set "PYEXE=python"
)

!PYEXE! -u pretrade_warmup.py >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
echo [%date% %time%] warmup done. ExitCode=!EC! >> "%LOGFILE%"

Forfiles /P "C:\fin\logs" /M pretrade_warmup_*.log /D -30 /C "cmd /c del @file" 2>nul
endlocal & exit /b %EC%
