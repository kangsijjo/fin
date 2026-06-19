# 자동 실행 설정 가이드 (Windows)

매일 평일 장 마감 후, 또는 컴퓨터 시작 시 데이터 수집이 자동으로 실행되도록 설정하는 방법입니다.

## 사전 준비

### 1) Python 3.11 환경 확인

cmd에서 아래 둘 중 하나가 작동해야 합니다:

```cmd
py -3.11 --version
python --version
```

작동 안 하면 [python.org](https://www.python.org/downloads/)에서 Python 3.11 설치 후
설치 옵션에서 **"Add Python to PATH"** 와 **"py launcher"** 를 체크하세요.

### 2) 라이브러리 설치

```cmd
py -3.11 -m pip install -r requirements.txt
```

### 3) KIS API 키를 **시스템 환경변수**로 등록

자동 실행은 본인이 cmd 창에서 `export`로 설정한 변수를 못 봅니다.
**시스템 환경변수에 영구 등록**해야 합니다.

**방법 A: cmd로 한 줄씩 (관리자 권한 필요 없음, 사용자 변수)**

```cmd
setx KIS_APP_KEY "본인_APP_KEY"
setx KIS_APP_SECRET "본인_APP_SECRET"
```

→ **이후 새로 여는 cmd 창부터 적용됩니다.** 이미 열려 있는 창에는 적용 안 됨.

**방법 B: GUI (확실함)**

1. 시작 메뉴 → "환경 변수" 검색 → "시스템 환경 변수 편집" 클릭
2. "환경 변수(N)..." 버튼 클릭
3. **사용자 변수** 영역에서 "새로 만들기" 클릭
4. 변수 이름: `KIS_APP_KEY`, 변수 값: 본인 키
5. 같은 방법으로 `KIS_APP_SECRET` 도 추가
6. 모든 창 확인 후 닫기

### 4) 확인

새 cmd 창을 열고:

```cmd
echo %KIS_APP_KEY%
```

본인 키가 출력되면 OK.

## 자동 실행 등록

### 단계 1: 수동 실행으로 한 번 테스트

자동 등록 전에 반드시 한 번 직접 실행해서 정상 작동을 확인하세요.

```cmd
cd /d "프로젝트경로"
run_collector.bat
```

`logs\collect_YYYYMMDD.log` 파일이 생성되고, `data\rankings\`, `data\minute_bars\` 폴더에 CSV가 쌓이면 성공.

### 단계 2: 작업 스케줄러에 등록

`register_task.bat`을 **관리자 권한**으로 실행합니다:

1. `register_task.bat` 파일 우클릭
2. **"관리자 권한으로 실행"** 클릭
3. UAC 창이 뜨면 "예"
4. "등록 완료!" 메시지가 보이면 성공

이러면 두 개의 작업이 등록됩니다:

| 작업 이름 | 트리거 | 설명 |
|---|---|---|
| `KISDataCollector_Daily` | 평일 월~금 15:35 | 메인 — 장 마감 후 자동 수집 |
| `KISDataCollector_Startup` | 시스템 시작 후 3분 | 백업 — 컴퓨터 켤 때 누락분 수집 |

### 단계 3: 등록 확인

cmd에서:

```cmd
schtasks /query /tn "KISDataCollector_Daily"
schtasks /query /tn "KISDataCollector_Startup"
```

또는 시작 메뉴에서 "작업 스케줄러" 실행 → 라이브러리에서 위 두 작업 확인.

### 단계 4: 수동으로 한 번 실행해서 검증

작업 스케줄러가 정상 동작하는지 시뮬레이션:

```cmd
schtasks /run /tn "KISDataCollector_Daily"
```

`logs\` 폴더에 로그가 생기고 데이터가 들어오면 자동화 성공.

## 동작 방식 정리

```
평일 15:35
   │
   ▼
컴퓨터가 켜져 있나?
   │
   ├── YES → run_collector.bat 실행 → 데이터 수집
   │
   └── NO  → 작업 건너뜀 (Windows 기본 동작)
            └── 컴퓨터 다음에 켤 때 → 시작 후 3분 트리거 작동
                                       → run_collector.bat 실행 → 데이터 수집
```

같은 날 두 번 실행되더라도, `data_collector.py`의 스킵 로직 덕분에 이미 받은
데이터는 다시 호출하지 않습니다 (안전).

## 자동 실행 끄기

```cmd
unregister_task.bat
```

(관리자 권한 필요)

## 문제 해결

### "스케줄러는 실행했는데 로그가 안 생긴다"

대부분 환경변수 미인식 문제입니다. 작업 스케줄러는 본인 사용자 계정의 환경변수를
읽으므로, **사용자 환경변수**에 `KIS_APP_KEY`, `KIS_APP_SECRET` 이 있어야 합니다.
시스템 환경변수가 아니라 사용자 환경변수에 등록되었는지 확인하세요.

### "py: command not found"

Python 설치 시 **"py launcher"** 옵션을 체크 안 한 경우입니다.
Python 재설치 또는 `run_collector.bat`에서 `py -3.11`을 본인 Python 경로로 수정.
예: `C:\Python311\python.exe data_collector.py today`

### "갑자기 실행이 실패한다"

- `logs\` 폴더의 그날 로그 확인
- KIS API 토큰 만료 가능성 → `.kis_token.json` 파일 삭제 후 재시도
- 네트워크 문제 → 다음 트리거를 기다리거나 수동 실행

### "주말에도 컴퓨터 시작 시 실행돼서 신경 쓰임"

`run_collector.bat` 맨 위에 아래 추가:

```batch
REM 주말이면 종료
for /f %%i in ('powershell -Command "(Get-Date).DayOfWeek.value__"') do set DOW=%%i
if %DOW% GEQ 6 exit /b 0
if %DOW% EQU 0 exit /b 0
```

(월요일=1, ... 토요일=6, 일요일=0)

휴장 시에는 KIS API가 그날 데이터가 없다고 반환하므로 그냥 실행돼도 손해는 없습니다.
