# run_backfill.ps1 (UTF-8 with BOM) — 데이터 수집기 전체 1회 실행 (결측치 backfill 전용).
#
# 학습/백테스트/진단은 제외하고 '수집기만' 돈다. 수집기는 전부 증분/갭필이라
# 여러 번 돌려도 안전(이미 있는 데이터는 스킵, 빈 구간만 채움).
#
# 사용: powershell -ExecutionPolicy Bypass -File C:\fin\Stock_AI_Project\run_backfill.ps1
#
# 신용잔고/대차(credit_balance)는 키움 API 토큰 충돌 우려로 제외. 필요하면 매매 안 도는 시간에:
#   .\venv\Scripts\python.exe -m src.collector.kiwoom_extra --backfill --since 2026-05-01

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

Set-Location "C:\fin\Stock_AI_Project"
$py = ".\venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "[ERROR] venv 없음: $py" -ForegroundColor Red; exit 1 }

function Step($t, $blk) {
    Write-Host ""
    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Host " $t" -ForegroundColor Cyan
    Write-Host "==================================================" -ForegroundColor Cyan
    & $blk
    if ($LASTEXITCODE -ne 0) { Write-Host "[경고] 위 단계 비정상 종료 (ExitCode=$LASTEXITCODE) — 계속 진행" -ForegroundColor Yellow }
}

$t0 = Get-Date
Step "1) 주가 OHLCV (KOSPI/KOSDAQ + 미국, 갭필)" { & $py -m src.collector.main_collector }
Step "2) 거시지표 (NASDAQ/VIX/환율/SOX/KOSPI)"   { & $py -m src.collector.macro }
Step "3) 외국인/기관 수급 (KIS)"                 { & $py -m src.collector.supply_demand }
Step "4) 뉴스/공시 (전 섹터, 증분)"              { & $py -m src.processor.news historical all }
Step "5) 갭 감지 + 자동 백필 (마무리 점검)"      { & $py -m src.collector.weekend_audit }

$elapsed = [int]((Get-Date) - $t0).TotalMinutes
Write-Host ""
Write-Host "=== backfill 완료 ($elapsed 분) ===" -ForegroundColor Green
Write-Host "대시보드(localhost:5050) 새로고침 → 헬스 배너가 정상인지 확인." -ForegroundColor Green
