# cleanup_legacy_tasks.ps1
# Disable legacy root-level scheduled tasks that DUPLICATE the \StockAI\ tasks.
# Found 2026-07-07 audit: both generations were active at the same time, causing
#   - two scheduler.py daemons (KIS rate-limit EGW00201, stock.db write races)
#   - kiwoom trader running twice every morning (09:01 legacy + 09:03 StockAI)
#     and TWO processes at the same minute 15:21 (double-sell race)
#   - KIS_Data_Collector: stale duplicate of KIS_EOD, failing every day
# Tasks are DISABLED (not deleted) so this is reversible with Enable-ScheduledTask.
#
# Run: right-click cleanup_legacy_tasks.bat -> Run as administrator

$ErrorActionPreference = 'Stop'

try {
    if (-not (New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "[ERROR] Not Administrator. Right-click cleanup_legacy_tasks.bat -> Run as administrator." -ForegroundColor Red
        exit 1
    }

    # Tasks to disable and why (kept: KIS_Paper (macro_data collector, bat fixed),
    # KIS_Tick_Collector (bat got weekend guard), KIS_EOD/Ranking/Backtest/KRX/DART/
    # Monthly/Recheck (own collectors, no StockAI duplicate)).
    $targets = @(
        @{ Name = 'KIS_KiwoomBuy';      Why = 'duplicate of StockAI\KiwoomTrader 09:03' },
        @{ Name = 'KIS_KiwoomSell';     Why = 'same-minute duplicate of StockAI\KiwoomTrader 15:21' },
        @{ Name = 'KIS_Data_Collector'; Why = 'stale duplicate of KIS_EOD (same bat, failing daily)' },
        @{ Name = 'Stock_AI_Scheduler'; Why = 'duplicate scheduler daemon (StockAI\Scheduler owns it now)' }
    )

    foreach ($t in $targets) {
        $task = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Host "[SKIP] $($t.Name) not found" -ForegroundColor Yellow
            continue
        }
        try { Stop-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue } catch {}
        Disable-ScheduledTask -TaskName $t.Name | Out-Null
        Write-Host "[OK] disabled $($t.Name)  ($($t.Why))" -ForegroundColor Green
    }

    # The legacy daemon may leave an orphan python (market_scanner child). Show survivors
    # so the user can decide; a reboot also cleans them up.
    Write-Host ""
    Write-Host "=== Remaining python processes (check for orphan market_scanner) ===" -ForegroundColor Cyan
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Select-Object ProcessId, CreationDate, CommandLine | Format-List

    Write-Host "=== Legacy task states after cleanup ===" -ForegroundColor Cyan
    Get-ScheduledTask -TaskName 'KIS_*','Stock_AI_Scheduler' -ErrorAction SilentlyContinue |
        Format-Table TaskName, State -AutoSize
    Write-Host "Done. StockAI\* tasks are now the single owner of trading/scheduler." -ForegroundColor Cyan

} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
