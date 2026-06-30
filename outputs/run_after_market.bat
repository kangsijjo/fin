@echo off
REM run_after_market.bat [--probe] - 시간외 단일가 등락률 순위 수집 -> stock.db after_market 누적.
REM 시간외 단일가(16:00~18:00) 종료 후 ~18:10 실행. 인자는 after_market.py 로 그대로 전달(--probe 등).
setlocal EnableExtensions
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOG=C:\fin\logs\after_market_%DT%.log"

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

"%PYEXE%" -u after_market.py %* >> "%LOG%" 2>&1
echo [%date% %time%] after_market done args=%* >> "%LOG%"
Forfiles /P "C:\fin\logs" /M after_market_*.log /D -30 /C "cmd /c del @file" 2>nul
endlocal
