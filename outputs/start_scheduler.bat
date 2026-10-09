@echo off
setlocal
set PYTHONIOENCODING=utf-8

if not exist "C:\fin\logs" mkdir "C:\fin\logs"
set "LOG=C:\fin\logs\scheduler.log"

echo [%date% %time%] start_scheduler START >> "%LOG%"

cd /d C:\fin\Stock_AI_Project
echo [%date% %time%] CWD OK >> "%LOG%"

if not exist "venv\Scripts\python.exe" (
    echo [%date% %time%] ERROR: venv not found >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] 90s wait... >> "%LOG%"
ping 127.0.0.1 -n 91 > nul

echo [%date% %time%] Starting scheduler >> "%LOG%"
start "StockAI-Scheduler" /min cmd /c "venv\Scripts\python.exe -u scheduler.py >> C:\fin\logs\scheduler.log 2>&1"
echo [%date% %time%] done >> "%LOG%"
endlocal

