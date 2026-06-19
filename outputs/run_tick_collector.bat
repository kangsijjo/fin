@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Tick collector loop. Fix history:
REM  - bare "python" call could die instantly (MS Store alias) and
REM    restart every 5s forever (44k log lines). Now: venv python
REM    first, and stop after 5 consecutive instant-deaths.
REM ============================================================
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"
if not exist "logs" mkdir "logs"
echo [%date% %time%] tick collector scheduler start >> logs\tick_collector.log

if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
    goto :ready
)
where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    set "PYEXE=py -3.11"
    goto :ready
)
echo [%date% %time%] [ERROR] python not found. exit. >> logs\tick_collector.log
endlocal
exit /b 2

:ready
set "FAILCOUNT=0"

:loop
for /f "tokens=*" %%a in ('powershell -NoProfile -Command "Get-Date -Format 'HHmm'"') do set "hourMin=%%a"

if !hourMin! GEQ 1530 (
    echo [%date% %time%] market closed. collector stop. >> logs\tick_collector.log
    goto end
)

echo [%date% %time%] tick collector run/restart >> logs\tick_collector.log

for /f %%s in ('powershell -NoProfile -Command "[long](Get-Date -UFormat %%s)"') do set "T0=%%s"
!PYEXE! tick_collector.py >> logs\tick_collector.log 2>&1
for /f %%s in ('powershell -NoProfile -Command "[long](Get-Date -UFormat %%s)"') do set "T1=%%s"

set /a RUNTIME=!T1!-!T0!
if !RUNTIME! LSS 10 (
    set /a FAILCOUNT+=1
    echo [%date% %time%] [warn] instant exit detected (!RUNTIME!s, consecutive !FAILCOUNT!) >> logs\tick_collector.log
    if !FAILCOUNT! GEQ 5 (
        echo [%date% %time%] [ERROR] 5 consecutive instant exits - env problem suspected. stop. >> logs\tick_collector.log
        goto end
    )
    timeout /t 30 /nobreak >nul
) else (
    set "FAILCOUNT=0"
    timeout /t 5 /nobreak >nul
)
goto loop

:end
endlocal
exit /b 0
