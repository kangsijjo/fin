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

    # 5. KIS trader: AM(09:01 stop매도+매수) / PM(15:21 만기매도=마감 동시호가) 분리.
    #    [2026-07-17] 만기 매도를 종가 청산(백테스트 가정)과 정합시키기 위해 PM 트리거
    #    신설 + am/pm 명시 인자(지연복구 가드는 kis_trader.py daily-am/pm 내부).
    Write-Host "[5/6] Registering KIS trader (AM/PM split)..."
    Unregister-ScheduledTask -TaskName "KisTrader" -TaskPath "\StockAI\" -Confirm:$false -ErrorAction SilentlyContinue
    $s5  = New-ScheduledTaskSettingsSet `
               -MultipleInstances IgnoreNew `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $a5a = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kis_trader.bat" -Argument "am"
    $t5a = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:01"
    $t5b = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName "StockAI\KisTraderAM" `
        -Action $a5a -Trigger $t5a,$t5b -Settings $s5 -Principal $principal -Force | Out-Null
    $a5c = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kis_trader.bat" -Argument "pm"
    $t5c = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "15:21"
    Register-ScheduledTask -TaskName "StockAI\KisTraderPM" `
        -Action $a5c -Trigger $t5c -Settings $s5 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\KisTraderAM(09:01 stop+buy, +logon) / KisTraderPM(15:21 expiry)" -ForegroundColor Green

    # 6. Dashboard: at logon (60s delay inside bat)
    Write-Host "[6/6] Registering dashboard..."
    $a6  = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_dashboard.bat"
    $t6  = New-ScheduledTaskTrigger -AtLogOn
    # [2026-07-12] 12h 한도 제거 — start 로 띄운 상주 서버가 잡 오브젝트에 남으면
    # 로그온 12시간 뒤 트리 강제종료로 대시보드가 죽고 재로그온까지 미복구.
    # 상주 데몬 패턴은 Scheduler 작업과 동일하게 무제한이 맞음.
    $s6  = New-ScheduledTaskSettingsSet `
               -MultipleInstances IgnoreNew `
               -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName "StockAI\Dashboard" `
        -Action $a6 -Trigger $t6 -Settings $s6 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\Dashboard (at logon, 60s delay)" -ForegroundColor Green

    # 7. KIS balance snapshot refresh: weekdays 15:40 (after market close)
    Write-Host "[7/7] Registering KIS balance refresh..."
    # ※ 인자 'auto' 없으면 bat 이 pause 로 멈춤(수동클릭 시 결과확인용 pause) → 예약은 반드시 "auto".
    $a7  = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kis_status.bat" -Argument "auto"
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

    # 9. Kiwoom trader (안C): weekdays 09:03 매수 + 15:21 매도 — 트리거별 별도 작업.
    #    [2026-07-12] 단일 작업+시계분기였을 때 StartWhenAvailable 지연복구가 정오를
    #    넘기면 09:03 '매수' 트리거가 매도 경로로 실행되던 문제 → am/pm 인자를 명시
    #    전달하는 2개 작업으로 분리(파이썬 쪽 지연복구 가드와 세트).
    #    ※ 인자 'auto' 없으면 bat 이 pause 로 멈춤 → 반드시 -Argument.
    Write-Host "[9/9] Registering Kiwoom trader (AM/PM split)..."
    Unregister-ScheduledTask -TaskName "KiwoomTrader" -TaskPath "\StockAI\" -Confirm:$false -ErrorAction SilentlyContinue
    $s9  = New-ScheduledTaskSettingsSet `
               -MultipleInstances IgnoreNew `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $a9a = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kiwoom.bat" -Argument "auto am"
    $t9a = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:03"
    Register-ScheduledTask -TaskName "StockAI\KiwoomTraderAM" `
        -Action $a9a -Trigger $t9a -Settings $s9 -Principal $principal -Force | Out-Null
    $a9b = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_kiwoom.bat" -Argument "auto pm"
    $t9b = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "15:21"
    Register-ScheduledTask -TaskName "StockAI\KiwoomTraderPM" `
        -Action $a9b -Trigger $t9b -Settings $s9 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\KiwoomTraderAM(09:03 buy) + KiwoomTraderPM(15:21 sell)" -ForegroundColor Green

    # 10. 장중 생산기(시황 2x6 그리드 + 잠정 예비후보): weekdays, 15분마다 09:15~15:00
    #     (15:15/15:30 런 제거 → 15:21 키움 매도와 시황(키움) 호출 충돌 차단)
    Write-Host "[10/10] Registering intraday producer (grid+preview)..."
    $a10 = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_intraday_preview.bat"
    $t10 = New-ScheduledTaskTrigger -Daily -At "09:15"
    $rep10 = (New-ScheduledTaskTrigger -Once -At "09:15" `
                  -RepetitionInterval (New-TimeSpan -Minutes 15) `
                  -RepetitionDuration (New-TimeSpan -Hours 5 -Minutes 45)).Repetition
    $t10.Repetition = $rep10
    $s10 = New-ScheduledTaskSettingsSet `
               -MultipleInstances IgnoreNew `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Minutes 14)
    Register-ScheduledTask -TaskName "StockAI\IntradayPreview" `
        -Action $a10 -Trigger $t10 -Settings $s10 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\IntradayPreview (weekdays, every 15min 09:15-15:00)" -ForegroundColor Green

    # 11. Weekly AI pipeline (light, gated): Sunday 03:00 - collect -> dataset -> train (Optuna)
    Write-Host "[11/11] Registering weekly AI pipeline..."
    $a11 = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_ai_pipeline.bat" -Argument "auto"
    $t11 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "03:00"
    $s11 = New-ScheduledTaskSettingsSet `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    Register-ScheduledTask -TaskName "StockAI\AIPipeline" `
        -Action $a11 -Trigger $t11 -Settings $s11 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\AIPipeline (Sunday 03:00, gated: collect->dataset->train)" -ForegroundColor Green

    # 12. Watchdog (silent-failure detector -> Telegram): problems-only at 07:45/09:20/18:50
    Write-Host "[12/13] Registering watchdog (3x daily checks)..."
    $a12  = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_watchdog.bat"
    $t12a = New-ScheduledTaskTrigger -Daily -At "07:45"
    $t12b = New-ScheduledTaskTrigger -Daily -At "09:20"
    $t12c = New-ScheduledTaskTrigger -Daily -At "18:50"
    $s12  = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName "StockAI\Watchdog" `
        -Action $a12 -Trigger $t12a,$t12b,$t12c -Settings $s12 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\Watchdog (07:45/09:20/18:50, alert on problems)" -ForegroundColor Green

    # 13. Watchdog daily heartbeat: 23:00 --daily (정상이어도 전송 → 침묵=경보)
    Write-Host "[13/13] Registering watchdog daily heartbeat..."
    $a13 = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_watchdog.bat" -Argument "--daily"
    $t13 = New-ScheduledTaskTrigger -Daily -At "23:00"
    $s13 = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName "StockAI\WatchdogDaily" `
        -Action $a13 -Trigger $t13 -Settings $s13 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\WatchdogDaily (daily 23:00 heartbeat)" -ForegroundColor Green

    # 14. After-market collector (시간외 단일가 등락률 → stock.db): weekdays 18:10 (16:00~18:00 종료 후)
    Write-Host "[14/14] Registering after-market collector..."
    $a14 = New-ScheduledTaskAction -Execute "C:\fin\outputs\run_after_market.bat"
    $t14 = New-ScheduledTaskTrigger -Weekly `
               -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "18:10"
    $s14 = New-ScheduledTaskSettingsSet `
               -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName "StockAI\AfterMarket" `
        -Action $a14 -Trigger $t14 -Settings $s14 -Principal $principal -Force | Out-Null
    Write-Host "[OK] StockAI\AfterMarket (weekdays 18:10, 시간외 등락률 -> stock.db)" -ForegroundColor Green

    Write-Host ""
    Write-Host "=== Registered Tasks ===" -ForegroundColor Cyan
    Get-ScheduledTask -TaskPath "\StockAI\" | Format-Table TaskName, State -AutoSize
    Write-Host "Logs: C:\fin\logs\" -ForegroundColor Cyan

} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
