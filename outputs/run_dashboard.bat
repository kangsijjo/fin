@echo off
setlocal
REM ============================================================
REM  Dashboard start / restart (the ONLY dashboard launcher).
REM  - Task Scheduler (StockAI\Dashboard) runs this at logon.
REM  - Double-click any time = clean restart (old :5050 server is stopped first).
REM  - 60s wait only right after boot (uptime under 5 min); manual runs start at once.
REM  [2026-10-23] start_dashboard.bat merged into this file and archived.
REM ============================================================
set PYTHONIOENCODING=utf-8
cd /d C:\fin\outputs
if not exist logs mkdir logs
REM Bat markers go to a SEPARATE file: dashboard.log is held open by the running
REM server process, so appending to dashboard.log from a re-run silently fails.
set "LOG=C:\fin\outputs\logs\dashboard_boot.log"
echo [%date% %time%] run_dashboard START >> "%LOG%"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
echo [%date% %time%] PY=%PY% >> "%LOG%"
powershell -NoProfile -Command "if (((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalMinutes -lt 5) { exit 1 } else { exit 0 }"
if errorlevel 1 goto bootwait
echo [%date% %time%] manual run - no wait >> "%LOG%"
goto cleanup
:bootwait
echo [%date% %time%] just booted - 60s wait... >> "%LOG%"
echo Just booted - waiting 60s before starting the dashboard...
ping 127.0.0.1 -n 61 > nul
:cleanup
REM Kill only the process listening on :5050 (previous dashboard instance).
REM Makes re-run idempotent: re-launch = clean restart. Other python jobs untouched.
echo [%date% %time%] cleanup old listener on :5050 >> "%LOG%"
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }" >> "%LOG%" 2>&1
echo [%date% %time%] Starting dashboard http://localhost:5050 >> "%LOG%"
start "StockAI-Dashboard" /min cmd /c "%PY% -u integrated_dashboard_server.py >> C:\fin\outputs\logs\dashboard.log 2>&1"
echo [%date% %time%] done >> "%LOG%"
echo Dashboard (re)started: http://localhost:5050  - the browser opens by itself in a few seconds.
echo Server log: C:\fin\outputs\logs\dashboard.log
endlocal
