@echo off
REM run_ai_pipeline.bat - Weekly LIGHT AI refresh, gated end-to-end.
REM   1) data collection (run_backfill.ps1: OHLCV+macro+supply+news)
REM   2) build training dataset (make_trades_history_v3.py)
REM   3) train meta-model with Optuna (ai_trainer_v4.py)
REM Each step ABORTS the pipeline if the previous one fails (no silent training on stale data).
REM Manual double-click: shows the log and pauses. Scheduler must pass "auto" to skip pause.
REM Heavy full backtest is NOT here - see Stock_AI_Project\run_full_pipeline.ps1 (monthly).
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONIOENCODING=utf-8
set "OUT=C:\fin\outputs"
set "SAI=C:\fin\Stock_AI_Project"

if not exist "C:\fin\logs" mkdir "C:\fin\logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "DT=%%I"
set "LOG=C:\fin\logs\ai_pipeline_%DT%.log"
echo [%date% %time%] AI pipeline start >> "%LOG%"

REM ---- single-instance guard: prevent concurrent collectors on the same DB/API ----
set "LOCK=C:\fin\logs\ai_pipeline.lock"
if exist "%LOCK%" (
    echo [SKIP] An AI pipeline run is already in progress.
    echo        If you are SURE none is running, delete this file and retry:
    echo        %LOCK%
    echo [%date% %time%] SKIP - lock exists >> "%LOG%"
    if /i not "%1"=="auto" pause
    endlocal
    exit /b 0
)
echo %DT% %time% > "%LOCK%"

echo ============================================================
echo  AI PIPELINE - detailed output goes to the LOG file below.
echo  (the console only shows step markers; a blank console is NORMAL)
echo  LOG: %LOG%
echo  Step 1 collects 3000+ tickers - expect several minutes. Please wait.
echo  Live watch:  powershell -Command "Get-Content '%LOG%' -Wait -Tail 20"
echo ============================================================
echo.

REM python (outputs venv) detection
set "PYEXE=%OUT%\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

REM ---- 1/3 data collection (gated) ----
echo [%time%] [1/3] Data collection running (OHLCV+macro+supply+news)... please wait
echo [1/3] data collection (run_backfill.ps1) ... >> "%LOG%"
powershell -ExecutionPolicy Bypass -File "%SAI%\run_backfill.ps1" >> "%LOG%" 2>&1
if errorlevel 1 ( echo [ABORT] data collection failed ^(exit !ERRORLEVEL!^) >> "%LOG%" & echo [X] data collection FAILED & goto :fail )
echo [%time%] [1/3] done.

REM ---- 2/3 build training dataset (gated) ----
echo [%time%] [2/3] Building training dataset (make_trades_history_v3)...
echo [2/3] make_trades_history_v3.py ... >> "%LOG%"
cd /d "%OUT%"
"%PYEXE%" -u make_trades_history_v3.py >> "%LOG%" 2>&1
if errorlevel 1 ( echo [ABORT] dataset build failed ^(exit !ERRORLEVEL!^) >> "%LOG%" & echo [X] dataset build FAILED & goto :fail )
echo [%time%] [2/3] done.

REM ---- 3/3 train meta-model (Optuna) (gated) ----
echo [%time%] [3/3] Training meta-model (Optuna 40 trials)... a few minutes
echo [3/3] ai_trainer_v4.py ... >> "%LOG%"
"%PYEXE%" -u ai_trainer_v4.py >> "%LOG%" 2>&1
if errorlevel 1 ( echo [ABORT] training failed ^(exit !ERRORLEVEL!^) >> "%LOG%" & echo [X] training FAILED & goto :fail )
echo [%time%] [3/3] done.

echo [%date% %time%] AI pipeline DONE >> "%LOG%"
del "%LOCK%" 2>nul
Forfiles /P "C:\fin\logs" /M ai_pipeline_*.log /D -60 /C "cmd /c del @file" 2>nul
if /i not "%1"=="auto" ( echo. & type "%LOG%" & echo. & echo ===== DONE ===== & pause )
endlocal & exit /b 0

:fail
echo [%date% %time%] AI pipeline FAILED - downstream steps skipped >> "%LOG%"
del "%LOCK%" 2>nul
if /i not "%1"=="auto" ( echo. & type "%LOG%" & echo. & echo ===== FAILED ^(see log^) ===== & pause )
endlocal & exit /b 1
