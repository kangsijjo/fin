@echo off
:: run_kis_status.bat
:: 평일 15:40 KIS 모의계좌 잔고 스냅샷 갱신 (장마감 후)
:: kis_trader.py status → 실제 API 잔고조회 → db/kiwoom/kis_snapshot.json 갱신
:: 대시보드가 항상 당일 잔고를 보여주도록 한다.
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

REM ── 주말 스킵 ──────────────────────────────────────────────
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" echo [SKIP] Sunday & endlocal & exit /b 0
if "!DOW!"=="6" echo [SKIP] Saturday & endlocal & exit /b 0

REM ── 로그 설정 ──────────────────────────────────────────────
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\kis_status_%DT%.log"

echo [%date% %time%] KIS status (balance refresh) starting >> "%LOGFILE%"

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
!PYEXE! -u kis_trader.py status >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
echo [%date% %time%] KIS status done. ExitCode=!EC! >> "%LOGFILE%"

REM ── 30일 이전 로그 정리 ───────────────────────────────────
Forfiles /P "C:\fin\logs" /M kis_status_*.log /D -30 /C "cmd /c del @file" 2>nul

endlocal & exit /b %EC%
