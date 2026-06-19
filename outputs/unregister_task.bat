@echo off
REM Remove legacy scheduled tasks (admin required)
REM NOTE: Current scheduler is managed by install_scheduler.ps1, not this file.

schtasks /delete /tn "KISDataCollector_Daily" /f
schtasks /delete /tn "KISDataCollector_Startup" /f

echo.
echo Tasks removed.
pause
