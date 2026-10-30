@echo off
REM run_setting_sweep.bat - weekly strength x stop x take FINE setting search (2026-10-30, system doc 88).
REM   Saturday 02:00 (market closed, done before the 07:00 weekend backfill): coarse grid -> 3 best regions refined (strength 0.1 / stop 1pt / take 2pt),
REM   picked by neighbourhood average + yearly walk-forward check -> db\setting_sweep.json
REM   (settings screens "target" advice + strategy recommendation evidence). Telegram summary at the end.
REM   Manual: double-click (shows the log and pauses) or dashboard Data+Logs [setting search].
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "OUT=C:\fin\outputs"
if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOG=C:\fin\logs\setting_sweep_%DT%.log"
set "PYEXE=%OUT%\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
cd /d "%OUT%"
echo [%date% %time%] setting sweep start >> "%LOG%"
"%PYEXE%" -u setting_sweep.py --fine >> "%LOG%" 2>&1
set "RC=!ERRORLEVEL!"
echo [%date% %time%] setting sweep end rc=!RC! >> "%LOG%"
Forfiles /P "C:\fin\logs" /M setting_sweep_*.log /D -60 /C "cmd /c del @file" 2>nul
if /i not "%1"=="auto" ( echo. & type "%LOG%" & echo. & pause )
endlocal & exit /b %RC%
