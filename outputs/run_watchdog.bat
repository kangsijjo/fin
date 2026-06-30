@echo off
REM run_watchdog.bat [--daily] - 조용한 실패 감지 → 텔레그램.
REM   인자(--daily 등)는 watchdog.py 로 그대로 전달. --daily = 하루 마감 하트비트(정상이어도 전송).
REM   예약: 평상시 3회(인자없음) + 23:00 1회(--daily). 자세한 설명은 watchdog.py 상단.
setlocal EnableExtensions
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOG=C:\fin\logs\watchdog_%DT%.log"

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

"%PYEXE%" -u watchdog.py %* >> "%LOG%" 2>&1
echo [%date% %time%] watchdog done args=%* >> "%LOG%"
Forfiles /P "C:\fin\logs" /M watchdog_*.log /D -30 /C "cmd /c del @file" 2>nul
endlocal
