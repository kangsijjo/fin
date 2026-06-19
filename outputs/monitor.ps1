# KIS Data Collection Monitor - auto-refresh every 30s
# This window only DISPLAYS status. Closing it does NOT stop collection
# (the Task Scheduler keeps running data collection in the background).
$ErrorActionPreference = "SilentlyContinue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($dir) { Set-Location $dir }

function Show-Status {
    Clear-Host
    $now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  KIS Data Collection Monitor      $now" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""

    Write-Host "[Task Scheduler]" -ForegroundColor Yellow
    foreach ($t in @("KIS_Ranking","KIS_EOD","KIS_Monthly")) {
        $info = Get-ScheduledTask -TaskName $t | Get-ScheduledTaskInfo
        if ($info) {
            $last = if ($info.LastRunTime -and $info.LastRunTime.Year -gt 1999) { $info.LastRunTime.ToString("MM-dd HH:mm") } else { "never" }
            $next = if ($info.NextRunTime) { $info.NextRunTime.ToString("MM-dd HH:mm") } else { "-" }
            $rc = $info.LastTaskResult
            if ($rc -eq 0) { $rcText = "OK" }
            elseif ($rc -eq 267011) { $rcText = "not-run-yet" }
            elseif ($rc -eq 267009) { $rcText = "running" }
            else { $rcText = "code:$rc" }
            Write-Host ("  {0,-13} last {1}   result {2}   next {3}" -f $t, $last, $rcText, $next)
        } else {
            Write-Host ("  {0,-13} NOT REGISTERED - run install_scheduler.bat" -f $t) -ForegroundColor Red
        }
    }
    Write-Host ""

    $today = Get-Date -Format "yyyyMMdd"
    $ym = Get-Date -Format "yyyy-MM"
    Write-Host "[Today's data  $today]" -ForegroundColor Yellow
    $rankFile = "data\rankings\$today.csv"
    if (Test-Path $rankFile) {
        $rows = @(Import-Csv $rankFile)
        $snaps = @($rows | Select-Object -ExpandProperty snapshot_time -Unique)
        $codes = @($rows | Select-Object -ExpandProperty code -Unique)
        $lastSnap = ($snaps | Sort-Object | Select-Object -Last 1)
        Write-Host ("  ranking   : {0} snapshot-times (latest {1}), {2} unique codes" -f $snaps.Count, $lastSnap, $codes.Count) -ForegroundColor Green
    } else {
        Write-Host "  ranking   : none yet (created after 09:00 market open)"
    }
    $invFile = "db\investor\$ym\$today.csv"
    $dayFile = "db\daily\$ym\$today.csv"
    if (Test-Path $invFile) { Write-Host "  investor  : collected" -ForegroundColor Green } else { Write-Host "  investor  : pending (after 15:40 close)" }
    if (Test-Path $dayFile) { Write-Host "  daily     : collected" -ForegroundColor Green } else { Write-Host "  daily     : pending (after 15:40 close)" }
    Write-Host ""

    $logFile = "logs\collect_$today.log"
    Write-Host "[Today's log - last 8 lines]" -ForegroundColor Yellow
    if (Test-Path $logFile) {
        Get-Content $logFile -Tail 8 | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Host "  (no log file - not run yet today)"
    }
    Write-Host ""
    Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "Closing this window does NOT stop collection (Task Scheduler runs it)." -ForegroundColor DarkGray
    Write-Host "Auto-refresh every 30s.  To quit, just close this window." -ForegroundColor DarkGray
}

while ($true) {
    Show-Status
    Start-Sleep -Seconds 30
}