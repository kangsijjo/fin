@echo off
REM ============================================================
REM  KIS scheduled tasks installer (runs install_scheduler.ps1)
REM ============================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_scheduler.ps1"
echo.
pause