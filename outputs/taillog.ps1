# taillog.ps1 - 최신 C:\fin\logs\<prefix>*.log 를 UTF-8 로 실시간 따라가기(한글 안 깨짐).
#
# 사용:
#   powershell -ExecutionPolicy Bypass -File C:\fin\outputs\taillog.ps1 ai_pipeline
#   또는 PS 안에서:  .\taillog.ps1 ai_pipeline
#   prefix 생략 시 기본 ai_pipeline. 다른 예: kis_trader, paper, collect, kis_signal ...
param(
    [string]$Prefix = "ai_pipeline",
    [int]$Tail = 30
)

# 콘솔 자체도 UTF-8 로(혹시 모를 깨짐 방지)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$f = Get-ChildItem "C:\fin\logs\$Prefix*.log" -ErrorAction SilentlyContinue |
     Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not $f) {
    Write-Host "[taillog] C:\fin\logs\$Prefix*.log 없음" -ForegroundColor Yellow
    Write-Host "  사용 가능한 로그:" -ForegroundColor DarkGray
    Get-ChildItem "C:\fin\logs\*.log" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 10 Name | Format-Table -HideTableHeaders
    exit 1
}

Write-Host "tailing (UTF-8): $($f.FullName)" -ForegroundColor Cyan
Write-Host "  (Ctrl+C 로 중단 - 파이프라인은 계속 돕니다)" -ForegroundColor DarkGray
Get-Content $f.FullName -Wait -Tail $Tail -Encoding UTF8
