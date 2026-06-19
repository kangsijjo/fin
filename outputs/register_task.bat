@echo off
REM ============================================================
REM  Windows 작업 스케줄러에 자동 수집 작업 등록
REM
REM  ★ 관리자 권한 cmd에서 실행하세요 ★
REM     (이 파일 우클릭 → "관리자 권한으로 실행")
REM
REM  등록되는 작업:
REM   1) 평일(월~금) 15:35에 자동 실행
REM   2) 시스템 시작 후 3분 뒤 자동 실행
REM ============================================================

setlocal

set TASK_DAILY=KISDataCollector_Daily
set TASK_STARTUP=KISDataCollector_Startup
set BATPATH=%~dp0run_collector.bat

REM 배치 파일 존재 확인
if not exist "%BATPATH%" (
    echo [ERROR] run_collector.bat not found: %BATPATH%
    pause
    exit /b 1
)

echo ============================================================
echo  Registering scheduled tasks
echo  Batch file: %BATPATH%
echo ============================================================
echo.

REM ---- 평일 15:35 트리거 ----
echo [1/2] Registering weekday 15:35 task...
schtasks /create /f ^
  /tn "%TASK_DAILY%" ^
  /tr "\"%BATPATH%\"" ^
  /sc weekly /d MON,TUE,WED,THU,FRI ^
  /st 15:35 ^
  /rl HIGHEST

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to register weekday task. Run as administrator.
    pause
    exit /b 1
)

REM ---- 시스템 시작 시 트리거 (3분 지연) ----
echo.
echo [2/2] Registering on-startup task...
schtasks /create /f ^
  /tn "%TASK_STARTUP%" ^
  /tr "\"%BATPATH%\"" ^
  /sc onstart ^
  /delay 0000:03 ^
  /rl HIGHEST

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to register on-startup task.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Done!
echo ============================================================
echo.
echo  Registered tasks:
echo   - %TASK_DAILY%   (weekday 15:35)
echo   - %TASK_STARTUP% (3 min after system start)
echo.
echo  Verify: taskschd.msc
echo  Manual test: schtasks /run /tn "%TASK_DAILY%"
echo  To remove: run unregister_task.bat
echo.
pause
