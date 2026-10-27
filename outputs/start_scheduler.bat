@echo off
setlocal
REM ============================================================
REM  Stock_AI scheduler start / restart (the ONLY scheduler launcher).
REM  - Task Scheduler (StockAI\Scheduler) runs this at logon.
REM  - Dashboard [scheduler restart] button and double-click = clean restart:
REM    the running scheduler (python scheduler.py + any run_scheduler.bat loop)
REM    is stopped first, so there is never a second copy running the same jobs.
REM  - 90s wait only right after boot (uptime under 5 min); manual runs start at once.
REM  [2026-10-27] before this, the restart button started a SECOND scheduler.
REM ============================================================
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

powershell -NoProfile -Command "if (((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalMinutes -lt 5) { exit 1 } else { exit 0 }"
if errorlevel 1 goto bootwait
echo [%date% %time%] manual run - no wait >> "%LOG%"
goto stopold
:bootwait
echo [%date% %time%] just booted - 90s wait... >> "%LOG%"
ping 127.0.0.1 -n 91 > nul
:stopold
REM Stop a running scheduler: run_scheduler.bat restart loops first (else they respawn it), then python scheduler.py.
echo [%date% %time%] stopping old scheduler if any >> "%LOG%"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*run_scheduler.bat*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*scheduler.py*' -and $_.ExecutablePath -like '*Stock_AI_Project*' } | ForEach-Object { Write-Output ('stop pid ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >> "%LOG%" 2>&1

echo [%date% %time%] Starting scheduler >> "%LOG%"
start "StockAI-Scheduler" /min cmd /c "venv\Scripts\python.exe -u scheduler.py >> C:\fin\logs\scheduler.log 2>&1"
echo [%date% %time%] done >> "%LOG%"
echo Scheduler (re)started. Log: C:\fin\logs\scheduler.log
endlocal
