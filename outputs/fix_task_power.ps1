# fix_task_power.ps1  (2026-10-09)
# Patch EXISTING scheduled tasks in place: allow start/continue on battery power.
#
# Why: Task Scheduler default is "Start the task only if the computer is on AC power".
#   On battery every trigger is refused (LastTaskResult 0x800710E0) and all missed jobs
#   fire together when AC returns. 2026-10-09: the scheduler daemon was alive all
#   morning, but 07:45/08:50/09:00/09:05/09:15/09:20 jobs all ran at 11:02.
#
# Scope: \StockAI\* (also fixed in register_tasks.ps1 for future re-registration)
#        + root KIS_* legacy tasks (KIS_Paper = universe collector, KIS_Tick_Collector, ...)
#        which register_tasks.ps1 does not own.
# Safe to re-run. Nothing else in the task (trigger/action/principal) is changed.
#
# Run: right-click fix_task_power.bat -> Run as administrator

$ErrorActionPreference = 'Stop'

if (-not (New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[ERROR] Not Administrator. Right-click fix_task_power.bat -> Run as administrator." -ForegroundColor Red
    exit 1
}

$tasks = @(Get-ScheduledTask | Where-Object { $_.TaskPath -like '\StockAI\*' -or $_.TaskName -like 'KIS_*' })
if ($tasks.Count -eq 0) {
    Write-Host "[WARN] no StockAI / KIS_* tasks found" -ForegroundColor Yellow
    exit 1
}

$ok = 0; $skip = 0; $fail = @()
foreach ($t in $tasks) {
    $name = $t.TaskPath + $t.TaskName
    $s = $t.Settings
    if (-not $s.DisallowStartIfOnBatteries -and -not $s.StopIfGoingOnBatteries) {
        Write-Host ("[SKIP] {0,-34} already battery-OK" -f $name) -ForegroundColor DarkGray
        $skip++
        continue
    }
    try {
        $s.DisallowStartIfOnBatteries = $false
        $s.StopIfGoingOnBatteries = $false
        Set-ScheduledTask -TaskPath $t.TaskPath -TaskName $t.TaskName -Settings $s | Out-Null
        $chk = (Get-ScheduledTask -TaskPath $t.TaskPath -TaskName $t.TaskName).Settings
        if ($chk.DisallowStartIfOnBatteries -or $chk.StopIfGoingOnBatteries) { throw "verify failed" }
        Write-Host ("[OK]   {0,-34} battery start/continue allowed" -f $name) -ForegroundColor Green
        $ok++
    } catch {
        # Tasks registered as "Run whether user is logged on or not" need the password to
        # be re-saved, so Set-ScheduledTask can fail here. Fix those by hand (see below).
        Write-Host ("[FAIL] {0,-34} {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
        $fail += $name
    }
}

Write-Host ""
Write-Host ("Done: patched {0}, already OK {1}, failed {2}" -f $ok, $skip, $fail.Count) -ForegroundColor Cyan
if ($fail.Count -gt 0) {
    Write-Host "Manual fix for failed tasks: Task Scheduler > task > Properties > Conditions tab" -ForegroundColor Yellow
    Write-Host "  uncheck 'Start the task only if the computer is on AC power' > OK" -ForegroundColor Yellow
    $fail | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    exit 2
}
exit 0
