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

    Write-Host ""
    Write-Host "=== Registered Tasks ===" -ForegroundColor Cyan
    Get-ScheduledTask -TaskPath "\StockAI\" | Format-Table TaskName, State -AutoSize
    Write-Host "Logs: C:\fin\logs\" -ForegroundColor Cyan

} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
