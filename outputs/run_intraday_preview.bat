@echo off
:: run_intraday_preview.bat
:: Hourly intraday preview of provisional candidates (informational only, not trading).
:: intraday_preview.py -> evaluates signal rules on provisional pykrx OHLCV -> db/kiwoom/intraday_preview.json
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM -- skip weekends --
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" endlocal & exit /b 0
if "!DOW!"=="6" endlocal & exit /b 0

REM -- market hours only. [2026-07-12 fix] '0915' fails octal parse -> string
REM    compare bug skipped ALL 09:xx runs daily; '1' prefix forces decimal compare.
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format HHmm"') do set "HM=%%I"
if 1!HM! LSS 10900 endlocal & exit /b 0
if 1!HM! GTR 11530 endlocal & exit /b 0

if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\intraday_preview_%DT%.log"

if exist ".venv\Scripts\python.exe" ( set "PYEXE=.venv\Scripts\python.exe" & goto :run )
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 ( set "PYEXE=py" & goto :run )
echo [ERROR] python not found >> "%LOGFILE%"
endlocal & exit /b 1

:run
REM Market grid 2x6 (Kiwoom ranking, separate process) then the pykrx preview, in sequence
!PYEXE! -u market_grid.py >> "%LOGFILE%" 2>&1
!PYEXE! -u intraday_preview.py >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
Forfiles /P "C:\fin\logs" /M intraday_preview_*.log /D -14 /C "cmd /c del @file" 2>nul
endlocal & exit /b %EC%
