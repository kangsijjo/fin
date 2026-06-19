@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  단타 박스권 매매 - 데이터 자동 수집 배치 (최적화 버전)
REM ============================================================

cd /d "%~dp0"
set "EXITCODE=0"

REM --- 요일과 날짜를 PowerShell 두 번 호출로 가져오기 (escape 안전) ---
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd')"') do set "TODAY=%%I"

REM --- 주말(토=6, 일=0) 차단 ---
if "!DOW!"=="0" goto :weekend
if "!DOW!"=="6" goto :weekend
goto :weekday

:weekend
echo [SKIP] Weekend (DOW=!DOW!). Market closed.
if /i not "%1"=="auto" pause
endlocal
exit /b 0

:weekday
set "LOGDIR=logs"
if not exist "!LOGDIR!" mkdir "!LOGDIR!"
set "LOGFILE=!LOGDIR!\collect_!TODAY!.log"

REM --- 30일이 지난 오래된 로그 파일 자동 삭제 (유지보수 프리) ---
Forfiles /P "!LOGDIR!" /M *.log /D -30 /C "cmd /c del @file" 2>nul

echo. >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"
echo  Start: %DATE% %TIME% >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"

REM Check .env exists
if not exist ".env" (
    echo [ERROR] .env file not found in %CD%
    echo [ERROR] .env file not found in %CD% >> "!LOGFILE!"
    set "EXITCODE=1"
    goto :end
)

REM Prefer venv python (where project deps are installed)
if exist ".venv\Scripts\python.exe" (
    echo [INFO] running with: .venv\Scripts\python.exe
    ".venv\Scripts\python.exe" data_collector.py today >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)

where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    echo [INFO] running with: py -3.11
    py -3.11 data_collector.py today >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)

where python >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    echo [INFO] running with: python
    python data_collector.py today >> "!LOGFILE!" 2>&1
    set "EXITCODE=!ERRORLEVEL!"
    goto :report
)

echo [ERROR] neither py nor python found on PATH
echo [ERROR] neither py nor python found on PATH >> "!LOGFILE!"
set "EXITCODE=2"
goto :end

:report
echo. >> "!LOGFILE!"
echo  Exit code: !EXITCODE! (time: %TIME%) >> "!LOGFILE!"
echo ============================================================ >> "!LOGFILE!"
echo.
echo Exit code: !EXITCODE!
echo Log file: !LOGFILE!

:end
if /i not "%1"=="auto" (
    echo.
    pause
)

set "RC=!EXITCODE!"
endlocal & exit /b %RC%