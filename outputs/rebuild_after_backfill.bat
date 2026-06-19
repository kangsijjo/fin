@echo off
setlocal
cd /d C:\fin\outputs
set PYTHONIOENCODING=utf-8

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 set "PYEXE=py -3.11"
)
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] Python not found. Install Python or activate venv.
    pause
    exit /b 2
)

echo [Python] %PYEXE%
echo.

echo [1/3] Rebuilding trades_history_v3.csv ...
%PYEXE% make_trades_history_v3.py
if errorlevel 1 (
    echo [ERROR] make_trades_history_v3.py failed
    pause
    exit /b 1
)

echo.
echo [2/3] Retraining AI model v4 ...
%PYEXE% ai_trainer_v4.py
if errorlevel 1 (
    echo [ERROR] ai_trainer_v4.py failed
    pause
    exit /b 1
)

echo.
echo [3/3] Done. Feature coverage check:
%PYEXE% -c "import pandas as pd; df = pd.read_csv('trades_history_v3.csv'); cols = ['news_sent_7d','crd_remn_rt','for_net5_db','ins_net5_db','rsi_db','macd_hist_db','bb_pct_db','prm_net_5d_ratio']; [print(f'  {c}: {df[c].notna().mean():.1%%}') for c in cols if c in df.columns]"

echo.
echo Rebuild complete.
endlocal
pause
