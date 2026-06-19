@echo off
:: run_kis_trader.bat
:: 평일 09:01 KIS 모의매매 실행 (작업 스케줄러 + 부팅 시 자동 실행)
:: kis_trader.py daily → sell(만기/stop) → buy(신규 신호)
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

REM ── 주말 스킵 ──────────────────────────────────────────────
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" echo [SKIP] Sunday - 종료 & endlocal & exit /b 0
if "!DOW!"=="6" echo [SKIP] Saturday - 종료 & endlocal & exit /b 0

REM ── 로그 설정 ──────────────────────────────────────────────
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\kis_trader_%DT%.log"

echo [%date% %time%] KIS trader starting >> "%LOGFILE%"

REM ── Python 탐색 ────────────────────────────────────────────
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
!PYEXE! -u kis_trader.py daily >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
echo [%date% %time%] KIS trader done. ExitCode=!EC! >> "%LOGFILE%"

REM ── 30일 이전 로그 정리 ───────────────────────────────────
Forfiles /P "C:\fin\logs" /M kis_trader_*.log /D -30 /C "cmd /c del @file" 2>nul

endlocal & exit /b %EC%
