@echo off
setlocal

REM outputs 전용 venv 우선, 없으면 Stock_AI_Project venv, 없으면 시스템 python
set PY=C:\fin\outputs\.venv\Scripts\python.exe
if not exist "%PY%" set PY=C:\fin\Stock_AI_Project\.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [Dashboard] Checking Flask...
"%PY%" -c "import flask" 2>nul
if errorlevel 1 (
    echo [Dashboard] Installing Flask...
    "%PY%" -m pip install flask --quiet
)

cd /d C:\fin\outputs
echo [Dashboard] Starting at http://localhost:5050
"%PY%" integrated_dashboard_server.py %*
echo.
echo [Dashboard] Server stopped.
pause
endlocal
