@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  collect_all.bat - manual "collect every missing data" key (2026-10-18)
REM  Double-click. Same job as the dashboard button [collect all missing]:
REM    price/foreign/inst (missing days) -> individual net buying (2018-06~)
REM    -> KOSDAQ short-selling share (2018-06~) -> credit balance (2021-01~, Kiwoom)
REM  Only empty days are fetched. Credit is skipped during 08:30~16:00 on trading
REM  days (shares the Kiwoom key with trading). When finished, a Telegram report
REM  lists which items are complete and which trading days are still missing.
REM  Progress: dashboard backtest tab [data status] or logs\collect_*.log
REM ============================================================
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE (
    where py >nul 2>nul
    if !ERRORLEVEL! EQU 0 set "PYEXE=py -3.11"
)
if not defined PYEXE (
    echo [ERROR] python not found
    if /i not "%1"=="auto" pause
    exit /b 2
)

echo [%date% %time%] collect all missing data - started. This can take hours (credit).
echo Closing this window stops the collection.
!PYEXE! -u collect_aux.py --kind all
set "RC=!ERRORLEVEL!"
if "!RC!"=="9" echo [SKIP] another collection is already running.
echo [%date% %time%] finished, exit=!RC! - see the Telegram report.
if /i not "%1"=="auto" pause
endlocal & exit /b %RC%
