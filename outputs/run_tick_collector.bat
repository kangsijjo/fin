@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ============================================================
REM  Tick collector loop. Fix history:
REM  - bare "python" call could die instantly (MS Store alias) and
REM    restart every 5s forever (44k log lines). Now: venv python
REM    first, and stop after 5 consecutive instant-deaths.
REM  - [2026-07-07] weekend guard added: task fires daily 08:59 and
REM    on Sunday 07-05 the loop retried a refused connection all day
REM    (696 tracebacks). Also raised fail threshold 10s -> 60s:
REM    connect timeouts take 20-40s so they reset the counter and
REM    the 5-strike stop never triggered.
REM ============================================================
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"
if not exist "logs" mkdir "logs"

REM -- weekend skip (task trigger is daily incl. Sat/Sun) --
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set "DOW=%%I"
if "!DOW!"=="0" (
    echo [%date% %time%] [SKIP] Sunday - no market. >> logs\tick_collector.log
    endlocal
    exit /b 0
)
if "!DOW!"=="6" (
    echo [%date% %time%] [SKIP] Saturday - no market. >> logs\tick_collector.log
    endlocal
    exit /b 0
)

REM -- [2026-08-20] log rotation. This was the ONLY daily log with no cleanup:
REM    a single tick_collector.log had grown to 229 MB ("[DB] 1 tick stored" per
REM    line, every trading day since June, never rotated). Every other runner
REM    uses dated files + Forfiles. Roll over past 20 MB, keep 5 generations.
if exist "logs\tick_collector.log" (
    for /f %%S in ('powershell -NoProfile -Command "(Get-Item 'logs\tick_collector.log').Length"') do set "LOGSZ=%%S"
    if !LOGSZ! GTR 20971520 (
        for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "RSTAMP=%%I"
        move /y "logs\tick_collector.log" "logs\tick_collector_!RSTAMP!.log" >nul 2>&1
        echo [%date% %time%] [rotate] previous log archived as tick_collector_!RSTAMP!.log >> logs\tick_collector.log
    )
)
Forfiles /P "logs" /M tick_collector_*.log /D -14 /C "cmd /c del @file" 2>nul

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
if !RUNTIME! LSS 60 (
    set /a FAILCOUNT+=1
    echo [%date% %time%] [warn] short-lived exit detected (!RUNTIME!s, consecutive !FAILCOUNT!) >> logs\tick_collector.log
    if !FAILCOUNT! GEQ 5 (
        echo [%date% %time%] [ERROR] 5 consecutive short-lived exits - env/network problem suspected. stop. >> logs\tick_collector.log
        goto end
    )
    timeout /t 60 /nobreak >nul
) else (
    set "FAILCOUNT=0"
    timeout /t 5 /nobreak >nul
)
goto loop

:end
endlocal
exit /b 0
