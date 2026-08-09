@echo off
REM ============================================================
REM  Register the auto-collection task in Windows Task Scheduler
REM
REM  *** Run this from an ADMINISTRATOR cmd prompt ***
REM     (right-click this file -> Run as administrator)
REM
REM  Tasks registered:
REM   1) weekdays (Mon-Fri) at 15:35
REM   2) 3 minutes after system startup
REM ============================================================

setlocal

set TASK_DAILY=KISDataCollector_Daily
set TASK_STARTUP=KISDataCollector_Startup
set BATPATH=%~dp0run_collector.bat

REM verify the batch file exists
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

REM ---- weekday 15:35 trigger ----
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

REM ---- at-startup trigger (3 min delay) ----
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
