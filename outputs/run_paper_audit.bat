@echo off
:: run_paper_audit.bat
:: 일요일 09:00 주간 수익 리포트 자동 실행 (작업 스케줄러 호출)
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
echo [%date% %time%] Paper audit done. ExitCode=%errorlevel% >> %LOGFILE%
endlocal
