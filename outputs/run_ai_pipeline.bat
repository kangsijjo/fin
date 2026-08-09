@echo off
REM run_ai_pipeline.bat - Weekly LIGHT AI refresh, gated end-to-end.
REM   1) preflight regression tests (ci_gate.py -> pytest tests)
REM   2) data collection (run_backfill.ps1: OHLCV+macro+supply+news)
REM   3) build training dataset (make_trades_history_v3.py)
REM   4) train meta-model with Optuna (ai_trainer_v4.py)
REM   5) post-train verification (ci_gate.py -> pytest tests)
REM Each step ABORTS the pipeline if it fails (no silent training on a broken state).
REM On abort, ci_gate.py sends a Telegram alert (test failure -> training stopped).
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
REM [2026-07-12] stale-lock handling added: a hard-killed run left the lock forever
REM and every Sunday run skipped with exit 0 and NO alert -> model silently stale.
REM [2026-08-09] ownership check added. The old version could not tell "another run
REM is genuinely working" from "dead leftover", so ANY fresh lock was treated as a
REM failure: exit 1 + a scary telegram, which also triggered the retry path and sent
REM the alert twice (observed 08-09 08:56 while the 07:43 run was still collecting).
REM Now the lock stores the owner PID:
REM   owner alive      -> normal skip, exit 0, no alert (the work IS being done)
REM   owner gone       -> reclaim the lock and proceed
REM   older than 6h    -> reclaim regardless (guards against PID reuse)
REM "training never ran" is detected by watchdog (last DONE age), not by this path.
set "LOCK=C:\fin\logs\ai_pipeline.lock"
set "PYEXE=%OUT%\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

REM own PID = parent of the powershell we spawn (this cmd instance)
set "MYPID="
for /f %%P in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID)).ParentProcessId"') do set "MYPID=%%P"

if exist "%LOCK%" (
    REM HOLD=1 -> a live owner holds it (skip) / HOLD=0 -> reclaimable
    set "HOLD="
    for /f "usebackq" %%A in (`powershell -NoProfile -Command "$f=$env:LOCK; $age=(New-TimeSpan -Start (Get-Item $f).LastWriteTime -End (Get-Date)).TotalHours; $first=(Get-Content $f -TotalCount 1 -ErrorAction SilentlyContinue); if ($age -gt 6) { 0 } elseif ($first -match '^\d+$') { if (Get-Process -Id ([int]$first) -ErrorAction SilentlyContinue) { 1 } else { 0 } } else { 1 }"`) do set "HOLD=%%A"
    if "!HOLD!"=="1" (
        echo [SKIP] An AI pipeline run is already in progress - nothing to do.
        echo        This is normal when a scheduled run overlaps a manual one.
        echo [%date% %time%] SKIP - another run is active ^(owner alive^) >> "%LOG%"
        if /i not "%1"=="auto" pause
        endlocal
        exit /b 0
    ) else (
        echo [WARN] reclaiming lock - previous owner is gone or lock is stale. >> "%LOG%"
        del "%LOCK%" 2>nul
    )
)
echo !MYPID!> "%LOCK%"
echo %DT% %time%>> "%LOCK%"

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
cd /d "%OUT%"

REM ---- 1/5 preflight regression gate (gated) ----
REM   Run pytest tests BEFORE training. If contracts are broken (feature dim,
REM   signal CSV headers, etc.) ABORT and do NOT train on a broken state.
set "STEP=preflight"
echo [%time%] [1/5] Preflight regression tests (pytest tests)...
echo [1/5] ci_gate.py gate (preflight pytest) ... >> "%LOG%"
"%PYEXE%" ci_gate.py gate >> "%LOG%" 2>&1
if errorlevel 1 ( echo [ABORT] preflight tests failed ^(exit !ERRORLEVEL!^) >> "%LOG%" & echo [X] preflight tests FAILED & goto :fail )
echo [%time%] [1/5] done.

REM ---- 2/5 data collection (gated) ----
set "STEP=collect"
echo [%time%] [2/5] Data collection running (OHLCV+macro+supply+news)... please wait
echo [2/5] data collection (run_backfill.ps1) ... >> "%LOG%"
powershell -ExecutionPolicy Bypass -File "%SAI%\run_backfill.ps1" >> "%LOG%" 2>&1
if errorlevel 1 ( echo [ABORT] data collection failed ^(exit !ERRORLEVEL!^) >> "%LOG%" & echo [X] data collection FAILED & goto :fail )
echo [%time%] [2/5] done.

REM ---- 3/5 build training dataset (gated) ----
set "STEP=dataset"
echo [%time%] [3/5] Building training dataset (make_trades_history_v3)...
echo [3/5] make_trades_history_v3.py ... >> "%LOG%"
cd /d "%OUT%"
"%PYEXE%" -u make_trades_history_v3.py >> "%LOG%" 2>&1
if errorlevel 1 ( echo [ABORT] dataset build failed ^(exit !ERRORLEVEL!^) >> "%LOG%" & echo [X] dataset build FAILED & goto :fail )
echo [%time%] [3/5] done.

REM ---- 4/5 train meta-model (Optuna) (gated) ----
set "STEP=train"
echo [%time%] [4/5] Training meta-model (Optuna 40 trials)... a few minutes
echo [4/5] ai_trainer_v4.py ... >> "%LOG%"
"%PYEXE%" -u ai_trainer_v4.py >> "%LOG%" 2>&1
if errorlevel 1 ( echo [ABORT] training failed ^(exit !ERRORLEVEL!^) >> "%LOG%" & echo [X] training FAILED & goto :fail )
echo [%time%] [4/5] done.

REM ---- 5/5 post-train verification (gated) ----
REM   Re-run pytest tests AFTER training to verify the freshly-written model
REM   passes the dimension/contract tests before live scoring uses it.
set "STEP=postcheck"
echo [%time%] [5/5] Post-train verification (pytest tests)...
echo [5/5] ci_gate.py gate (post-train pytest) ... >> "%LOG%"
"%PYEXE%" ci_gate.py gate >> "%LOG%" 2>&1
if errorlevel 1 ( echo [ABORT] post-train tests failed ^(exit !ERRORLEVEL!^) >> "%LOG%" & echo [X] post-train tests FAILED & goto :fail )
echo [%time%] [5/5] done.

echo [%date% %time%] AI pipeline DONE >> "%LOG%"
del "%LOCK%" 2>nul
Forfiles /P "C:\fin\logs" /M ai_pipeline_*.log /D -60 /C "cmd /c del @file" 2>nul
if /i not "%1"=="auto" ( echo. & type "%LOG%" & echo. & echo ===== DONE ===== & pause )
endlocal & exit /b 0

:fail
echo [%date% %time%] AI pipeline FAILED at step "!STEP!" - downstream steps skipped >> "%LOG%"
REM Telegram alert (Korean message built inside ci_gate.py; bat stays ASCII).
"%PYEXE%" ci_gate.py notify-fail "!STEP!" >> "%LOG%" 2>&1
del "%LOCK%" 2>nul
if /i not "%1"=="auto" ( echo. & type "%LOG%" & echo. & echo ===== FAILED ^(see log^) ===== & pause )
endlocal & exit /b 1
