@echo off
REM ============================================================
REM Full pipeline runner. Use for first-time setup or full rebuild.
REM Total runtime: about 6 to 12 hours (includes 9-yr walk-forward).
REM
REM Normal operation uses scheduler.py - this script is NOT needed
REM for daily runs.
REM
REM Detailed logs: logs\YYYY-MM-DD.log
REM ============================================================

REM Make cmd console and Python both speak UTF-8 (fixes Korean garbling)
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d C:\fin\Stock_AI_Project
call venv\Scripts\activate

echo.
echo ===================================================
echo  Step 0: Data hygiene (one-shot, fast)
echo ===================================================
python fix_date_format.py
python fix_dup_stocks.py
python fix_dup_indicators.py

echo.
echo ===================================================
echo  Step 1: Data collection (30min ~ several hours)
echo  - Stocks / macro / supply-demand
echo ===================================================
python -m src.collector.main_collector
python -m src.collector.macro
python -m src.collector.supply_demand

echo.
echo ===================================================
echo  Step 2: Indicators (30min ~ 1hr)
echo ===================================================
python -m src.processor.indicators all

echo.
echo ===================================================
echo  Step 3: Model training (30min ~ 1hr)
echo ===================================================
python -m src.models.train all

echo.
echo ===================================================
echo  Step 4: Backtest (6~9hr, BACKTEST_REBUILD=1)
echo ===================================================
set BACKTEST_REBUILD=1
python -m src.trader.backtest all
set BACKTEST_REBUILD=

echo.
echo ===================================================
echo  Step 5: Diagnostics (1min)
echo  Note: pass a sector name from PowerShell if needed
echo  e.g. python diag_backtest.py <sector_name>
echo ===================================================
python diag_backtest.py
python diag_top1.py

echo.
echo ===================================================
echo  Pipeline complete
echo  - Logs: logs\