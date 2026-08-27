@echo off
REM ============================================================
REM  run_verify_kis.bat - KIS mock account health check (read-only).
REM  Never places an order. Safe to run any time.
REM
REM  Why a bat: the PowerShell in this environment is Windows PowerShell 5.1,
REM  which does NOT support the "&&" operator, so one-line bash-style commands
REM  fail with a parser error before anything runs. A bat removes that trap -
REM  just run this file, no shell syntax involved.
REM
REM  ASCII-only (project rule): Korean bytes in a bat break cmd parsing on a
REM  Korean Windows console. See the encoding section of the system manual.
REM ============================================================
setlocal EnableExtensions
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

"%PYEXE%" verify_kis_account.py
set "EC=%ERRORLEVEL%"

echo.
if "%EC%"=="0" (
    echo [RESULT] account readable. See the report above for any leftover cleanup.
) else (
    echo [RESULT] NOT healthy yet - fix the items listed above and run again.
)
if /i not "%1"=="auto" pause
endlocal & exit /b %EC%
