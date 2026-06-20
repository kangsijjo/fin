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

:: 전략 랩 (forward 누적) — live_signal 직후 같은 데이터로 29전략 백테스트→forward 집계.
:: 결정론적이라 매일 재실행하면 LAB_START 이후 완료 트레이드가 자동 누적됨.
echo [%date% %time%] strategy_lab starting >> %LOGFILE%
.venv\Scripts\python.exe -u strategy_lab.py >> %LOGFILE% 2>&1
echo [%date% %time%] strategy_lab done. ExitCode=%errorlevel% >> %LOGFILE%

:: 일일 자동화 자가점검 — 오늘 자동화가 제대로 돌았는지 한 장 요약을 logs\daily_audit_날짜.log 에 남김.
.venv\Scripts\python.exe -u daily_audit.py >> %LOGFILE% 2>&1

:: 일일 요약 텔레그램 푸시 (신호/보유/예수금 한 장).
.venv\Scripts\python.exe -u daily_summary.py >> %LOGFILE% 2>&1
endlocal
