@echo off
:: run_paper_audit.bat
:: Sunday 09:00 weekly P&L report (called by Task Scheduler)
setlocal
set PYTHONIOENCODING=utf-8
if not exist C:\fin\logs mkdir C:\fin\logs

cd /d C:\fin\outputs

for /f "tokens=*" %%i in ('powershell -Command "Get-Date -Format yyyyMMdd"') do set DT=%%i
set LOGFILE=C:\fin\logs\paper_audit_%DT%.log

echo [%date% %time%] Paper audit starting >> %LOGFILE%

if not exist .venv\Scripts\python.exe (
    echo [ERROR] .venv not found in C:\fin\outputs >> %LOGFILE%
    exit /b 1
)

.venv\Scripts\python.exe -u paper_audit.py >> %LOGFILE% 2>&1
set "EC=%errorlevel%"
echo [%date% %time%] Paper audit done. ExitCode=%EC% >> %LOGFILE%
:: [2026-07-12] propagate exit code (was: echo -> always 0 in scheduler history)
endlocal & exit /b %EC%
