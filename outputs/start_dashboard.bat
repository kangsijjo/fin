@echo off
setlocal
REM [2026-07-21] 수동 기동 시에도 UTF-8 강제 — 없으면 서버 콘솔 출력이 cp949 로 나가
REM 한글/이모지 print 에서 UnicodeEncodeError 로 죽을 수 있다(타 실행기와 동일 규약).
set PYTHONIOENCODING=utf-8

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
