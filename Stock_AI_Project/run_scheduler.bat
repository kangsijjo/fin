@echo off
REM ============================================================
REM scheduler.py auto-restart loop.
REM If the process crashes, restarts after 30 seconds.
REM Combine with boot autostart (Startup folder or Task Scheduler)
REM for unattended operation.
REM Ctrl+C (twice) exits via code -1073741510.
REM ============================================================

REM Make cmd console and Python both speak UTF-8 (fixes Korean garbling)
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d C:\fin\Stock_AI_Project
call venv\Scripts\activate

set LOGDIR=C:\fin\Stock_AI_Project\logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

:loop
echo [%date% %time%] scheduler start >> "%LOGDIR%\runner.log"
python scheduler.py
set EXITCODE=%ERRORLEVEL%
echo [%date% %time%] scheduler exit (code=%EXITCODE%) >> "%LOGDIR%\runner.log"

REM Do not restart if terminated by Ctrl+C
if "%EXITCODE%"=="-1073741510" goto end
if "%EXITCODE%"=="3221225786" goto end

REM Otherwise, re