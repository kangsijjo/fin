# register_tasks.ps1
# Stock AI Task Scheduler Registration

$ErrorActionPreference = 'Stop'

try {
    if (-not (New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "[ERROR] Not Administrator. Right-click register_tasks.bat -> Run as administrator." -ForegroundColor Red
        exit 1
    }

    if (-not (Test-Path "C:\fin\logs")) { New-Item -ItemType Directory -Path "C:\fin\logs" | Out-Null }

    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

    # 1. Data collector scheduler: at logon + daily 06:10 (crash recovery)
    Write-Host "[1/3] Registering data collector scheduler..."
    $a1 = New-ScheduledTaskAction -Execute "C:\fin\outputs\start_scheduler.bat"
    $t1a = New-ScheduledTaskTrigger -AtLogOn
    $t1b = New-ScheduledTaskTrigger -Daily -At "06:10"
    $s1  = New-ScheduledTaskSettingsSet `
               -MultipleInstances IgnoreNew `
               -ExecutionTimeLimit ([TimeSpan]::Zero) `
               -StartWhenAvailable
    Register-ScheduledTask -TaskName "StockAI\Scheduler" `
        -Action $a1 -Trigger $t1a,$t1b -Settings $s1 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\Scheduler (at logon + daily 06:10)" -ForegroundColor Green

    # 2. Live signal detection: weekdays 18:30, run missed tasks on wake
    Write-Host "[2/3] Registering live signal detection..."
    $a2 = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_live_signal.bat"
    $t2  = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "18:30"
    $s2  = New-ScheduledTaskSettingsSet `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName "StockAI\LiveSignal" `
        -Action $a2 -Trigger $t2 -Settings $s2 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\LiveSignal (weekdays 18:30, runs on wake if missed)" -ForegroundColor Green

    # 3. Weekly profit report: Sunday 09:00, run missed tasks on wake
    Write-Host "[3/6] Registering weekly report..."
    $a3 = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_paper_audit.bat"
    $t3  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "09:00"
    $s3  = New-ScheduledTaskSettingsSet `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    Register-ScheduledTask -TaskName "StockAI\PaperAudit" `
        -Action $a3 -Trigger $t3 -Settings $s3 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\PaperAudit (Sunday 09:00, runs on wake if missed)" -ForegroundColor Green

    # 4. KIS live signal: weekdays 18:31 (1 min after kiwoom signal)
    Write-Host "[4/6] Registering KIS live signal..."
    $a4 = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kis_signal.bat"
    $t4  = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "18:31"
    $s4  = New-ScheduledTaskSettingsSet `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName "StockAI\KisSignal" `
        -Action $a4 -Trigger $t4 -Settings $s4 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\KisSignal (weekdays 18:31, runs on wake if missed)" -ForegroundColor Green

    # 5. KIS trader: weekdays 09:01 + at logon (부팅 시 자동 실행, bat 내부 주말 스킵)
    Write-Host "[5/6] Registering KIS trader..."
    $a5  = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kis_trader.bat"
    $t5a = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:01"
    $t5b = New-ScheduledTaskTrigger -AtLogOn
    $s5  = New-ScheduledTaskSettingsSet `
               -MultipleInstances IgnoreNew `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName "StockAI\KisTrader" `
        -Action $a5 -Trigger $t5a,$t5b -Settings $s5 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\KisTrader (weekdays 09:01 + at logon)" -ForegroundColor Green

    # 6. Dashboard: at logon (60s delay inside bat)
    Write-Host "[6/6] Registering dashboard..."
    $a6  = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_dashboard.bat"
    $t6  = New-ScheduledTaskTrigger -AtLogOn
    $s6  = New-ScheduledTaskSettingsSet `
               -MultipleInstances IgnoreNew `
               -ExecutionTimeLimit (New-TimeSpan -Hours 12)
    Register-ScheduledTask -TaskName "StockAI\Dashboard" `
        -Action $a6 -Trigger $t6 -Settings $s6 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\Dashboard (at logon, 60s delay)" -ForegroundColor Green

    # 7. KIS balance snapshot refresh: weekdays 15:40 (after market close)
    Write-Host "[7/7] Registering KIS balance refresh..."
    $a7  = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kis_status.bat"
    $t7  = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "15:40"
    $s7  = New-ScheduledTaskSettingsSet `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName "StockAI\KisBalance" `
        -Action $a7 -Trigger $t7 -Settings $s7 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\KisBalance (weekdays 15:40, runs on wake if missed)" -ForegroundColor Green

    # 8. KIS intraday stop-loss check: weekdays, every 15 min 09:05~15:20 (bat 내부 장중 가드)
    Write-Host "[8/8] Registering KIS intraday stop-loss check..."
    $a8 = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kis_stopcheck.bat"
    $t8 = New-ScheduledTaskTrigger -Daily -At "09:05"
    # 15분 간격 반복(6시간15분 동안) 부착 — Once 트리거의 Repetition 을 복사
    $rep = (New-ScheduledTaskTrigger -Once -At "09:05" `
                -RepetitionInterval (New-TimeSpan -Minutes 15) `
                -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 15)).Repetition
    $t8.Repetition = $rep
    $s8 = New-ScheduledTaskSettingsSet `
              -MultipleInstances IgnoreNew `
              -StartWhenAvailable `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName "StockAI\KisStopCheck" `
        -Action $a8 -Trigger $t8 -Settings $s8 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\KisStopCheck (weekdays, every 15min 09:05-15:20)" -ForegroundColor Green

    # 9. Kiwoom trader (안C): weekdays 09:03 매수 + 15:21 매도
    #    ※ run_kiwoom.bat 은 'daily' 가 시계로 분기(12시 이전=buy, 이후=sell)라 두 번 실행 필요.
    #    ※ 인자 'auto' 없으면 bat 이 pause 로 멈춤 → 반드시 -Argument "auto".
    Write-Host "[9/9] Registering Kiwoom trader..."
    $a9  = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kiwoom.bat" -Argument "auto"
    $t9a = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:03"
    $t9b = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "15:21"
    $s9  = New-ScheduledTaskSettingsSet `
               -MultipleInstances IgnoreNew `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName "StockAI\KiwoomTrader" `
        -Action $a9 -Trigger $t9a,$t9b -Settings $s9 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\KiwoomTrader (weekdays 09:03 buy + 15:21 sell)" -ForegroundColor Green

    # 10. 장중 잠정 예비후보(정보용): weekdays, 매시간 09:30~15:30 (bat 내부 장중 가드)
    Write-Host "[10/10] Registering intraday preview..."
    $a10 = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_intraday_preview.bat"
    $t10 = New-ScheduledTaskTrigger -Daily -At "09:30"
    $rep10 = (New-ScheduledTaskTrigger -Once -At "09:30" `
                  -RepetitionInterval (New-TimeSpan -Hours 1) `
                  -RepetitionDuration (New-TimeSpan -Hours 6)).Repetition
    $t10.Repetition = $rep10
    $s10 = New-ScheduledTaskSettingsSet `
               -MultipleInstances IgnoreNew `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    Register-ScheduledTask -TaskName "StockAI\IntradayPreview" `
        -Action $a10 -Trigger $t10 -Settings $s10 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\IntradayPreview (weekdays, hourly 09:30-15:30)" -ForegroundColor Green

    Write-Host ""
    Write-Host "=== Registered Tasks ===" -ForegroundColor Cyan
    Get-ScheduledTask -TaskPath "\StockAI\" | Format-Table TaskName, State -AutoSize
    Write-Host "Logs: C:\fin\logs\" -ForegroundColor Cyan

} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
