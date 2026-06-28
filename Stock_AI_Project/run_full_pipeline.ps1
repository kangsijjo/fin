# Full pipeline (PowerShell version).
# Usage: .\run_full_pipeline.ps1
# If blocked, run: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

$ErrorActionPreference = "Continue"

# Force UTF-8 console so Python's Korean print() output is not garbled.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

Set-Location "C:\fin\Stock_AI_Project"
. .\venv\Scripts\Activate.ps1

function Step($title, $block, [switch]$Critical) {
    Write-Host ""
    Write-Host "=============================================="
    Write-Host " $title"
    Write-Host "=============================================="
    & $block
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARN] '$title' exited with code $LASTEXITCODE" -ForegroundColor Yellow
        if ($Critical) {
            Write-Host "[ABORT] critical step failed - stopping (no training/backtest on bad data)." -ForegroundColor Red
            exit 1
        }
    }
}

Step "Step 0: Data hygiene" {
    python fix_date_format.py
    python fix_dup_stocks.py
    python fix_dup_indicators.py
}

Step "Step 1: Data collection (stocks / macro / supply-demand)" {
    python -m src.collector.main_collector; if ($LASTEXITCODE -ne 0) { return }
    python -m src.collector.macro;          if ($LASTEXITCODE -ne 0) { return }
    python -m src.collector.supply_demand
} -Critical

Step "Step 2: Indicators (all sectors)" {
    python -m src.processor.indicators all
}

Step "Step 3: Model training (all sectors)" {
    python -m src.models.train all
}

Step "Step 4: Backtest (BACKTEST_REBUILD=1, 6-9 hours)" {
    $env:BACKTEST_REBUILD = "1"
    python -m src.trader.backtest all
    Remove-Item Env:BACKTEST_REBUILD -ErrorAction SilentlyContinue
}

Step "Step 5: Diagnostics" {
    # PowerShell handles UTF-8 args correctly with the lines above
    python diag_backtest.py 반도체
    python diag_top1.py 반도체
}

Write-Host ""
Write-Host "Pipeline complete. Next: .\run_scheduler.bat for unattended ops"
Write-Host "Logs: logs\YYYY-MM-DD.log"
