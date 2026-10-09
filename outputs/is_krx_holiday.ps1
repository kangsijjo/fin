# is_krx_holiday.ps1  (2026-10-09)  ASCII-only.
# Exit 10 if the date (default: today) is listed in krx_holidays.txt, else exit 0.
# Fail-open: missing file / read error -> exit 0 (job runs as before this helper existed).
# Weekends are NOT handled here (each bat already has its own weekend guard).
#
# Usage in .bat:
#   powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0is_krx_holiday.ps1" >nul 2>&1
#   if !ERRORLEVEL! EQU 10 echo [SKIP] KRX holiday & endlocal & exit /b 0
param([string]$Date = (Get-Date -Format 'yyyyMMdd'))
try {
    $f = Join-Path $PSScriptRoot 'krx_holidays.txt'
    if (-not (Test-Path $f)) { exit 0 }
    foreach ($line in (Get-Content -LiteralPath $f -Encoding UTF8)) {
        $t = $line.Trim()
        if ($t.Length -ge 8 -and $t.Substring(0, 8) -eq $Date) {
            Write-Output "KRX holiday $Date"
            exit 10
        }
    }
    exit 0
} catch {
    exit 0
}
