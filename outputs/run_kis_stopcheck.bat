@echo off
:: run_kis_stopcheck.bat
:: Intraday stop MONITOR (every 15min). kis_trader.py stopcheck is monitor-only
:: since 2026-06-21 (NO auto-sell; EOD cmd_sell handles actual stops).
:: ASCII-only comments (cp949 parsing). Old comment claimed auto-sell - stale.
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

REM -- 주말 스킵 --
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" echo [SKIP] Sunday & endlocal & exit /b 0
if "!DOW!"=="6" echo [SKIP] Saturday & endlocal & exit /b 0

REM -- market hours only (09:00~15:30).
REM    [2026-07-12 fix] '0905' fails cmd octal parse (digit 9) -> string compare
REM    fallback -> '0905' < '900' TRUE -> ALL 09:xx runs skipped daily (first real
REM    run was 10:05). Prefix '1' forces same-width decimal compare (10905 vs 10900).
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format HHmm"') do set "HM=%%I"
if 1!HM! LSS 10900 echo [SKIP] before open & endlocal & exit /b 0
if 1!HM! GTR 11530 echo [SKIP] after close & endlocal & exit /b 0

REM -- 로그 --
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\kis_stop_%DT%.log"

echo [%date% %time%] stopcheck start >> "%LOGFILE%"

if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
    goto :run
)
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 ( set "PYEXE=py" & goto :run )
echo [ERROR] python not found >> "%LOGFILE%"
endlocal & exit /b 1

:run
!PYEXE! -u kis_trader.py stopcheck >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
echo [%date% %time%] stopcheck done EC=!EC! >> "%LOGFILE%"
Forfiles /P "C:\fin\logs" /M kis_stop_*.log /D -30 /C "cmd /c del @file" 2>nul
endlocal & exit /b %EC%
