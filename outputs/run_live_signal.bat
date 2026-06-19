@echo off
:: run_live_signal.bat
:: 평일 18:30 실전 신호 감지 자동 실행 (작업 스케줄러 호출)
setlocal
set PYTHONIOENCODING=utf-8
if not exist C:\fin\logs mkdir C:\fin\logs

cd /d C:\fin\outputs

for /f "tokens=*" %%i in ('powershell -Command "Get-Date -Format yyyyMMdd_HHmm"') do set DT=%%i
set LOGFILE=C:\fin\logs\live_signal_%DT%.log

echo [%date% %time%] Live signal starting >> %LOGFILE%

if not exist .venv\Scripts\python.exe (
    echo [ERROR] .venv not found in C:\fin\outputs >> %LOGFILE%
    exit /b 1
)

.venv\Scripts\python.exe -u live_signal.py >> %LOGFILE% 2>&1
echo [%date% %time%] Live signal done. ExitCode=%errorlevel% >> %LOGFILE%
endlocal
