@echo off
:: run_intraday_preview.bat
:: 장중 매시간 '잠정 예비후보' 미리보기 갱신(정보용, 매매 아님).
:: intraday_preview.py -> pykrx 장중 잠정 OHLCV 로 신호조건 평가 -> db/kiwoom/intraday_preview.json
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM -- 주말 스킵 --
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" endlocal & exit /b 0
if "!DOW!"=="6" endlocal & exit /b 0

REM -- 장중(09:00~15:30)만 --
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format HHmm"') do set "HM=%%I"
if !HM! LSS 900 endlocal & exit /b 0
if !HM! GTR 1530 endlocal & exit /b 0

if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\intraday_preview_%DT%.log"

if exist ".venv\Scripts\python.exe" ( set "PYEXE=.venv\Scripts\python.exe" & goto :run )
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 ( set "PYEXE=py" & goto :run )
echo [ERROR] python not found >> "%LOGFILE%"
endlocal & exit /b 1

:run
!PYEXE! -u intraday_preview.py >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
Forfiles /P "C:\fin\logs" /M intraday_preview_*.log /D -14 /C "cmd /c del @file" 2>nul
endlocal & exit /b %EC%
