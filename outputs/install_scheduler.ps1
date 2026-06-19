# Registers 6 scheduled tasks for KIS data collection + backtest + KRX + DART.
# KIS_Ranking / KIS_EOD / KIS_Backtest / KIS_KRX / KIS_DART : PowerShell Register-ScheduledTask
# KIS_Monthly                                                : schtasks (PowerShell has no native monthly trigger)
$ErrorActionPreference = "Stop"

# --- Self-elevation guard: relaunch as administrator if not elevated ---
$pr = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Not elevated. Relaunching as administrator (UAC prompt will appear)..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList @(
        "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`""
    )
    exit
}

$dir = $PSScriptRoot
if (-not $dir) { $dir = (Get-Location).Path }
$bat         = Join-Path $dir "run_collector.bat"
$batBacktest = Join-Path $dir "run_backtest.bat"
$batKrx      = Join-Path $dir "run_krx.bat"
$batDart     = Join-Path $dir "run_dart.bat"
$batPaper    = Join-Path $dir "run_paper.bat"
$batRecheck  = Join-Path $dir "run_recheck.bat"
$batKiwoom   = Join-Path $dir "run_kiwoom.bat"
$pyMonthly   = Join-Path $dir "monthly_xlsx_builder.py"

Write-Host "Installing KIS scheduled tasks from: $dir"
Write-Host ""

foreach ($f in @($bat, $batBacktest, $batKrx, $batDart, $batPaper, $batRecheck, $batKiwoom)) {
    if (-not (Test-Path $f)) {
        Write-Host "[ERROR] $f not found" -ForegroundColor Red
        exit 1
    }
}

# Remove existing tasks (clean install)
foreach ($t in @("KIS_Ranking","KIS_EOD","KIS_Monthly","KIS_Backtest","KIS_KRX","KIS_DART","KIS_Paper","KIS_Recheck","KIS_Kiwoom","KIS_KiwoomBuy","KIS_KiwoomSell")) {
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
}

$action         = New-ScheduledTaskAction -Execute $bat         -Argument "auto" -WorkingDirectory $dir
$actionBacktest = New-ScheduledTaskAction -Execute $batBacktest -Argument "auto" -WorkingDirectory $dir
$actionKrx      = New-ScheduledTaskAction -Execute $batKrx      -Argument "auto" -WorkingDirectory $dir
$actionDart     = New-ScheduledTaskAction -Execute $batDart     -Argument "auto" -WorkingDirectory $dir
$actionPaper    = New-ScheduledTaskAction -Execute $batPaper    -Argument "auto" -WorkingDirectory $dir
$actionRecheck  = New-ScheduledTaskAction -Execute $batRecheck  -Argument "auto" -WorkingDirectory $dir
$actionKiwoom   = New-ScheduledTaskAction -Execute $batKiwoom   -Argument "auto" -WorkingDirectory $dir

function New-KisSettings {
    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $s.DisallowStartIfOnBatteries = $false
    $s.StopIfGoingOnBatteries = $false
    return $s
}

# [1/6] KIS_Ranking : daily 09:00, repeat every 30 min for 5h30m
$trg = New-ScheduledTaskTrigger -Daily -At "09:00"
$rep = New-ScheduledTaskTrigger -Once -At "09:00" `
        -RepetitionInterval (New-TimeSpan -Minutes 30) `
        -RepetitionDuration (New-TimeSpan -Hours 5 -Minutes 30)
$trg.Repetition = $rep.Repetition
Register-ScheduledTask -TaskName "KIS_Ranking" -Action $action -Trigger $trg `
        -Settings (New-KisSettings) -Force | Out-Null
Write-Host "[1/6] KIS_Ranking  - daily 09:00, every 30 min until 14:30"

# [2/6] KIS_EOD : daily 15:40
$trgEod = New-ScheduledTaskTrigger -Daily -At "15:40"
Register-ScheduledTask -TaskName "KIS_EOD" -Action $action -Trigger $trgEod `
        -Settings (New-KisSettings) -Force | Out-Null
Write-Host "[2/6] KIS_EOD      - daily 15:40"

# [3/6] KIS_Backtest : daily 16:00
$trgBt = New-ScheduledTaskTrigger -Daily -At "16:00"
Register-ScheduledTask -TaskName "KIS_Backtest" -Action $actionBacktest -Trigger $trgBt `
        -Settings (New-KisSettings) -Force | Out-Null
Write-Host "[3/6] KIS_Backtest - daily 16:00"

# [4/6] KIS_KRX : daily 08:30 (전일치 KRX 신용/공매도)
$trgKrx = New-ScheduledTaskTrigger -Daily -At "08:30"
Register-ScheduledTask -TaskName "KIS_KRX" -Action $actionKrx -Trigger $trgKrx `
        -Settings (New-KisSettings) -Force | Out-Null
Write-Host "[4/6] KIS_KRX      - daily 08:30 (T-1 credit/short balance)"

# [5/7] KIS_Paper : daily 15:50 (메인 전략 신호 + paper tracker)
#   변경 이유: 15:30 종가 → 15:50 신호 (20분 후) → 16:00 시간외 매수 시간 시작
#   시간외 2시간 전체 활용 가능 (이전 16:30 시작 시 30분 손실 = 25%)
$trgPaper = New-ScheduledTaskTrigger -Daily -At "15:50"
Register-ScheduledTask -TaskName "KIS_Paper" -Action $actionPaper -Trigger $trgPaper `
        -Settings (New-KisSettings) -Force | Out-Null
Write-Host "[5/7] KIS_Paper    - daily 15:50 (paper trading signals + tracker)"

# KIS_KiwoomBuy/Sell : 키움 모의투자 집행 (모의서버는 시간외단일가 미지원 → 원본 모드)
#   09:01 매수 (전일 신호, 시장가 ≈ 시가) / 15:21 매도 (만기, 마감 동시호가 ≈ 종가)
#   같은 bat 공용 — kiwoom_trader.py daily 가 시계로 매수/매도 분기
$trgKwBuy = New-ScheduledTaskTrigger -Daily -At "09:01"
Register-ScheduledTask -TaskName "KIS_KiwoomBuy" -Action $actionKiwoom -Trigger $trgKwBuy `
        -Settings (New-KisSettings) -Force | Out-Null
Write-Host "[+]   KIS_KiwoomBuy  - daily 09:01 (market-open buy)"
$trgKwSell = New-ScheduledTaskTrigger -Daily -At "15:21"
Register-ScheduledTask -TaskName "KIS_KiwoomSell" -Action $actionKiwoom -Trigger $trgKwSell `
        -Settings (New-KisSettings) -Force | Out-Null
Write-Host "[+]   KIS_KiwoomSell - daily 15:21 (closing-auction sell)"

# KIS_Recheck : daily 20:00 — 당일 수집 검증 + 누락분 재수집 + 신호/대시보드 후속 갱신
$trgRecheck = New-ScheduledTaskTrigger -Daily -At "20:00"
Register-ScheduledTask -TaskName "KIS_Recheck" -Action $actionRecheck -Trigger $trgRecheck `
        -Settings (New-KisSettings) -Force | Out-Null
Write-Host "[+]   KIS_Recheck  - daily 20:00 (collection verify + re-collect)"

# [6/7] KIS_DART : daily 19:00 (당일 공시)
$trgDart = New-ScheduledTaskTrigger -Daily -At "19:00"
Register-ScheduledTask -TaskName "KIS_DART" -Action $actionDart -Trigger $trgDart `
        -Settings (New-KisSettings) -Force | Out-Null
Write-Host "[5/6] KIS_DART     - daily 19:00 (today's disclosures)"

# [6/6] KIS_Monthly : 1st of month 02:00
$monthlyCmd = 'cmd /c cd /d "' + $dir + '" && py -3.11 "' + $pyMonthly + '"'
schtasks /create /tn "KIS_Monthly" /tr $monthlyCmd /sc monthly /mo 1 /d 1 /st 02:00 /f | Out-Null
$mt = Get-ScheduledTask -TaskName "KIS_Monthly"
$mt.Settings.StartWhenAvailable = $true
Set-ScheduledTask -TaskName "KIS_Monthly" -Settings $mt.Settings | Out-Null
Write-Host "[6/6] KIS_Monthly  - 1st of month 02:00"

Write-Host ""
Write-Host "=== Verification (next run time) ==="
$allOk = $true
foreach ($t in @("KIS_Ranking","KIS_EOD","KIS_Backtest","KIS_KRX","KIS_DART","KIS_Paper","KIS_KiwoomBuy","KIS_KiwoomSell","KIS_Recheck","KIS_Monthly")) {
    $info = Get-ScheduledTask -TaskName $t | Get-ScheduledTaskInfo
    $next = if ($info.NextRunTime) { $info.NextRunTime } else { "<NONE - PROBLEM>"; $allOk = $false }
    Write-Host ("  {0,-13} next: {1}" -f $t, $next)
}
Write-Host ""
if ($allOk) {
    Write-Host "All 7 tasks have a valid next-run time. OK." -ForegroundColor Green
} else {
    Write-Host "A task has no next-run time. Check above." -ForegroundColor Red
}
