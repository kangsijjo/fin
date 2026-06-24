@echo off
REM run_kis_status.bat
REM Weekday 15:40 KIS mock balance snapshot refresh (after market close).
REM kis_trader.py status -> real API balance query -> db/kiwoom/kis_snapshot.json refresh.
REM Manual double-click: shows result and keeps the window open (pause).
REM Scheduler must pass argument "auto" to skip the pause (see register_tasks.ps1).
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM weekend skip
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" goto :skipday
if "!DOW!"=="6" goto :skipday

REM log setup
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOGFILE=C:\fin\logs\kis_status_%DT%.log"
echo [%date% %time%] KIS status (balance refresh) starting >> "%LOGFILE%"

REM python detection
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
    goto :run
)
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    set "PYEXE=py"
    goto :run
)
echo [ERROR] python not found >> "%LOGFILE%"
echo [ERROR] python not found
if /i not "%1"=="auto" pause
endlocal & exit /b 1

:run
!PYEXE! -u kis_trader.py status >> "%LOGFILE%" 2>&1
set "EC=!ERRORLEVEL!"
echo [%date% %time%] KIS status done. ExitCode=!EC! >> "%LOGFILE%"
Forfiles /P "C:\fin\logs" /M kis_status_*.log /D -30 /C "cmd /c del @file" 2>nul

REM manual run: show the result and keep window open
if /i not "%1"=="auto" (
    echo.
    echo ============== KIS status result ==============
    type "%LOGFILE%"
    echo ===============================================
    echo ExitCode=!EC!
    pause
)
endlocal & exit /b %EC%

:skipday
echo [SKIP] Weekend
if /i not "%1"=="auto" pause
endlocal & exit /b 0
