@echo off
REM cleanup_legacy_tasks.bat - run as ADMINISTRATOR (right-click).
REM Disables legacy duplicate scheduled tasks (see cleanup_legacy_tasks.ps1).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup_legacy_tasks.ps1"
pause
