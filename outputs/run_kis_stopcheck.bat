@echo off
:: run_kis_stopcheck.bat
:: 장중 15분마다 KIS 모의계좌 손절 점검(stop-only). 만기청산은 건드리지 않음.
:: kis_trader.py stopcheck -> 라이브 현재가로 진입가 대비 손절 발동분만 시장가 매도.
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

REM -- 주말 스킵 --
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" echo [SKIP] Sunday & endlocal & exit /b 0
if "!DOW!"=="6" echo [SKIP] Saturday & endlocal & exit /b 0

REM -- 장중(09:00~15:30)만 실행: HHMM 정수 비교 --
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format HHmm"') do set "HM=%%I"
if !HM! LSS 900 echo [SKIP] before open & endlocal & exit /b 0
if !HM! GTR 1530 echo [SKIP] after close & endlocal & exit /b 0

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
