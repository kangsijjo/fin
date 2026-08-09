@echo off
setlocal
REM [2026-07-21] Force UTF-8 for manual starts too; otherwise the server console is CP949
REM and a Korean/emoji print can kill the process with UnicodeEncodeError.
set PYTHONIOENCODING=utf-8

REM Prefer the outputs venv, then Stock_AI_Project venv, then system python
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
