# ============================================================
#  setup_fin_monorepo.ps1   (UTF-8 with BOM)
#  C:\fin 를 단일 monorepo 로 만들어 github.com/kangsijjo/fin 에 올린다.
#  폴더(outputs / Stock_AI_Project)는 이동하지 않는다 — 라이브 절대경로 보존.
#  기존 두 repo 의 이력은 _backups 에 bundle 로 보존 후 nested .git 을 제거한다.
#
#  실행: powershell -ExecutionPolicy Bypass -File C:\fin\setup_fin_monorepo.ps1
# ============================================================

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Set-Location C:\fin

Write-Host "=== C:\fin monorepo 셋업 ===" -ForegroundColor Cyan
Write-Host "outputs\.git 와 Stock_AI_Project\.git 를 제거하고" -ForegroundColor Yellow
Write-Host "C:\fin 를 새 git 루트로 만들어 kangsijjo/fin 에 push 합니다." -ForegroundColor Yellow
Write-Host "(이력은 _backups 에 bundle 로 보존됩니다.)" -ForegroundColor Yellow
$ans = Read-Host "진행하려면 YES 를 입력"
if ($ans -ne 'YES') { Write-Host "취소됨."; exit 0 }

# 0) 사전 점검
if (-not (Test-Path 'C:\fin\.gitignore')) { throw 'C:\fin\.gitignore 가 없습니다. 먼저 생성하세요.' }

# 1) 기존 이력 bundle 보존
$ts  = Get-Date -Format 'yyyyMMdd_HHmm'
$bak = "C:\fin\_backups\git_history_$ts"
New-Item -ItemType Directory -Force -Path $bak | Out-Null
if (Test-Path 'C:\fin\outputs\.git') {
    git -C C:\fin\outputs bundle create "$bak\outputs.bundle" --all
}
if (Test-Path 'C:\fin\Stock_AI_Project\.git') {
    git -C C:\fin\Stock_AI_Project bundle create "$bak\Stock_AI_Project.bundle" --all
}
Write-Host "[OK] 기존 이력 보존: $bak" -ForegroundColor Green

# 2) nested .git 제거 + 빈 클론 폴더 제거
Remove-Item -Recurse -Force 'C:\fin\outputs\.git'           -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force 'C:\fin\Stock_AI_Project\.git'  -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force 'C:\fin\fin'                    -ErrorAction SilentlyContinue
Write-Host "[OK] nested .git 제거 완료" -ForegroundColor Green

# 3) C:\fin 를 git 루트로 init + 원격 연결
if (-not (Test-Path 'C:\fin\.git')) { git init | Out-Null }
git branch -M main
if ((git remote) -contains 'origin') { git remote remove origin }
git remote add origin https://github.com/kangsijjo/fin.git
Write-Host "[OK] git init + origin = kangsijjo/fin" -ForegroundColor Green

# 4) 스테이징 후 사람이 직접 확인
git add .
Write-Host ""
Write-Host "=== git status 요약 (데이터/시크릿이 안 잡히는지 확인!) ===" -ForegroundColor Cyan
$staged = (git diff --cached --name-only)
$count  = ($staged | Measure-Object).Count
Write-Host "스테이징된 파일 수: $count"
$leak = $staged | Select-String -Pattern '\.env|token|secret|\.key|\.pkl$|/db/|/data/|macro_data/|trades_history|paper_signals'
if ($leak) {
    Write-Host "[경고] 아래 항목이 스테이징됨 — .gitignore 점검 필요:" -ForegroundColor Red
    $leak | ForEach-Object { Write-Host "   $_" -ForegroundColor Red }
} else {
    Write-Host "[OK] 데이터/시크릿 패턴이 스테이징에 없음." -ForegroundColor Green
}
Write-Host ""
$go = Read-Host "위 내용이 맞으면 commit+push 진행 (YES 입력, 아니면 중단)"
if ($go -ne 'YES') {
    Write-Host "커밋 전 중단. .gitignore 수정 후 재실행하세요." -ForegroundColor Yellow
    exit 0
}

# 5) 커밋 + 롤백 태그 + push
git commit -m "monorepo: outputs(매매)+Stock_AI_Project(데이터) 통합 초기 커밋"
git tag rollback-$ts
git push -u origin main --tags
Write-Host ""
Write-Host "[완료] kangsijjo/fin 에 push 됨. 롤백 태그: rollback-$ts" -ForegroundColor Green
Write-Host "이후 롤백: git reset --hard rollback-$ts" -ForegroundColor DarkGray
