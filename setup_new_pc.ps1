# setup_new_pc.ps1 — 새 PC 파이썬 환경 한 번에 설치(천억이 outputs\.venv + Stock_AI_Project\venv)
# 실행: PowerShell 에서  powershell -ExecutionPolicy Bypass -File C:\fin\setup_new_pc.ps1
# 하는 일: ① Python 3.11·Git 확인 ② 두 폴더에 requirements-freeze.txt 생성(옛 PC 와 같은 버전)
#          ③ 가상환경이 없으면 만들고 있으면 그대로 사용 ④ 패키지 설치 ⑤ 핵심 모듈 불러오기 확인 ⑥ 테스트(pytest)
# 지우는 것 없음. 여러 번 실행해도 안전(이미 깔린 건 건너뜀).
$ErrorActionPreference = "Continue"
$FIN = "C:\fin"
$fail = @()
function Say($m, $c = "Cyan") { Write-Host $m -ForegroundColor $c }

Say "=== 1/6 Python 3.11 확인 ==="
& py -3.11 -c "import sys; print(sys.version)"
if ($LASTEXITCODE -ne 0) {
    Say "Python 3.11 이 없습니다. 설치 후 이 스크립트를 다시 실행하세요:" Red
    Say "  winget install -e --id Python.Python.3.11   (또는 python.org 에서 3.11.9, 'Add to PATH'·'py launcher' 체크)" Yellow
    exit 1
}
$hasGit = $null -ne (Get-Command git -ErrorAction SilentlyContinue)
if (-not $hasGit) { Say "Git 이 없어 FinanceDataReader 는 PyPI 판으로 설치합니다(Git 설치 권장: winget install -e --id Git.Git)" Yellow }

$OUT_REQ = @'
alembic==1.18.5
annotated-doc==0.0.4
anyio==4.13.0
arch==8.0.0
backtrader==1.9.78.123
beautifulsoup4==4.14.3
blinker==1.9.0
certifi==2026.4.22
cffi==2.0.0
charset-normalizer==3.4.7
click==8.3.3
cloudpickle==3.1.2
colorama==0.4.6
colorlog==6.10.1
contourpy==1.3.3
curl_cffi==0.15.0
cycler==0.12.1
Deprecated==1.3.1
filelock==3.29.0
finance-datareader==0.9.202
Flask==3.1.3
flask-cors==6.0.5
fonttools==4.62.1
formulaic==1.2.2
fsspec==2026.4.0
greenlet==3.5.3
h11==0.16.0
hf-xet==1.5.0
httpcore==1.0.9
httpx==0.28.1
huggingface_hub==1.14.0
idna==3.13
iniconfig==2.3.0
interface_meta==2.0.1
itsdangerous==2.2.0
Jinja2==3.1.6
joblib==1.5.3
kiwisolver==1.5.0
kiwoom-rest-api==0.1.12
lightgbm==4.6.0
llvmlite==0.44.0
lxml==6.1.0
Mako==1.3.12
markdown-it-py==4.2.0
MarkupSafe==3.0.3
matplotlib==3.10.9
mdurl==0.1.2
mpmath==1.3.0
multipledispatch==1.0.0
multitasking==0.0.13
narwhals==2.20.0
networkx==3.6.1
numba==0.61.2
numpy==2.2.6
optuna==4.9.0
packaging==26.2
pandas==2.3.3
pandas-ta-classic==0.6.20
patsy==1.0.3
peewee==4.0.6
pillow==12.2.0
platformdirs==4.10.0
plotly==6.7.0
pluggy==1.6.0
protobuf==7.35.0
pycparser==3.0
Pygments==2.20.0
pykrx==1.2.8
pyparsing==3.3.2
pytest==9.1.1
python-dateutil==2.9.0.post0
python-dotenv==1.2.2
pytz==2026.2
PyYAML==6.0.3
regex==2026.4.4
requests==2.33.1
requests-file==3.0.1
rich==15.0.0
safetensors==0.7.0
schedule==1.2.2
scikit-learn==1.8.0
scipy==1.17.1
shap==0.51.0
shellingham==1.5.4
six==1.17.0
slicer==0.0.8
soupsieve==2.8.3
SQLAlchemy==2.0.51
statsmodels==0.15.0
sympy==1.14.0
tabulate==0.10.0
threadpoolctl==3.6.0
tokenizers==0.22.2
torch==2.11.0
tqdm==4.67.3
transformers==5.8.0
typer==0.25.1
typing_extensions==4.15.0
tzdata==2026.2
urllib3==2.6.3
websockets==16.0
Werkzeug==3.1.8
wrapt==2.1.2
xgboost==3.2.0
yfinance==1.4.1
'@
$SAI_REQ = @'
FinRL==0.3.7
absl-py==2.4.0
aiodns==4.0.0
aiohappyeyeballs==2.6.1
aiohttp==3.13.5
aiosignal==1.4.0
alpaca-trade-api==0.48
alpha_vantage==3.0.0
altair==6.1.0
annotated-doc==0.0.4
anyio==4.13.0
asttokens==3.0.1
asyncio-nats-client==0.11.5
attrs==26.1.0
backtrader==1.9.78.123
beautifulsoup4==4.14.3
blinker==1.9.0
Bottleneck==1.6.0
cachetools==7.1.4
ccxt==4.5.52
certifi==2026.5.20
cffi==2.0.0
charset-normalizer==3.4.7
click==8.3.3
cloudpickle==3.1.2
coincurve==21.0.0
colorama==0.4.6
contourpy==1.3.3
cryptography==48.0.0
curl_cffi==0.15.0
cycler==0.12.1
decorator==5.2.1
Deprecated==1.3.1
deprecation==2.1.0
dotenv==0.9.9
empyrical-reloaded==0.5.12
exchange_calendars==4.13.2
executing==2.2.1
Farama-Notifications==0.0.6
filelock==3.29.0
finance-datareader @ git+https://github.com/FinanceData/FinanceDataReader.git
fonttools==4.62.1
frozendict==2.4.7
frozenlist==1.8.0
fsspec==2026.4.0
gitdb==4.0.12
GitPython==3.1.50
greenlet==3.5.0
grpcio==1.80.0
gymnasium==1.2.3
h11==0.16.0
hf-xet==1.5.0
httpcore==1.0.9
httptools==0.8.0
httpx==0.28.1
huggingface_hub==1.14.0
idna==3.13
ipython==9.8.0
ipython_pygments_lexers==1.1.1
itsdangerous==2.2.0
jedi==0.20.0
Jinja2==3.1.6
joblib==1.5.3
jqdatasdk==1.9.8
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
kiwisolver==1.5.0
kiwoom-rest-api==0.1.12
korean-lunar-calendar==0.3.1
lightgbm==4.6.0
llvmlite==0.44.0
lxml==6.1.0
Markdown==3.10.2
markdown-it-py==4.2.0
MarkupSafe==3.0.3
matplotlib==3.10.9
matplotlib-inline==0.2.2
mdurl==0.1.2
mpmath==1.3.0
msgpack==1.0.3
multidict==6.7.1
multipledispatch==1.0.0
multitasking==0.0.13
narwhals==2.20.0
networkx==3.6.1
numba==0.61.2
numpy==2.2.6
packaging==26.2
pandas==2.3.3
pandas-ta-classic==0.5.44
parso==0.8.7
peewee==3.17.3
pillow==12.2.0
platformdirs==4.9.6
plotly==6.7.0
ply==3.11
prompt_toolkit==3.0.52
propcache==0.5.2
protobuf==7.34.1
psycopg2-binary==2.9.12
pure_eval==0.2.3
pyarrow==24.0.0
pycares==5.0.1
pycparser==3.0
pydeck==0.9.2
pyfolio-reloaded==0.9.9
Pygments==2.20.0
pykrx==1.2.8
pyluach==2.3.0
PyMySQL==1.1.3
pyparsing==3.3.2
python-dateutil==2.9.0.post0
python-dotenv==1.2.2
python-multipart==0.0.29
pytz==2026.2
PyYAML==6.0.3
ray==2.55.1
referencing==0.37.0
regex==2026.4.4
requests==2.33.1
requests-file==3.0.1
rich==13.9.4
rpds-py==0.30.0
safetensors==0.7.0
schedule==1.2.2
scikit-learn==1.8.0
scipy==1.17.1
seaborn==0.13.2
shap==0.51.0
shellingham==1.5.4
Shimmy==2.0.1
six==1.17.0
slicer==0.0.8
smmap==5.0.3
soupsieve==2.8.3
SQLAlchemy==2.0.49
stable_baselines3==2.8.0
stack-data==0.6.3
starlette==1.2.0
stockstats==0.6.8
streamlit==1.58.0
sympy==1.14.0
tenacity==9.1.4
tensorboard==2.20.0
tensorboard-data-server==0.7.2
threadpoolctl==3.6.0
thriftpy2==0.4.20
tokenizers==0.22.2
toml==0.10.2
toolz==1.1.0
torch==2.11.0
tqdm==4.67.3
traitlets==5.15.0
transformers==5.8.0
typer==0.9.4
typing_extensions==4.15.0
tzdata==2026.2
urllib3==2.6.3
uvicorn==0.48.0
watchdog==6.0.0
wcwidth==0.7.0
websocket-client==1.9.0
websockets==16.0
Werkzeug==3.1.8
wrapt==2.1.2
wrds==3.5.0
yarl==1.23.0
yfinance==1.3.0
certifi==2026.4.22
'@
if (-not $hasGit) { $SAI_REQ = $SAI_REQ -replace 'finance-datareader @ git\+\S+', 'finance-datareader' }

$projects = @(
    @{ Name = "천억이(outputs)";        Dir = "$FIN\outputs";           Venv = ".venv"; Req = $OUT_REQ;
       Check = "import pandas, numpy, xgboost, sklearn, flask, dotenv, pykrx, kiwoom_rest_api; print('OK', pandas.__version__, xgboost.__version__)" },
    @{ Name = "수집(Stock_AI_Project)"; Dir = "$FIN\Stock_AI_Project";  Venv = "venv";  Req = $SAI_REQ;
       Check = "import pandas, numpy, sklearn, FinanceDataReader, requests; print('OK', pandas.__version__)" }
)

$step = 2
foreach ($p in $projects) {
    Say "=== $step/6 $($p.Name) — 가상환경·패키지 ==="
    if (-not (Test-Path $p.Dir)) { Say "  폴더 없음: $($p.Dir)" Red; $fail += "$($p.Name): 폴더 없음"; continue }
    Set-Location $p.Dir
    $reqFile = Join-Path $p.Dir "requirements-freeze.txt"
    [System.IO.File]::WriteAllText($reqFile, ($p.Req -replace "`r?`n", "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
    $py = Join-Path $p.Dir "$($p.Venv)\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        Say "  가상환경 만드는 중: $($p.Venv)"
        & py -3.11 -m venv $p.Venv
    } else { Say "  기존 가상환경 사용: $($p.Venv)" }
    if (-not (Test-Path $py)) { Say "  가상환경 생성 실패" Red; $fail += "$($p.Name): venv 생성 실패"; continue }
    & $py -m pip install --upgrade pip --disable-pip-version-check -q
    Say "  패키지 설치 중(torch 등 커서 수 분 걸릴 수 있음)..."
    & $py -m pip install -r $reqFile --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        Say "  일부 패키지 설치 실패 — 버전 고정 없이 한 번 더 시도(실패한 것만 최신판으로)" Yellow
        $loose = Join-Path $env:TEMP "req_loose_$($p.Venv).txt"
        ($p.Req -split "`r?`n" | ForEach-Object { ($_ -replace '==.*$', '') }) | Set-Content -Encoding ascii $loose
        & $py -m pip install -r $loose --disable-pip-version-check
        if ($LASTEXITCODE -ne 0) { $fail += "$($p.Name): pip 설치 실패(위 빨간 줄 확인)" }
    }
    & $py -c $p.Check
    if ($LASTEXITCODE -ne 0) { $fail += "$($p.Name): 모듈 불러오기 실패" } else { Say "  모듈 확인 OK" Green }
    $step++
}

Say "=== 5/6 천억이 테스트(pytest, 2~3분) ==="
Set-Location "$FIN\outputs"
& "$FIN\outputs\.venv\Scripts\python.exe" -m pytest tests -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { $fail += "pytest 실패(위 FAILED 줄 확인)" }

Say "=== 6/6 결과 ==="
if ($fail.Count -eq 0) {
    Say "모두 정상입니다." Green
    Say "다음: C:\fin\outputs\register_tasks.bat 를 '관리자 권한으로 실행'해 작업 스케줄러 등록 → run_dashboard.bat 로 대시보드 확인" Yellow
} else {
    Say "문제가 있습니다:" Red
    $fail | ForEach-Object { Say "  - $_" Red }
    Say "이 창의 마지막 30줄 정도를 복사해 보내 주세요." Yellow
}
