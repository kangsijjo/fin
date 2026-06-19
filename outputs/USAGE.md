# 천억이 — KOSDAQ 자동매매 시스템 사용 가이드

**구성: 1부 사용법(이것만 봐도 운영 가능) → 2부 돌아가는 원리·파일 상세 → 3부 업데이트 내역.**
최종 업데이트: 2026-06-19 (8차 — 대시보드 버그 수정 + 운영 지침 추가).

**키움 모의 — 안C 포트폴리오 (10슬롯, 2026-06-17~)**: 슬롯 1~4 `high_52w_filt` (52주 신고가+게이트, 20일) / 슬롯 5~8 `rsi_reversal` (RSI<30 반전, 5일) / 슬롯 9~10 `rsi_vol` (RSI<30+거래량2배, 7일). 청산 오버레이 없음(만기 보유 최적).

**KIS 모의 — 안D 포트폴리오 (10슬롯, 2026-06-18~)**: 슬롯 1~4 `h52w_for3d_mkt` (52주신고가+외국인3일+시장강세, 20일 / stop -15%) / 슬롯 5~8 `for_high20_mkt` (20일신고가+외국인3일+시장강세, 20일 / stop -10%) / 슬롯 9~10 `gc_for3d` (골든크로스+외국인3일, 15일 / stop -26%). 두 증권사는 **완전히 다른 전략** → 상호 비교 가능.

백테스트 2019~2025 총자본 기준 CAGR (슬롯 회전 반영):

| 포트폴리오 | CAGR | 비고 |
|---|---|---|
| 이전 6:2:2 (high/for/rsi) | -2.6% | for_high20_mkt 2022·2024 부진 |
| **안C 4:4:2 (high/rsi/vol)** | **+11.6%** | **현재 적용 포트** |

연도별 안C 기대수익 (총자본 대비):

| 연도 | 수익률 | 비고 |
|---|---|---|
| 2019 | +22.6% | |
| 2020 | +34.1% | 코로나 회복 강세장 |
| 2021 | +37.1% | 유동성 강세장 |
| 2022 | -54.0% | 금리 인상 하락장 (전략 간 상관↑) |
| 2023 | +29.9% | 반등 |
| 2024 | +16.2% | 횡보 (이전 포트 -20.1% 대비 방어) |
| 2025 | +37.4% | 강세 회복 |

> 2022 하락장은 전략 간 상관관계가 높아지는 구간으로 어떤 포트도 방어가 어려움.
> CAGR +11.6%는 백테스트(비용 0.245% 적용, look-ahead 없음) 기준이며 실제 운용 수익은 다를 수 있음.

---

# 1부 — 사용법

## 1.1 평소에 할 일: 없음 (전부 자동)

| 시각 (평일) | 작업 | 내용 |
|---|---|---|
| 08:30 | KIS_KRX | T-1 공매도 |
| **09:01** | **KIS_KiwoomBuy** | 전일 신호 종목 키움 모의계좌 시장가 매수 (≈ 시가 체결) — `kiwoom_trader.py buy` |
| 09:00~14:30 | KIS_Ranking | 30분마다 분봉/랭킹/지수 |
| 15:40 | KIS_EOD | 장 마감 수집 |
| **15:21** | **KIS_KiwoomSell** | 키움 만기 종목 시장가 매도 — `kiwoom_trader.py sell` |
| **15:50** | **KIS_Paper** | pykrx 일봉 수집 → macro 갱신 → 신호 감지 → 페이퍼 추적 → 대시보드 (금요일엔 +AI 주간학습) |
| **18:30** | **StockAI\LiveSignal** | 키움 신호 감지 — `live_signal.py` → `paper_signals.csv` |
| **18:31** | **StockAI\KisSignal** | KIS 신호 감지 — `kis_live_signal.py` → `kis_paper_signals.csv` |
| 16:00 | KIS_Backtest | 단타 v2 (별건) |
| 19:00 | KIS_DART | 당일 공시 |
| **20:00** | **KIS_Recheck** | 당일 수집 검증 → 누락분 재수집 → 신호/대시보드 후속 갱신 |
| 매월 1일 02:00 | KIS_Monthly | xlsx 합본 |

조건: PC 켜짐 + 로그인 상태. 꺼져 있던 시간의 작업은 부팅 후 자동 보충 실행됨.

## 1.2 주 1회 확인 루틴 (2분)

1. `dashboard.html` 열어 포지션/손익 확인
2. `C:\fin\logs\live_signal_*.log` — 최근 평일 신호 결과 확인 (팩터 점수 포함)
3. `C:\fin\logs\paper_audit_*.log` (일요일 09:00 자동 생성) — drift 없으면 OK
4. `logs\recheck_*.log` 마지막 줄이 "모든 필수 항목 정상"인지 확인 — [FAIL] 있으면 그때만 개입

## 1.3 수동 명령 (필요할 때만)

| 목적 | 명령 |
|---|---|
| 오늘 신호 즉시 확인 | `python live_signal.py` |
| 페이퍼 손익/포지션 | `python paper_tracker.py` |
| **paper vs 백테스트 drift 감사** | `python paper_audit.py` → `paper_audit_result.csv` |
| 키움 모의 상태 확인 | `python kiwoom_trader.py status` |
| **KIS 모의 상태 확인** | **`python kis_trader.py status`** |
| **전략 기준선 백테스트** | **`python strategy_engine.py --start 20190101 --end 20251231`** |
| **손익 오버레이 테스트** | **`python profit_target_test.py --start 20190101 --end 20251231 --target 7 --stop -8 --no-cache`** |
| 밀린 데이터 즉시 백필 | `run_recheck.bat` |
| **8년치 과거 백필 (1회 권장, 밤에)** | `python pykrx_collector.py 8` |
| AI 수동 재학습 | `python make_trades_history_v3.py` → `python ai_trainer_v4.py` |
| **신용/대차 백필 완료 후 AI 재구성** | `rebuild_after_backfill.bat` (백필 종료 직후 1회) |
| 신용잔고 API 단독 검증 | `python credit_collector.py probe` |
| CA(액면분할)필터 표본 검증 | `python adjusted_probe.py` |
| **팩터 스코어 단독 테스트** | `python factor_scorer.py` (IC 가중치 + 현재 피처 점수 출력) |
| 스케줄 재등록 (KIS_* 작업) | `.\install_scheduler.ps1` (자동으로 관리자 권한 요청) |
| **StockAI 작업 등록 (1회만)** | `register_tasks.bat` 우클릭 → 관리자 권한으로 실행 |
| **대시보드 시작** | `start_dashboard.bat` 더블클릭 (venv 자동 선택) |
| 대시보드 수동 시작 | `(.venv) > python integrated_dashboard_server.py` → 브라우저 `http://localhost:5050` |

> **대시보드 주의**: `python integrated_dashboard_server.py` 를 직접 실행하지 말고 `start_dashboard.bat` 를 사용할 것. 직접 실행 시 시스템 Python(Flask 없음)이 선택될 수 있음. 포트 5050이 이미 사용 중이면 "포트 이미 사용 중" 메시지 출력 후 자동 종료됨.

## 1.4 문제가 생기면

- `logs\recheck_YYYYMMDD.log` 에서 [FAIL] 항목 확인 → 위 표의 해당 수집기 수동 실행
- 한글 깨짐/!변수! 그대로 출력 → .bat 파일이 ASCII+CRLF 인지 확인 (메모장 다른이름저장 시 주의)
- KRX/pykrx 가 사이트 개편으로 깨지면 recheck 로그에 [FAIL] 로 나타남 → 그때 점검
- **대시보드 "로드 실패" / 데이터 없음** → `start_dashboard.bat` 로 서버를 새로 시작한 후 브라우저 Ctrl+Shift+R 로 강력 새로고침. CMD 창에 `[Dashboard] /api/all ERROR` 메시지가 있으면 내용을 확인해 조치

## 1.5 키움 모의투자 자동매매 (2026-06-12 신설 / 2026-06-16 3전략 전환)

> **KIS 모의투자 트레이더도 별도 존재** — 섹션 1.5b 참조. 두 트레이더가 같은 `paper_signals.csv`와 `db/kiwoom/`을 공유하지만 독립 실행 가능.

⚠️ **키움 모의서버는 시간외단일가 미지원**(지정가/시장가만) — **원본 모드** 운용.  
매수는 전략별 슬롯 상한(6/2/2) 배정 → 거래대금 내림차순 선택 → 시장가 매수.

| 시각 | 작업 | 내용 |
|---|---|---|
| 09:01 | KIS_KiwoomBuy | 전일 신호 종목 전략별 슬롯 배정 후 시장가 매수 (≈ 시가 체결) |
| 15:21 | KIS_KiwoomSell | 전략별 보유일 만기 종목 시장가 매도 (rsi_reversal: 5일, 나머지: 20일) |

주문 로그 `db/kiwoom/orders_*.csv`, 실행 로그 `logs/kiwoom_*.log`.
paper_tracker 는 X2(시간외) 모드를 계속 추적 — **두 모드 병행 검증** 체계.
키움 계좌 내역은 아직 dashboard.html 에 미표시 (확인은 `python kiwoom_trader.py status`).

**최초 설정 (1회)**:

1. `.env` 에 추가:
```env
# 키움증권 REST API (openapi.kiwoom.com 에서 발급)
KIWOOM_ENV=mock
KIWOOM_MOCK_APP_KEY=모의용_앱키
KIWOOM_MOCK_APP_SECRET=모의용_시크릿
KIWOOM_PROD_APP_KEY=실전용_앱키
KIWOOM_PROD_APP_SECRET=실전용_시크릿
```
2. `pip install kiwoom-rest-api` (.venv 활성화 후 — PyPI 명칭 주의, "kiwoom-api" 아님)
3. 연결 검증: `python kiwoom_trader.py status` → 예수금/잔고가 나오면 OK
4. `.\install_scheduler.ps1` 재실행 (KIS_Kiwoom 16:05 등록)

**수동 명령**: `python kiwoom_trader.py [status|buy|sell|daily]`

**안전장치**: `KIWOOM_ENV=prod` 면 주문이 차단됨 (조회만 가능). 실전 전환은 모의
검증 수개월 후 별도 결정. 주문유형은 시간외단일가(62) — 모의서버가 거부하면
로그 확인 후 `kiwoom_trader.py` 상단 `ORDER_TYPE_BUY="00"`(지정가)로 바꿔 장중 테스트.

## 1.5b KIS 모의투자 자동매매 — 안D 포트폴리오 (2026-06-18 전면 개편)

`kis_live_signal.py` + `kis_trader.py` — 한국투자증권 REST API 기반 완전 자동화.  
키움(안C)과 **전혀 다른 전략**으로 두 증권사를 독립적으로 비교 운용한다.

### 안D 포트폴리오 구성

| 슬롯 | 전략 | 진입 조건 | 보유 | Stop-loss |
|---|---|---|---|---|
| 1~4 | `h52w_for3d_mkt` | 52주 신고가 돌파 + 외국인 3일 연속 순매수 + 시장강세(MA60>0) | 20일 | **-15%** |
| 5~8 | `for_high20_mkt` | 20일 신고가 돌파 + 외국인 3일 연속 순매수 + 시장강세(MA60>0) | 20일 | **-10%** |
| 9~10 | `gc_for3d` | 골든크로스(MA20>MA60) 발생 + 외국인 3일 연속 순매수 | 15일 | **-26%** |

> 세 전략 모두 **stop-only 오버레이**에서만 백테스트 delta 양수 확인됨.  
> 익절(target) 추가 시 delta 전부 음수 → 만기까지 보유하되 손절만 설정.

### 자동화 스케줄 (register_tasks.ps1 등록 완료)

| 시각 | 작업 | 내용 |
|---|---|---|
| 18:31 (평일) | StockAI\KisSignal | `kis_live_signal.py` → `kis_paper_signals.csv` |
| 09:01 (평일) + **부팅 시** | StockAI\KisTrader | `kis_trader.py daily` → stop 체크 → 매도 → 매수 |

부팅 시 자동 실행: 장 시작 후 PC를 켜도 9시 이후면 당일 매매 자동 처리됨.  
주말은 bat 파일 내부에서 자동 스킵.

### 신호·주문 파일 분리 (키움과 충돌 없음)

| 항목 | 키움(안C) | KIS(안D) |
|---|---|---|
| 신호 파일 | `paper_signals.csv` | `kis_paper_signals.csv` |
| 주문 로그 | `db/kiwoom/orders_*.csv` | `db/kiwoom/kis_orders_*.csv` |
| 진입가 추적 | 없음 | `db/kis/kis_positions.csv` |

### 최초 설정 (1회)

`.env` 에 추가:
```env
# 한국투자증권 모의투자 (openapivts)
KIS_MOCK_APP_KEY=모의용_앱키
KIS_MOCK_APP_SECRET=모의용_시크릿
KIS_MOCK_ACCOUNT=50186907   # 8자리 계좌번호
```

자동화 등록 (관리자 PowerShell):
```
C:\fin\outputs\register_tasks.ps1
```

### 수동 명령

```
python kis_live_signal.py          # 신호 수동 감지
python kis_trader.py status        # 예수금·잔고·슬롯 현황
python kis_trader.py daily         # 매도(만기/stop) → 매수 한번에
```

⚠️ **안전장치**: 모의투자 서버(`openapivts.koreainvestment.com:29443`)만 사용. 실전 계좌 접근 불가.

---

## 1.5c 전략 백테스트 워크플로 & 결과

### 실행 순서

**STEP 1 — 기준선 백테스트 (strategy_engine.py)**

전략 추가·변경 시마다 실행. 기간은 매번 명시.

```
python strategy_engine.py --start 20190101 --end 20251231
```
→ `results/strategy_compare_날짜.csv` 저장. 대시보드 백테스트데이터 탭 상단 표에 자동 반영.

**STEP 2 — 손익 오버레이 테스트 (profit_target_test.py)**

첫 실행은 `--no-cache` (기준선 캐시 생성, 10~30분). 이후 조건 변경 시 수십 초.

```
:: 첫 실행 — 캐시 생성
python profit_target_test.py --start 20190101 --end 20251231 --target 0 --stop 0 --no-cache

:: 손절 단독
python profit_target_test.py --start 20190101 --end 20251231 --stop -8

:: 수익실현+손절 조합 (빠름)
python profit_target_test.py --start 20190101 --end 20251231 --target 5 --stop -8
python profit_target_test.py --start 20190101 --end 20251231 --target 7 --stop -8
python profit_target_test.py --start 20190101 --end 20251231 --target 10 --stop -8
```
→ `results/exit_compare_기간_조건.csv`. 대시보드 백테스트데이터 탭 하단 표에 자동 반영.

> **기간 변경 시 `--no-cache` 필요** (캐시 파일명이 날짜를 포함하므로 자동으로 미스)

### 결과 해석 기준

| 지표 | 의미 | 기준 |
|---|---|---|
| `ev_10slot` | 10슬롯 연간 기대수익%p | 높을수록 |
| `win_rate` | 승률 | 50% 이상 |
| `mdd_roll` | 10연속 MDD | -20% 이하면 위험 |
| `sharpe` | 위험조정수익 | 0.5 이상 |
| `delta_avg` | 오버레이 적용 후 평균% 변화 | + 면 개선, − 면 손해 |

### 이전 실험 결과 요약 (3전략 기준, 2026-06-17)

핵심 3전략(`high_52w_filt`, `for_high20_mkt`, `rsi_reversal`)에 익절 오버레이 적용 시 모두 delta_avg 음수 → **만기 보유가 최적**.  
`high_52w_filt` 수익 분포가 오른쪽 꼬리 비대칭 구조 — 7~10%에서 자르면 +30~50% 구간 포기.  
손절 오버레이는 MDD 개선 효과 확인 필요 — 전략별로 다를 수 있음.

---

## 1.6 두 시스템 구조 — 역할 분리

```
C:\fin\outputs\ (천억이 — 마스터)        C:\fin\Stock_AI_Project\ (보조 — 데이터 전용)
┌──────────────────────────────┐         ┌──────────────────────────────┐
│ 전략 / 신호 / 매매            │         │ 데이터 수집만 (매매 없음)       │
│  live_signal.py              │         │  main_collector.py           │
│  kiwoom_trader.py  ◄─ 매매   │         │    └ KOSPI/KOSDAQ/S&P500     │
│  paper_tracker.py            │         │  supply_demand.py (KIS 수급) │
│  ai_trainer_v4.py            │         │  kiwoom_extra.py (신용/대차)  │
│                              │ ──────► │  news.py (감성분석)          │
│ pykrx_collector.py           │ stock.db│  macro.py (지표)             │
│  └ 16:05 수집 후 stock.db    │ 공유     │                              │
│    자동 동기 (UPSERT)         │ ◄─────  │  scheduler.py 실행 시각:     │
│                              │         │   06:30 OHLCV (우리 뒤)      │
│ make_trades_history_v3.py    │         │   10:05 KIS 수급             │
│  └ stock.db 피처 자동 흡수    │         │   토08:00 키움 신용/대차      │
│                              │         │   일07:00 모델 재학습         │
└──────────────────────────────┘         └──────────────────────────────┘
```

**핵심 원칙**:
- 키움 API 매매: 오직 `kiwoom_trader.py` (천억이) 만 실행 — Stock_AI_Project는 키움 데이터 수집만
- Stock_AI_Project의 `auto_trader.py` / `rule_trader.py` / `finrl_trader.py` 는 비활성 (데이터 수집 스케줄에 미포함)
- 두 시스템이 동시에 키움 API 접근 시 **파일락** (`data/.kiwoom.lock`) 으로 충돌 방지 — 60초 대기 후 stale lock 자동 해제
- stock.db는 16:05 천억이 pykrx 수집 → Stock_AI_Project 06:30 OHLCV가 이미 채워진 날짜 자동 스킵

**Stock_AI_Project 스케줄 확인**: `C:\fin\Stock_AI_Project\scheduler.py` 의 `schedule.every()` 블록

## 1.7 증권사 API — 현재 사용 + 보강 옵션

**현재 사용 (무료)**:

| 소스 | 용도 |
|---|---|
| 한국투자증권 KIS Open API | 분봉·랭킹·지수·신용잔고(probe), **모의투자 주문** (`kis_trader.py`) |
| pykrx (KRX 정보데이터시스템) | 일봉·수급·공매도 — 시점 기준이라 상폐 포함 (백테스트의 생명줄) |
| DART OpenAPI | 공시 + 재무제표 (dart_fundamentals) |
| yfinance | 미국 지수·VIX·환율 (us_market / macro_collector) |

**보강이 필요해지면 (계좌 개설 + 앱키 발급, 무료)**:

- **키움증권 REST API** — 종목별 신용잔고·프로그램매매·투자자별 상세 등 수급 데이터 제공 범위가 넓음. KIS 신용잔고 probe 가 끝내 실패하면 1순위 대안.
- **LS증권(구 이베스트) xingAPI/REST** — 신용잔고 추이, 테마/업종 수급 등 전통적으로 데이터 항목이 가장 다양. 단 구형 xingAPI 는 Windows COM 기반.
- 대신증권 크레온(CYBOS)은 데이터는 풍부하나 32bit 전용이라 비권장.
- 연동 방법: 해당 증권사 앱키를 .env 에 추가하고 `credit_collector.py` 의 probe 패턴(후보 엔드포인트 자동 검증)을 복제해 붙이면 됨.

---

# 2부 — 돌아가는 원리 & 파일 상세

## 시스템 한눈에

```
데이터 수집                전략 평가                실전 운용
┌─────────────┐          ┌─────────────┐         ┌─────────────┐
│ KIS API     │          │ strategies/ │         │ live_signal │
│   ranking   │ ──────► │   14종 등록  │ ──────►│   매일 15:50 │
│   분봉      │          │             │         │             │
│ pykrx       │          │ engine.py   │         │ paper_      │
│   3년 일봉  │          │   비교 표   │         │   tracker   │
│ KRX,DART    │          │ capital_sim │         │             │
│             │          │ walkforward │         │ → 진짜 시장 │
└─────────────┘          └─────────────┘         └─────────────┘
```

---

## 1. 자주 쓰는 명령어

### ⭐ 메인 운영 (매일)
| 목적 | 명령어 |
|---|---|
| **오늘 메인 전략 신호** | `python live_signal.py` |
| **paper trading 상태 + 누적 손익** | `python paper_tracker.py` |
| 6/5 같이 신호 0건 시 진단 | `python debug_signal.py` |

### 전략 평가 (가끔)
| 목적 | 명령어 |
|---|---|
| **47전략 기준선 백테스트** | **`python strategy_engine.py --start 20190101 --end 20251231`** |
| 손익 오버레이 테스트 (첫 실행) | `python profit_target_test.py --start 20190101 --end 20251231 --target 7 --stop -8 --no-cache` |
| 손익 오버레이 조건 변경 (빠름) | `python profit_target_test.py --start 20190101 --end 20251231 --target 5 --stop -8` |
| Walk-forward 견고성 (3 split) | `python walkforward.py` |
| paper_signals.csv 초기 시드 | `python seed_paper_signals.py` |

> **주의**: `strategy_engine.py`는 기간 인수를 매번 명시해야 합니다 (설정 저장 안 됨).  
> `profit_target_test.py`는 `--no-cache` 첫 실행 후 캐시 재사용 → 이후 조건 변경 시 수십 초.  
> 결과: `results/strategy_compare_날짜.csv` (기준선) / `results/exit_compare_날짜_조건.csv` (오버레이)

### 데이터 수집 (자동화됨, 수동은 가끔)
| 목적 | 명령어 |
|---|---|
| KIS 데이터 (단타 별건) | `python data_collector.py today` |
| KRX 공매도 | `python krx_collector.py both` |
| DART 공시 | `python dart_collector.py today` |
| pykrx 일봉 (메인 전략 데이터) | `python pykrx_collector.py` |

### 옛 백테스트 (별건 유지)
| 목적 | 명령어 |
|---|---|
| 단타 v2 백테스트 | `python backtest.py` |
| 오버나잇 종가베팅 | `python backtest_swing.py` |
| 매크로 3전략 벡터화 | `python pykrx_backtester.py` |

### 시스템
| 목적 | 명령어 |
|---|---|
| 자동화 상태 확인 | `Get-ScheduledTask -TaskName "KIS_*" \| Get-ScheduledTaskInfo \| Select TaskName, NextRunTime, LastRunTime, LastTaskResult` |

모든 명령은 먼저 `cd C:\fin\outputs` + `.venv\Scripts\Activate.ps1` (또는 `.venv\Scripts\activate`) 후 실행. 그러면 `python` 명령이 venv 의 Python 을 가리켜 pykrx 등 프로젝트 패키지를 정상 인식.

`py -3.11` 사용도 가능하지만 시스템 Python 을 띄우기 때문에 venv 패키지 못 보임. **venv 활성화 후 `python` 사용 권장.**

---

## 1.5 메인 전략 — high_500d_h40_MKT

### 룰
| 항목 | 값 |
|---|---|
| 진입 | 종가가 직전 500 영업일 신고가 돌파 |
| 시장 게이트 | 시장 평균 등락률의 60일 MA > 0 (강세장만) |
| 유동성 | 거래대금 ≥ 30억 (신호일) — 2026-06-11 백테스트와 통일 |
| 진입가 | 신호 다음 영업일 시가 |
| 청산 | 진입 후 40 영업일 종가 |
| 자본 운용 | 최대 10종목 동시보유 (max_concurrent), 자본 1/N 분배 |

### 실측 성과 (3년치 macro_data, 단방향 -30% 컷오프 적용)

> ⚠️ **구버전 측정 기준** (컷오프 + 원가평가) — 현재 공식 수치는 최상단 요약 및 3부 업데이트 내역 참조 (CAGR +139.9%, MDD -42.6%, Sharpe 1.34).

| 지표 | 값 |
|---|---|
| n_trades | 1,411 (단방향 -30% 컷오프 후) |
| 승률 | 49.28% |
| CAGR | **+156.27%/년** |
| Real MDD | **-8.03%** |
| Real Sharpe | **+3.01** |
| 1천만원 → 3년 후 | 약 2,752만원 |

### 액면분할/병합 방어 — 단방향 컷오프 정책

pykrx 의 raw OHLCV 는 수정주가 미반영 → 액면분할 시 가짜 -80% 폭락 발생.
**음수 한쪽만 -30% 컷오프** (양수는 진짜 익절이므로 보존):
- 컷오프 186건 (~13%) = 액면분할/병합 의심 매매 제외
- 양방향 ±30% 는 진짜 익절 359건도 잘라 결과 왜곡 (검증됨)
- 진짜 정답 = pykrx adjusted 미지원 → FinanceDataReader 같은 외부 라이브러리 별건 작업

### Walk-forward Multi-split 견고성

| Split | OOS CAGR |
|---|---|
| 50/50 | +77.67% |
| 67/33 | +35.40% |
| 75/25 | +44.50% |
| **모든 split 에서 OOS 양수** | ⭐ 가장 견고한 단일 전략 |

### 폐기/대안 전략
- **단타 박스권 룰 v2**: profit_factor 0.55, 비용 0.33% 못 이김 → 폐기 (자동 백테스트만 유지)
- **portfolio_v1~v4**: 다각화가 max_concurrent=10 슬롯 병목으로 단일보다 약함
- 종가베팅/모멘텀/RSI/갭매매 등 11개: OOS 검증 못 통과

---

## 1.6 분석 4단계 — 진실에 도달한 과정

| 단계 | 결과 | 진실? |
|---|---|---|
| 1. 매매당 평균 | high_500d_h40 (pf 2.04) 우위 | ❌ 자본 회전 무시 |
| 2. 자본 시뮬 | high_500d_h40 (+129%) 우위 | ❌ train 운 포함 |
| 3. 단일 walk-forward (67/33) | portfolio_trend3, h500_40 폐기 | ❌ 단일 분할 운 |
| 4. **Multi-split walk-forward** | **high_500d_h40_MKT 단일 확정** | ⭐ **진짜 답** |

**교훈**: 4단계 검증 거쳐야 진실. 매매당 평균만 보고 결정 X. 시장 게이트 (MKT 필터) 가 진짜 가치.

---

## 1.7 Paper Trading 사용법

### 매일 운영 (자동)
- **KIS_Paper 작업** 매일 15:50 자동 실행
- `live_signal.py` → 오늘 신호 종목 출력 + `paper_signals.csv` 추가
- `paper_tracker.py` → 누적 매매 + 현재 보유 + 자본 곡선

### 수동 점검 (필요 시)
```powershell
python live_signal.py       # 오늘 신호 (시장 약세면 0건이 정상)
python paper_tracker.py     # 누적 손익 + 보유 포지션
python debug_signal.py      # 신호 0건일 때 진단
```

### 실전 진행 단계 (계획)
1. **지금~1개월**: 자동 paper trading 누적 (KIS_Paper 16:30)
2. **1개월 후**: 실제 누적 결과 점검. 백테스트 예상치와 비교.
3. **2~3개월 누적 → 검증 통과 시**: 소액 (50~100만원) 실전 진입
4. **6개월 안정 운용 후**: 자본 증액 단계

### Paper 데이터 흐름
```
macro_data/daily/*.csv ──► live_signal.py ──► paper_signals.csv
                                                  │
                                                  ▼
                              paper_tracker.py ──► 자본 곡선 + 손익
```

---

## 2. .py 파일별 용도와 실행법

### config.py
**용도**: 룰 v2 파라미터, KIS 인증, 경로 설정 일괄 관리. 다른 모듈이 다 import 해서 씀.

**직접 실행**: `py -3.11 config.py` → 현재 설정 요약 출력 (디버그용)

**주요 토글**:
- `KIS_ENV` — "prod"(실전) / "vps"(모의)
- `TOP_N_STOCKS = 50` — 거래대금 raw 상위 N개 (ETF 포함)
- `ENABLE_FOREIGN_FILTER` — True 면 백테스트 시 D-1 외인 순매수>0 종목만 진입
- `ENABLE_ETF_FILTER` — True 면 백테스트 시 ETF/우선주 제외
- `RULE_V2` dict — 박스 폭, 익절/손절, 시간청산, 비용 가정 등

룰 바꾸려면 이 파일만 수정.

---

### data_collector.py
**용도**: KIS API → CSV 데이터 수집 메인.

**명령어**:
```
py -3.11 data_collector.py ranking            # 거래대금 raw 상위 50 (KOSPI+KOSDAQ 머지)
py -3.11 data_collector.py minutes YYYYMMDD   # 특정 날짜 분봉 (당일치만 가능)
py -3.11 data_collector.py investor [date]    # 외인/기관/개인 매매 데이터
py -3.11 data_collector.py daily [date]       # 종목별 일봉 60일치
python data_collector.py index [date]         # KOSPI/KOSDAQ 지수 snapshot (15컬럼)
python data_collector.py index_minutes [date]  # KOSPI/KOSDAQ 지수 당일 분봉 (EOD 1회)
py -3.11 data_collector.py today              # 위 모두 + 장 마감 후 자동 분기
```

**자동 호출**: `run_collector.bat` 가 `today` 모드로 호출.

`today` 모드 분기:
- 15:30 이전: `ranking` + `index_snapshot` 만 누적
- 15:30 이후: `ranking` + `index_snapshot` + `minutes` + `investor` + `daily` + `index_minutes`

---

### data_loader.py
**용도**: 저장된 CSV → pandas DataFrame 변환. backtest/detector 가 사용.

**직접 실행**: `py -3.11 data_loader.py` → 최신 날짜의 ranking/분봉 샘플 출력 (디버그)

직접 호출할 일은 거의 없음.

---

### detector.py
**용도**: 박스권 + 거래량 급증 신호 감지. simulator 가 사용.

**직접 실행**: `py -3.11 detector.py` → 테스트 데이터로 감지 동작 확인 (디버그)

룩어헤드 편향 방지를 위해 `shift(1)` 적용된 상태. `is_box`=True 는 "직전 5분 박스 형성됨" 을 의미.

---

### simulator.py
**용도**: 가상 매매 시뮬레이터. backtest 가 사용.

**직접 실행**: `py -3.11 simulator.py [YYYYMMDD] [MODE]`
- MODE: A (박스 매수), B (거래량 돌파), AB (둘 다)
- 예: `py -3.11 simulator.py 20260604 AB`

Mode B 게이트: `close > box_high` (진짜 돌파만).

---

### analyzer.py
**용도**: 매매 리스트 → 승률/손익비/MDD 등 통계 계산. backtest 가 사용.

직접 실행 안 함.

---

### backtest.py
**용도**: 백테스트 메인 엔트리포인트. 인자 없으면 전체 누적 날짜 × 모드 A/B/AB 다 비교.

**명령어**:
```
py -3.11 backtest.py                    # 전체 / 전체 모드
py -3.11 backtest.py 20260604           # 특정 날짜
py -3.11 backtest.py AB                 # 특정 모드
py -3.11 backtest.py 20260604 AB        # 둘 다
```

**결과**:
- 콘솔: 모드별 비교 리포트 (n_trades, win_rate, avg_net_pct, profit_factor, cum_pct, mdd_pct)
- 파일: `results/trades_MODE_시작-끝.csv`, `results/daily_MODE_시작-끝.csv`

**자동 호출**: `run_backtest.bat` 가 매일 16:00 호출.

---

### live_signal.py *(MAIN)*
**용도**: 메인 전략 high_500d_h40_MKT 신호 감지. 매일 실행.

**명령어**: `python live_signal.py`

**기능**:
- macro_data/daily/ 최신 영업일 데이터로 500일 신고가 돌파 검사
- 시장 강세 게이트 (60일 MA > 0) 확인
- 거래대금 10억 이상 필터
- ETF/우선주 제외
- 신호 종목 → 콘솔 출력 + `paper_signals.csv` 누적 (멱등)
- **factor_scorer 연동 (scipy 설치 시)**: 신호 종목별 팩터 점수 0-10점 자동 출력.
  IC 기반 점수 + 상위 기여 팩터 목록 포함. 매매 결정과 무관(참고용).

**자동 호출**: `run_paper.bat` 가 매일 15:50 호출 (KIS_Paper 작업).
또한 **StockAI\LiveSignal** 작업이 평일 18:30 `run_live_signal.bat` 으로 별도 실행 → `C:\fin\logs\live_signal_*.log`.

### factor_scorer.py *(NEW)*
**용도**: 신호 종목별 멀티팩터 점수(0-10) 계산. live_signal.py 가 자동으로 import해 사용.
수동으로 실행하면 현재 IC 가중치 및 피처 분포 진단 출력.

**명령어**: `python factor_scorer.py`

**전제**: `pip install scipy` (.venv 활성화 후) — IC 계산에 필요.

**점수 산출 방식**:
- Method A (IC 점수): 각 피처의 과거 수익률과의 Spearman 상관계수(IC) 가중 합산.
  현재 피처값의 과거 분포 내 백분위 → 0~10점 (5=중간값).
- Method B (SHAP): XGBoost 모델의 SHAP 기반 상위 기여 피처 요약 (모델 있을 때만).

**핵심 피처 (IC 상위)**:
- `prm_net_5d_ratio` 프로그램 5일 순매수 +0.142 (가장 강한 신호)
- `atr_pct` 변동성 -0.132 (낮을수록 좋음)
- `vix` VIX 공포지수 +0.065 (역발상: 공포장 신고가 = 진짜 강도)

**주의**: 점수는 참고용 — AI와 마찬가지로 매매 결정에 자동 반영 안 됨 (USE_AI=False).

---

### paper_tracker.py *(MAIN)*
**용도**: paper_signals.csv 의 모든 신호로 가상 매매 수행. 누적 손익 + 보유 포지션 출력.

**명령어**: `python paper_tracker.py`

**자동 호출**: `run_paper.bat` 가 live_signal 후 호출.

### debug_signal.py
**용도**: live_signal.py 가 0건일 때 진단. 시장 게이트 / 신고가 / 거래대금 단계별 종목 수.

**명령어**: `python debug_signal.py`

### seed_paper_signals.py
**용도**: paper_signals.csv 를 과거 3년치 백테스트 신호로 시드. 즉시 의미 있는 paper_tracker 결과 확인.

**명령어**: `python seed_paper_signals.py` (y 입력)

### strategy_engine.py
**용도**: 14개 전략 일괄 백테스트 + 자본 시뮬 비교.

**명령어**: `python strategy_engine.py`

### walkforward.py
**용도**: 3개 split (50/50, 67/33, 75/25) walk-forward 견고성 검증.

**명령어**: `python walkforward.py`

### capital_simulator.py
**용도**: max_concurrent 슬롯 cap 적용 자본 시뮬. CAGR / 진짜 MDD / Sharpe.

직접 호출 안 함 — 다른 모듈이 import.

### strategies/ 폴더 *(NEW)*
14개 전략 모듈:
- `base.py` — BaseStrategy + StrategyTrade
- `daily_loader.py` — macro_data 통합 로더
- `_swing_base.py` — 진입 lag + 보유 청산 공통 헬퍼
- `gap_buy.py` — 갭매매 (#4)
- `momentum_5d.py` — 5일 모멘텀 (#8)
- `breakout_5d.py` — 5일 신고가 (#9)
- `rsi_reversal.py` — RSI 과매도 (#10)
- `high_52w.py` — 52주 신고가 (#11) **베이스**
- `volume_surge.py` — 거래량 급증 (#12)
- `high_with_filters.py` — 신고가 + 시장 게이트 + 거래량 (high_500d_h40_MKT) ⭐ **메인**
- `portfolio.py` — 다중 전략 결합

### backfill_history.py
**용도**: 과거 분봉/ranking 백필. **FHKST03010230 endpoint 발견 후 1년치 과거 분봉 백필 가능** (실전 키 필수).

**전제** — 실전 KIS 키 + .env 에 `KIS_ENV=prod` + `KIS_PROD_*` 변수 설정. 모의(vps) 환경에선 작동 안 함 (KIS 모의투자 미지원 endpoint).

**명령어**:
```
python backfill_history.py --dry-run    # 계획만 출력
python backfill_history.py              # 실행 (1년치 가능)
python backfill_history.py --days 14    # 윈도우 축소
```

백필 흐름:
1. db/daily 기반 과거 영업일별 raw 50 ranking 합성/재사용
2. 각 날짜의 종목별 분봉 수집 (FHKST03010230, 한 호출 120건)
3. 멱등 — 이미 있는 분봉 skip

---

### kis_api.py
**용도**: KIS API HTTP 클라이언트. 토큰 관리, 재시도, timeout.

직접 실행 안 함. 다른 모듈이 함수 import.

주요 함수:
- `get_volume_ranking(market, by)` — 거래대금/거래량 상위 (KOSPI/KOSDAQ 각각 호출)
- `get_minute_chart(stock_code, target_time)` — 종목 당일 분봉 (FHKST03010200, vps OK)
- `get_full_day_minutes(code, date)` — 당일치 분봉 누적
- **`get_minute_chart_historical(stock_code, date, target_time)` — 종목 과거 분봉 (FHKST03010230, prod 키 필수)**
- **`get_full_day_historical_minutes(code, date)` — 과거 특정일 하루치 분봉 누적 (시각 역순 4번 호출)**
- `get_daily_chart(code, period_days)` — 일봉
- `get_foreign_institution_trading(market)` — 외인/기관
- `get_stock_investor(code, target_date)` — 종목별 투자자
- `get_index_current(market)` — KOSPI/KOSDAQ 지수 현재값 + breadth (OHLC + 상승/하락 종목 수)
- `get_index_minute_chart(market, target_time)` — 지수 분봉 (vps 미지원, 일봉만 반환)

---

### krx_collector.py *(NEW)*
**용도**: pykrx 로 KRX 정보데이터시스템에서 공매도 데이터 수집. T-1 기준.

**전제**:
- `pip install pykrx` (venv 활성화 후)
- `.env` 에 KRX 로그인:
```
KRX_ID=hong****
KRX_PW=********
```
KRX 정보데이터시스템 (data.krx.co.kr) 무료 가입 후 ID/PW 사용.

**현재 제약 (pykrx 1.2.8 기준)**:
- **신용잔고 미지원** — pykrx 가 해당 함수를 제공 안 함. credit 명령은 안내만 출력 후 종료.
- **공매도 잔고 fallback 체인** — `get_shorting_balance_by_ticker` 가 KRX 컬럼 변경으로 깨진 상태 → `volume_by_ticker` → `value_by_ticker` → `balance_top50` 순으로 시도. 저장 파일 마지막 컬럼 `__source` 에 어떤 함수에서 받았는지 기록.

**명령어**:
```
python krx_collector.py short             # 직전 영업일 공매도
python krx_collector.py short 20260603    # 특정 날짜
python krx_collector.py both              # credit 안내 + short 실행
python krx_collector.py credit            # 안내만 (현재 미지원)
```

저장: `db/short/YYYY-MM/YYYYMMDD.csv`
실측 컬럼 예시 (`source=volume_by_ticker` 일 때):
```
티커, 공매도, 매수, 비중, __source
```
- 공매도: 공매도 거래량
- 매수: 일반 매수 거래량
- 비중: 공매도/총 거래의 % (해당 일 기준)
- `__source`: 어떤 fallback 함수에서 받았는지 (분석 시 동질성 체크)

(`db/credit/` 은 폴더만 생성되고 파일 안 들어옴 — pykrx 한계)

**자동 호출**: `run_krx.bat` 가 매일 평일 08:30 호출 (KIS_KRX 작업).

---

### dart_collector.py *(NEW)*
**용도**: DART (전자공시시스템) 일자별 공시 목록 수집.

**전제**: `.env` 에 `DART_API_KEY=...` 등록 (https://opendart.fss.or.kr 가입 후 발급).

**명령어**:
```
py -3.11 dart_collector.py today              # 오늘 공시
py -3.11 dart_collector.py date 20260604      # 특정 날짜
py -3.11 dart_collector.py corp 005930 20260601 20260604  # 특정 종목 기간
```

저장: `db/dart/YYYY-MM/YYYYMMDD.csv`

**자동 호출**: `run_dart.bat` 가 매일 평일 19:00 호출 (KIS_DART 작업).

---

### nxt_probe.py *(NEW)*
**용도**: NXT(넥스트레이드) 시간외 분봉 KIS API 지원 가능성 검증.

**직접 실행**: `py -3.11 nxt_probe.py`

market_div (J/NX/UN) 별로 17:00 시각 분봉을 시도해서 vps 환경 NXT 지원 여부 확인. 결과에 따라 data_collector 통합 여부 결정.

---

### kis_websocket.py / tick_collector.py
**용도**: WebSocket 실시간 체결 수신 + SQLite 적재.

**전제**: KIS 발급 별도 Approval Key 필요. `pip install websockets` 필요.

실시간 트레이딩 인프라 구축용 별건 모듈. 백테스트 흐름과는 독립.

### run_tick_collector.bat *(NEW)*
**용도**: `tick_collector.py` 를 장중 자동 실행, 15:30 종료. 끊김 시 재시작 루프.

---

## 스윙 전략 (별건, 일봉 기반)

단타(룰 v2)의 비용 한계 회피를 위해 일봉 기반 스윙 전략 인프라 별도 구축.

### pykrx_collector.py *(NEW)*
**용도**: pykrx 로 3년치 일봉 + 수급 데이터 수집 → `macro_data/daily/` 적재.

**전제**: `pip install pykrx`, KRX_ID/PW 등록 (krx_collector 와 동일).

**명령어**:
```
python pykrx_collector.py
```

### pykrx_backtester.py *(NEW)*
**용도**: 3년치 일봉 데이터로 매크로 전략 3개 동시 벡터화 백테스트. 단타 simulator 와 독립.

**명령어**:
```
python pykrx_backtester.py
```

### backtest_swing.py *(NEW)*
**용도**: 오버나잇 종가베팅 백테스트.
- 진입: 15:20, MA5 위 + 당일 외인 순매수 양수
- 청산: 익일 09:05 시가 (갭 수익 확정)

분봉 데이터 활용 (data_loader 와 같은 분봉 사용). analyzer/Trade 데이터클래스 재사용.

**명령어**:
```
python backtest_swing.py
```

전제: `data/rankings/`, `db/minute/`, `db/investor/` 모두 채워져 있어야 함 (분봉 시점 + 외인 데이터 필요).

---

### monthly_xlsx_builder.py
**용도**: 월말 데이터 xlsx 합본 빌드.

**자동 호출**: 매월 1일 02:00 (KIS_Monthly 작업)

---

### diag_env.py
**용도**: 환경 진단 (Python 버전, .env, KIS 인증 상태 등 체크).

**직접 실행**: `py -3.11 diag_env.py` → 환경 문제 점검할 때.

---

## 3. .bat / .ps1 파일

### run_collector.bat
- `data_collector.py today` 호출 + `logs/collect_YYYYMMDD.log` 기록
- 주말 자동 스킵, 로그 30일 자동 삭제
- 자동 호출: KIS_Ranking + KIS_EOD
- Python 우선순위: `.venv\Scripts\python.exe` → `py -3.11` → `python` (venv 가 있으면 무조건 우선 사용)

### run_backtest.bat
- `backtest.py` 호출 + `logs/backtest_YYYYMMDD.log` 기록
- 자동 호출: KIS_Backtest

### run_krx.bat *(NEW)*
- `krx_collector.py both` 호출 + `logs/krx_YYYYMMDD.log`
- 자동 호출: KIS_KRX

### run_dart.bat *(NEW)*
- `dart_collector.py today` 호출 + `logs/dart_YYYYMMDD.log`
- 자동 호출: KIS_DART

### install_scheduler.ps1
- Windows 작업 스케줄러에 **KIS_* 작업 6개** 등록 (Ranking/EOD/Backtest/KRX/DART/Monthly)
- 재실행하면 깔끔하게 재등록 (idempotent)
- 실행: `powershell -ExecutionPolicy Bypass -File .\install_scheduler.ps1`

### register_tasks.bat + register_tasks.ps1 *(NEW)*
- **StockAI\* 작업 3개** 등록: Scheduler(부팅시) / LiveSignal(평일 18:30) / PaperAudit(일요일 09:00)
- **최초 1회만 실행** — 이후 자동 관리됨. 재등록도 idempotent(덮어씌움).
- 실행: `register_tasks.bat` 우클릭 → 관리자 권한으로 실행
- register_tasks.bat은 관리자 권한 확인 후 register_tasks.ps1을 호출.

### start_scheduler.bat *(NEW)*
- 부팅 시 `StockAI\Scheduler` 작업이 자동 호출 — Stock_AI_Project의 scheduler.py 실행.
- 이미 실행 중이면(CIM 프로세스 쿼리) 스킵해 중복 실행 방지.
- 로그: `C:\fin\logs\scheduler.log`

### run_live_signal.bat *(NEW)*
- 매일 평일 18:30 `StockAI\LiveSignal` 작업이 호출.
- live_signal.py 실행 (팩터 점수 포함), 날짜 스탬프 로그 → `C:\fin\logs\live_signal_YYYYMMDD_HHmm.log`

### run_paper_audit.bat *(NEW)*
- 매주 일요일 09:00 `StockAI\PaperAudit` 작업이 호출.
- paper_audit.py 실행, 결과 → `C:\fin\logs\paper_audit_YYYYMMDD_HHmm.log` + `paper_audit_result.csv`

### register_task.bat / unregister_task.bat
- 옛 자동화 등록/해제 스크립트 (현재는 install_scheduler.ps1 + register_tasks.bat 사용)

### start_monitor.bat / monitor.ps1
- 모니터링 스크립트 (별건)

---

## 4. 자동화 작업 스케줄러 흐름

두 그룹의 작업이 독립 등록됨:

### KIS_* 작업 (install_scheduler.ps1 으로 등록)

| 시각 | 작업 이름 | 호출 |
|---|---|---|
| 매일 평일 08:30 | KIS_KRX | run_krx.bat → krx_collector.py both (T-1 공매도) |
| 매일 09:00~14:30 (30분 간격) | KIS_Ranking | run_collector.bat → data_collector.py today (분봉 ranking + 지수 snapshot) |
| 매일 15:40 | KIS_EOD | run_collector.bat → data_collector.py today (장 마감 종합) |
| 매일 16:00 | KIS_Backtest | run_backtest.bat → backtest.py (단타 v2 별건) |
| **매일 15:50** | **KIS_Paper** ⭐ | **run_paper.bat → live_signal + paper_tracker (메인)** |
| 매일 평일 19:00 | KIS_DART | run_dart.bat → dart_collector.py today |
| 매월 1일 02:00 | KIS_Monthly | monthly_xlsx_builder.py |

### StockAI\* 작업 (register_tasks.bat 으로 1회 등록)

| 트리거 | 작업 이름 | 호출 | 로그 |
|---|---|---|---|
| 로그인 시 + 매일 06:10 | StockAI\Scheduler | start_scheduler.bat → Stock_AI_Project/scheduler.py | C:\fin\logs\scheduler.log |
| **평일 18:30** | **StockAI\LiveSignal** | run_live_signal.bat → live_signal.py (팩터 점수 포함) | C:\fin\logs\live_signal_YYYYMMDD_HHmm.log |
| **일요일 09:00** | **StockAI\PaperAudit** | run_paper_audit.bat → paper_audit.py | C:\fin\logs\paper_audit_YYYYMMDD_HHmm.log |

**StartWhenAvailable 설정**: PC가 슬립 중이었다가 깨어나면 놓친 작업 즉시 실행됨 (일주일에 1회 부팅 패턴에 최적화).

PC가 깨어 있고 인터넷 연결되면 매일 자동 실행. 슬립 OFF + 부팅 자동 실행 필수.

---

## 5. 데이터 폴더 구조

```
C:\fin\outputs\
├── .env                         # KIS API 키 + DART_API_KEY (절대 git에 올리지 말 것)
├── .kis_token.json              # KIS 토큰 캐시
├── data/
│   └── rankings/YYYYMMDD.csv    # 시각별 거래대금 상위 50 (raw, ETF 포함)
├── db/
│   ├── minute/YYYY-MM/YYYYMMDD/CODE.csv   # 종목별 분봉
│   ├── investor/YYYY-MM/YYYYMMDD.csv      # 일자별 외인/기관/개인
│   ├── daily/YYYY-MM/YYYYMMDD.csv         # 종목별 60일 일봉
│   ├── index/YYYY-MM/YYYYMMDD.csv         # 지수 30분 snapshot (15컬럼: OHLC + breadth)  (NEW)
│   ├── index/YYYY-MM/YYYYMMDD_minute.csv  # 지수 당일 1분봉 (KOSPI+KOSDAQ)  (NEW)
│   ├── credit/YYYY-MM/YYYYMMDD.csv        # 신용잔고 (T-1)  (NEW)
│   ├── short/YYYY-MM/YYYYMMDD.csv         # 공매도 잔고 (T-1)  (NEW)
│   ├── dart/YYYY-MM/YYYYMMDD.csv          # DART 공시  (NEW)
│   ├── xlsx/                              # 월말 xlsx 합본
│   └── ticks.db                           # 실시간 틱 SQLite (tick_collector)
├── results/
│   ├── trades_MODE_시작-끝.csv             # 매매 한 건 한 건
│   └── daily_MODE_시작-끝.csv              # 일별 손익 요약
└── logs/
    ├── collect_YYYYMMDD.log               # 수집 로그
    ├── backtest_YYYYMMDD.log              # 백테스트 로그
    ├── krx_YYYYMMDD.log                   # KRX 수집 로그  (NEW)
    └── dart_YYYYMMDD.log                  # DART 수집 로그  (NEW)
```

---

## 6. 알려진 제약

1. **종목 분봉** — vps 는 당일치만(`FHKST03010200`), prod 는 1년치 과거 백필 가능(`FHKST03010230`). 평소 vps 로 매일 누적 + 백필 필요할 때 prod 로 일시 전환.
2. **호가/체결강도 데이터 없음** — KIS 무료 API 한계.
3. **KIS vps(모의) 환경 일부 endpoint 제약** — 실전 키와 동작 다를 수 있음.
4. **지수 분봉** — endpoint `inquire-index-tickprice` + TR_ID `FHKUP03500200` + `FID_PW_DATA_INCU_YN=Y` 조합으로 vps 정상 동작 확인. 1차 probe 에서 빠진 필드 때문에 잘못 진단했었음. EOD 1회 누적 (종목 분봉처럼 30분 단위 역순 호출). 추가로 snapshot(30분 간격) 도 별도 보존 — OHLC + breadth 정보가 분봉엔 없으므로 둘 다 가치 있음.
5. **KIS 토큰 24시간 만료** — 장시간 실행 시 재발급 필요.
6. **PC 슬립/종료 시 자동 수집 중단** — 슬립 OFF 필수, 종료 시 그날 데이터 손실 가능.
7. **KRX T-1 지연** — 신용/공매도 데이터는 항상 전일치만 가능 (한국거래소 정책).
8. **DART 공시는 실시간 아님** — 보통 18시까지 그날 공시 등록 완료. 19:00 수집이 안전.
9. **NXT 시간외 분봉** — KIS vps 지원 미검증. `nxt_probe.py` 결과에 따라.
10. **factor_scorer scipy 의존** — IC 계산에 `scipy` 필요. 미설치 시 live_signal 이 팩터 점수 없이 정상 실행됨 (ImportError graceful fallback). 설치: `.venv\Scripts\activate` → `pip install scipy`.
11. **AI 모델 현황 (2026-06-14)** — AUC 0.499 (7년 데이터 기준). USE_AI=False 유지 중. 사이징 연동은 Q5-Q1 스프레드 +3%p 이상이 수개월 안정 후 별도 논의.

---

## 7. 흔한 문제 해결

| 증상 | 원인 | 해결 |
|---|---|---|
| 자동 수집이 안 됨 | PC 슬립 / 작업 비활성화 / .bat escape 에러 | `Get-ScheduledTaskInfo` 로 LastTaskResult 확인. 255면 .bat escape 문제. |
| `cacert.pem` 에러 | 백신이 venv 파일 격리 | `pip install --force-reinstall certifi` + venv 백신 제외 등록 |
| 새 컬럼 추가 후 sqlite 에러 | 스키마 마이그레이션 누락 | ALTER TABLE ADD COLUMN |
| 백테스트 `수집된 분봉 데이터가 없습니다` | data/rankings 또는 db/minute 비어 있음 | 먼저 `data_collector.py today` 실행 |
| `pykrx 미설치` 에러 | KRX 수집기 첫 실행 | `(.venv)` 활성화 후 `pip install pykrx` |
| `pykrx 미설치` 에러 (venv 에는 설치돼 있는데) | `py -3.11` 이 venv 가 아닌 시스템 Python 호출 | `python krx_collector.py both` (venv 활성) 또는 `.venv\Scripts\python.exe krx_collector.py both`. 자동화 .bat 는 이미 .venv 우선 사용. |
| `module 'pykrx.stock' has no attribute 'get_market_credit_balance_by_ticker'` | pykrx 가 신용잔고 미지원 | 정상 — 신용잔고는 별건. `[credit] 미지원 (skip)` 안내만 출력 후 진행. |
| `get_shorting_balance_by_ticker: None of [Index([...])] are in [columns]` | KRX 가 응답 컬럼명 변경, pykrx 1.2.8 미동기화 | 정상 — 자동으로 다음 fallback (`volume_by_ticker` 등) 시도. 저장 CSV 의 `__source` 컬럼으로 사용된 함수 확인. pykrx 업그레이드 (`pip install -U pykrx`) 후 재시도하면 원래 함수로 복귀 가능. |
| `KRX 로그인 실패: KRX_ID 또는 KRX_PW 환경 변수가 설정되지 않았습니다` (`python -c` 직접 실행 시) | config 안 import → .env 안 로드 | 정상 — `krx_collector.py` 정상 실행 시엔 config 가 먼저 import 되어 .env 로드됨. 확인은 `python -c "import config; import os; print(os.environ.get('KRX_ID'))"` |
| `DART_API_KEY 환경변수 없음` | .env 누락 | https://opendart.fss.or.kr 가입 + .env 등록 |
| 지수 분봉 0건 | `FID_PW_DATA_INCU_YN` 누락 또는 TR_ID 잘못 | 정상 TR_ID = FHKUP03500200, 필수 파라미터 `FID_PW_DATA_INCU_YN=Y` 확인. kis_api.py 의 `get_index_minute_chart` 가 이미 정확한 파라미터로 호출. |
| 지수 snapshot 파일 컬럼 불일치 (옛 7컬럼 + 새 15컬럼) | 코드 업데이트 전후 같은 파일에 append | 해당 날짜 파일 삭제 후 재실행: `Remove-Item db/index/YYYY-MM/YYYYMMDD.csv; python data_collector.py index` |
| KRX 월요일에 빈 결과 | 일요일 데이터 없음 (`_last_business_day` 미적용 옛 버전) | 최신 코드는 자동으로 직전 영업일 사용. 옛 버전이면 코드 업데이트. |

---

## 8. 룰 v2 요약 + 분석 결과

### 룰 파라미터

| 항목 | 값 | 변경 이력 |
|---|---|---|
| 시장 | KOSPI + KOSDAQ | |
| 종목 풀 | 거래대금 raw 상위 50 (ETF/우선주는 백테스트에서 제외) | |
| **박스 시간** | **직전 10분** (현재 분봉 제외) | (기존 5분, 22일치 시간청산 78% 대응) |
| 박스 폭 | ±2.0% (0.8% 미만 제외) | |
| 진입 A | 박스 하단 + 박스폭의 5% | |
| 진입 B | 박스 + 거래량 2배↑ + close > box_high (진짜 돌파만) | |
| **1차 익절** | **+0.8%** 에서 절반 | (기존 0.7%) |
| 트레일링 | 고점 대비 -0.4% | |
| **시간 청산** | **15분** | (기존 5분, 진득 대기) |
| **손절** | **-2.0%** | (기존 -1.8%, 잔파도 털림 방지) |
| 비용 가정 | 수수료 0.015%×2 + 거래세(KOSDAQ 0.20%) + 슬리피지 0.05%×2 = 약 0.43% | |

### 8일치 데이터 분석 결과 (2026-06-04 기준)

| 지표 | 값 |
|---|---|
| 종목-일 수 | 388 |
| 진짜 돌파 표본 | 1,353건 |
| 5분 내 +0.7% 익절 도달률 | 38.8% |
| 5분 내 -1.8% 손절 도달률 | 10.1% |
| 시간청산 (5분내 미도달) | 52.5% |
| 룰 기댓값 (net) | **-0.614%** (비용 0.43% 못 이김) |

**핵심**: TP/SL 튜닝, 시간대 필터, vol_ratio 강화 등 모든 조합이 -EV. **돌파 신호 자체에 통계적 우위 부족**. 신호 추가 또는 룰 본체 재설계 필요. (USAGE.md 9번 섹션 참고)

---

## 9. 다음 단계 — 데이터 축적 + 분석 우선순위

| 데이터 | 상태 | 활용 가능성 |
|---|---|---|
| 시장지수 KOSPI/KOSDAQ | 수집 시작 | 시장 컨텍스트 필터 |
| 신용잔고 (T-1) | 수집 시작 | 변동성 위험 종목 분별 |
| 공매도 잔고 (T-1) | 수집 시작 | Short squeeze 후보 |
| DART 공시 | 수집 시작 (API 키 필요) | 이벤트 종목 제외 필터 |
| NXT 시간외 분봉 | 검증 대기 | nxt_probe.py 결과 후 결정 |
| 호가/체결강도 | 미지원 (유료 필요) | — |

각 데이터 1~2주 누적되면 detector 에 신호로 통합. 통합 패턴은 D-1 외인 필터와 동일 (`config.ENABLE_*_FILTER` 토글로 on/off 비교).


---

# 3부 — 업데이트 내역 (최신순)

## 2026-06-14 (2차) — 멀티팩터 스코어링 + 부팅 자동화 + 전체 파일 감사

### 멀티팩터 스코어링 (factor_scorer.py)
- `factor_scorer.py` 신설 — IC(Spearman 상관계수) 기반 팩터 점수 0-10점 산출.
  핵심 피처 20개: 수급(프로그램/외인/기관), 기술적(ATR/RSI/MACD/BB), 매크로(VIX/SOX/KRW), 모멘텀 등.
- IC 가중치 수식: `total = 5 + Σ(IC_f × (pct_rank_f − 0.5)) / (Σ|IC_f| × 0.5) × 5`
  → 모든 피처 최하위 = 0, 중위값 = 5, 최상위 = 10.
- 상위 기여 팩터 목록 + 방향 출력 (예: "프로그램 순매수 ▲ +0.8pt, VIX ▼ +0.5pt").
- SHAP 경로(Method B): XGBoost 모델 존재 시 모델 확률 + SHAP 상위 팩터 병기.
- `live_signal.py` 에 통합 — 신호 종목마다 바로 뒤에 팩터 리포트 출력. scipy 미설치 시 graceful skip.
- **전제**: `.venv\Scripts\activate` → `pip install scipy`

### StockAI 부팅 자동화 (register_tasks.bat)
- `start_scheduler.bat` — 부팅 시 Stock_AI_Project scheduler.py 자동 기동.
  중복 방지: CIM 쿼리로 이미 실행 중이면 스킵.
- `run_live_signal.bat` — 평일 18:30 live_signal.py 실행 + C:\fin\logs\ 날짜 스탬프 로그.
- `run_paper_audit.bat` — 일요일 09:00 paper_audit.py 실행 + 로그.
- `register_tasks.ps1` + `register_tasks.bat` — 위 3작업 Windows Task Scheduler 등록.
  **1회만 실행** (우클릭 → 관리자 권한으로 실행). StartWhenAvailable 설정으로 슬립 중 놓친 작업 자동 보충.
  등록 결과: StockAI\Scheduler, StockAI\LiveSignal, StockAI\PaperAudit 모두 "Ready" 상태 확인.

### 전체 파일 감사 — 버그 수정 4건
1. **exit_rule_engine.py**: `from us_market_collector import load_latest` → 삭제된 파일 참조.
   인라인 `load_us_market()` 함수로 대체 (macro_data/indicators.csv 에서 직접 읽음).
2. **recollect_guard.py**: 삭제된 us_market_collector.py 를 checklist에서 호출하던 항목 제거.
   주석 처리 + macro_ind ALWAYS_RUN 이 커버한다고 명시.
3. **make_trades_history_v3.py**: `pct_change(5)` FutureWarning → `pct_change(5, fill_method=None)` 수정 (4곳).
4. **config.py**: UTF-8 BOM (`\xef\xbb\xbf`) 제거 — AST parse 오류 원인.

### SAP(Stock_AI_Project) 파일 감사 결과
- `finrl` 참조 3곳 확인: 모두 주석(`# FinRL 표준화`) 또는 DB 테이블명 문자열(`'finrl_dataset_kr'`) — 실제 import 없음. 문제 없음.
- `auto_trader.py / rule_trader.py / finrl_trader.py` 는 스케줄에 미등록 — 데이터 수집만 활성.
- 실질 버그 없음 확인.

---

## 2026-06-14 — stock.db 통합·AI 피처 보강·감사 도구·키움 파일락

### 시스템 역할 분리 확립
- 천억이(C:\fin\outputs)를 마스터로, Stock_AI_Project를 데이터 수집 보조로 공식 정의.
- Stock_AI_Project의 auto_trader/rule_trader/finrl_trader는 비활성 — 매매는 오직 천억이만.
- 스케줄 최적화: 천억이 pykrx 16:05 수집 → stock.db UPSERT → Stock_AI_Project 06:30이 중복 스킵.

### stock.db 연동 및 AI 피처 보강
- `pykrx_collector.py` — 수집 후 `stock.db` korea_stocks/supply_demand 자동 UPSERT.
  name/sector 는 Stock_AI_Project 값 보존 (덮어쓰지 않음).
- `make_trades_history_v3.py` — stock.db supply_demand(2015~), korea_indicators(RSI/MACD/BB) 피처 추가.
  신규 컬럼: `for_net5_db`, `ins_net5_db`, `rsi_db`, `macd_hist_db`, `bb_pct_db`.
- `ai_trainer_v4.py` — 위 5개 피처 FEATURES 목록에 추가.
- `rebuild_after_backfill.bat` 신설 — 신용/대차 백필 완료 후 실행: trades_history_v3 재생성 → v4 재학습 → 커버리지 확인.

### paper_audit.py — paper vs 백테스트 drift 감사
- `paper_audit.py` 신설 — paper_signals.csv 의 1,597건 전체 실제 수익률 계산 (pykrx CSV 청산가 조회).
- 결과 저장: `paper_audit_result.csv`.
- 실측 결과 (2026-06-14 기준): 실제 평균 **+11.17%** / 백테 동기간 **+8.89%** (+2.3%p 실전이 더 좋음).
  승률 49.3% vs 47.3%. big-win(≥10%) 37.0% vs 34.5%. — 백테 예측이 보수적으로 잘 작동 중.
  ⚠ 신호 수 1,597건 vs 백테 동기간 7,229건(22%) — 슬롯 10개 + market_strong 필터 효과.

### 키움 파일락 (동시 접근 방지)
- `kiwoom_trader.py` buy/sell/daily 명령 + `kiwoom_extra.py` run() 양쪽에 파일락 추가.
- lock 파일: `C:/fin/Stock_AI_Project/data/.kiwoom.lock` (두 시스템 공유).
- 이미 잠겨 있으면 60초 대기 → stale(5분 초과) lock은 자동 강제 해제.

### 터미널 출력 정리
- `Stock_AI_Project/main_collector.py` — 3,000+개 종목마다 logger.info → logger.debug 강등.
  인라인 `_print_bar()` 게이지 추가 (우리 시스템 progress.py 패턴과 동일).
- `kiwoom_extra.py` — 50개마다 logger.info → `tqdm.write()` 교체 (tqdm 바 끊김 방지).
  커밋 주기 `i%5` → **종목마다 커밋** (중단 시 최대 1종목 손실, 기존 최대 4종목).

## 2026-06-13 (3차) — 전 수집기 "결측 우선" 정책

모든 일자별 수집기가 수집 전에 **결측(빈 날짜)을 먼저 스캔하고 구멍부터 채우도록** 개편:

| 수집기 | 결측 처리 |
|---|---|
| `gap_scan.py` (신설) | 공용 스캐너 — 최근 N영업일 결측 목록 + 휴장 마커(.holiday) |
| update_macro_daily | 마지막 파일 이후만 → **보유 구간 전체 스캔** (중간 구멍 자동 복구) |
| pykrx_collector | 휴장일에 `.holiday` 마커 생성 → 매일 재시도 낭비 제거 |
| us_market_collector | 최근 14영업일 결측 백필 (시계열 1회 다운로드 후 날짜별 분배) |
| krx_collector (공매도) | 최근 10영업일 결측 백필 후 전일분 |
| dart_collector | 최근 10영업일 결측 백필 후 당일분. **[버그 수정] 인자 없이 호출 시 사용법만 출력하고 종료하던 문제** (가드의 재수집이 무효였음) → 기본 동작 today |
| macro_collector | 기존부터 30일 윈도우 병합 = 이미 결측 우선 |
| kiwoom_collector | 일별 스냅샷이라 과거 재조회 불가 — 이력은 kiwoom_backfill 담당 (구조적 한계) |
| data_collector (KIS 분봉/랭킹) | 장중 스냅샷이라 과거 재조회 불가 — 놓친 날은 복구 불가 (구조적 한계) |

적용 직후 실측: us_market 12일·공매도 3일 결측 발견됨 → 다음 자동 실행(20:00 가드)에서 자동 복구.

## 2026-06-13 (2차) — 진행률 게이지 (progress.py)

- `progress.py` 신설 — 퍼센트 게이지 + 경과/예상 잔여시간(ETA). 장시간 배치 공용.
  적용: kiwoom_backfill (25종목마다), pykrx_collector (신규 10일마다).
  예: `[████████░░░░] 41.9% (665/1,587) 경과 12분 · 남은 ~17분 | 저장 600 실패 25`
- 앞으로 새로 만드는 장시간 작업에는 기본 적용.
- 참고: 백필이 이미 실행 중이어도 무관 — 다음 실행부터 적용 (이어받기 지원).

## 2026-06-13 — 키움 신용·프로그램 과거 백필 체계

- `kiwoom_backfill.py` 신설 (조회 전용): trades_history_v3 의 (종목, 신호일) 지점만
  연속조회(next-key)로 거슬러 수집 — 전 종목·전 기간이 아니라 학습에 필요한 곳만.
  종목당 파일 저장이라 **중단해도 재실행 시 이어받음**.
  - 백필: `python kiwoom_backfill.py` (수 시간) / 테스트: `python kiwoom_backfill.py credit 20`
  - 피처 생성: `python kiwoom_backfill.py merge` → `ai_data/kiwoom_hist_features.csv`
    (crd_remn_rt 신용잔고율, crd_remn_chg_5d 잔고 5일 증감, prm_net_5d_ratio 프로그램
    5일 순매수/거래대금)
  - 재학습: `python make_trades_history_v3.py` → `python ai_trainer_v4.py`
    (v3 가 피처 자동 병합, v4 가 자동 인식 — 없으면 기존 피처만 사용)
- 한계 (정직): 키움 이력 제공 범위는 실행 로그의 "도달 최소일"에서 확인.
  상장폐지 종목은 조회 불가 → 피처 결측 (라벨 아님 — XGBoost 가 NaN 자체 처리).
- **실행 순서 권장**: 월요일 키움 첫 자동매매(09:01) 정상 확인 → 그 후 백필 실행.

## 2026-06-12 (6차) — 키움 대시보드 통합 + 키움 수급 수집기

- **대시보드 개편**: `dashboard.html` 최상단에 키움 모의계좌 섹션 (예수금·보유·당일 주문)
  배치 — 메인. 기존 Paper(X2) 추적은 참고용으로 그 아래 유지 (두 모드 병행 비교 목적).
  데이터 소스: kiwoom_trader 가 status 때마다 저장하는 `db/kiwoom/snapshot.json` +
  `orders_*.csv` + `equity_history.csv` (일별 추이 누적).
- **`kiwoom_collector.py` 신설** (조회 전용 — 주문 코드 없음):
  ka10013 신용매매동향 → `db/credit/` (검증 안 되던 KIS probe 를 키움으로 대체),
  ka90013 종목별 프로그램매매 → `db/program/`. 대상 = 최근 신호 + 랭킹 상위.
  실전 키 있으면 조회용으로 우선 사용 (모의서버 시세 제한 회피).
  20:00 recollect_guard 에 credit/program 항목으로 등록.
- 향후 AI 피처 후보: 신용잔고 증감·프로그램 순매수 (표본 쌓인 뒤 v4 에 추가 검토).

## 2026-06-12 (5차) — 키움 모의: 시간외단일가 미지원 확인 → 원본 모드 전환

- 키움 모의서버는 **지정가/시장가만 지원** (시간외단일가 62 불가) — 공식 가이드 확인.
- kiwoom_trader 를 백테스트 원본 모드로 전환: 09:01 전일 신호 시장가 매수(≈시가),
  15:21 만기 시장가 매도(마감 동시호가 ≈ 종가). 스케줄 KIS_Kiwoom 16:05 →
  **KIS_KiwoomBuy 09:01 + KIS_KiwoomSell 15:21** 로 교체 (`install_scheduler.ps1` 재실행 필요).
- 매수 수량은 신호일 종가 기준 × 0.97 (갭상승 여유). 만기 계산도 진입=신호+1일로 정합.
- 결과적으로 키움 모의 = 원본 모드 / paper_tracker = X2 모드 — 두 진입 방식 병행 실측.

## 2026-06-12 (4차) — 전체 폴더 통합 재점검 수정

1. compare_exit_rules / compare_trailing_grid / compare_trailing_fine — 잔존하던
   `min_gross_pct=-30` 컷오프 제거 (CA필터가 대체. 이전 비교 결과는 재실행 필요).
2. `daily_loader` 에 pickle 캐시 추가 — 8년 데이터(1,961 CSV) 풀스캔이 단계마다
   반복되던 것을 1회만 수행. 캐시는 파일 추가/변경 시 자동 무효화
   (`macro_data/daily/_daily_cache.pkl`). 15:50 체인이 16:05 키움 주문 전에 여유있게 종료.
3. dashboard_generator KPI 교정 — CA필터 적용 + MTM(일별 종가 평가) + 랭킹 슬롯 +
   비X2 경로 off-by-one 수정. 이제 대시보드 수치가 백테스트/페이퍼와 동일 기준.
4. kiwoom_trader — 시간외단일가 매도에 당일 종가 지정(가격 필수 거부 대비),
   주문 멱등 비교의 CSV 문자열("True") 버그 수정.

참고(수정 안 함): `dashboard.py`·`ai_feature_engine.py`·`seed_paper_signals.py` 는
구버전 고아 스크립트 — 자동화에서 미사용. 재실행하지 말 것 (구룰 적용됨).
키움 buy 는 "오늘 날짜 신호"만 매수 — KRX 집계 지연일은 매수 0건이 정상 (룰상 신호 당일만 유효).

## 2026-06-12 (3차) — 키움증권 모의투자 자동매매 연동

- `kiwoom_trader.py` 신설 — `kiwoom-rest-api`(PyPI, REST 래퍼) 기반.
  인증은 환경변수(KIWOOM_API_KEY/SECRET/USE_SANDBOX) 주입 방식, 모듈별 클래스
  (Order.stock_buy_order_request_kt10000 등) 호출.
  매도(40일 만기) → 매수(오늘 신호, 거래대금 랭킹·빈 슬롯 1/N 배분, 시간외단일가) → 상태.
  멱등(당일 중복 주문 방지), 주문 감사 로그 `db/kiwoom/orders_*.csv`.
- config.py 에 KIWOOM_ENV/키 로드 추가. **prod 주문 차단 안전장치 내장.**
- `run_kiwoom.bat` + 스케줄러 `KIS_Kiwoom` 매일 16:05 등록 (시간외단일가 16:00 개장 직후).
- 응답 스키마는 방어적 파싱 — 첫 `status` 실행에서 필드 식별 실패 경고가 나오면
  로그의 raw keys 를 보고 `_pick()` 후보에 키를 추가하면 됨.
- 이로써 검증 체계 3중화: 백테스트(시뮬) / paper_tracker(가상) / 키움 모의계좌(실제 체결·슬리피지).

## 2026-06-12 (2차) — 8년 백필 완료 + 데이터 정화 + AI 7년 검증 결과

**데이터**:
- 8년 백필 완료: 정상 일봉 1,961일 (2018-06-14 ~ ). 단, KRX 가 공휴일에 "전 종목 0원"
  프레임을 반환해 **가짜 휴장일 파일 125개**가 섞여 있던 것을 발견 → `.holiday` 로 격리.
- **치명 버그 수정**: 장중/장전에 당일 파일이 0원으로 미리 생성되면 멱등 스킵 때문에
  그날 신호가 통째로 누락됨 (실제로 20260612.csv 가 0원으로 생성돼 있었음 → 제거).
  pykrx_collector 에 ① 전 종목 0원 프레임 저장 금지, ② 당일 15:40 이전 수집 금지 가드 추가.
  daily_loader 에도 close<=0 행 제거 방어선 추가.

**AI v4 — 7년 데이터 재검증 (정직한 결과)**:
- 학습셋 28,320 매매 (2019-06~2026-05, 2020 폭락·2022 약세장 포함)
- **test AUC 0.480, 확률 분위 스프레드 -6.69%p (역전)** — 13개월 데이터에서 보였던
  +4.68%p 스프레드는 강세장 한 구간에 과적합된 신기루였음이 확인됨.
- 결론: **AI 는 표시 전용 확정 (USE_AI=False), 사이징 연동 보류.** 주간 학습은 유지하되
  모니터링 용도. 수익의 원천은 전략 룰 + 리스크 관리이지 AI 가 아님.

## 2026-06-12 — 매크로 지표 연속 수집 + USAGE 재구성

- `macro_collector.py` 신설 — stock.db 의 macro_indicators(VIX/SOX/환율/KOSPI 등)가
  2026-05 에서 끊겨 AI 피처가 늙어가는 문제 해결. yfinance 로 `macro_data/indicators.csv`
  누적 (stock.db 와 동일 스키마). 20:00 recollect_guard 가 매일 자동 실행.
- `make_trades_history_v3.py` — 매크로 피처를 stock.db + indicators.csv 병합으로 읽도록
  변경 (최신분 우선, stock.db 없어도 동작).
- USAGE.md 재구성: 1부 사용법 / 2부 원리·파일 상세 / 3부 업데이트 내역.
  앞으로 모든 변경은 이 문서에 자동 반영하고 이 구조를 유지함.
- 증권사 API 가이드 추가 (1부 1.5): 신용잔고/프로그램매매 보강 시
  키움 REST API 또는 LS증권 xingAPI 권장.

## 2026-06-11 코드 검토 및 수정 내역

### 성과 측정 교정 (효과성 검증 결과)

| # | 문제 | 수정 |
|---|---|---|
| 1 | `capital_simulator` 의 -30% 일괄 컷오프가 실제 -60~-75% 손실 186건(11.7%)을 "액면분할 의심"으로 삭제 → CAGR/MDD/Sharpe 과대평가 | 컷오프 기본 해제. `strategies/_swing_base.find_corporate_action_dates()` 가 KRX 등락률(기준가 조정 반영) vs 원시 종가 비율의 10%p 괴리로 기업행위 발생일을 감지해 **해당 매매만**(21건) 제외 |
| 2 | MDD 가 보유 포지션을 진입원가로 평가 → 보유 중 평가손실 미반영 (-8% 허상) | `simulate_capital(price_map=, trading_dates=)` 로 일별 종가 mark-to-market 평가. 실제 MDD -42.6% |
| 3 | Sharpe 가 entry/exit 일자만의 불규칙 곡선에 √252 적용 → 과대 | MTM 일별 곡선 기준으로 재계산 (1.34) |
| 4 | 백테스트(거래대금 30억, ETF 미제외) vs live_signal(10억, ETF 제외) 유니버스 불일치 | live_signal 30억으로 통일 + `daily_loader.filter_universe()` 로 백테스트도 ETF/우선주/스팩 제외 (생존편향 방지 위해 이름 미상 종목은 유지) |
| 5 | walk-forward 의 "OOS 통과 전략 선별"은 OOS 재사용 → 낙관 편향 | 코드 수정 불가(방법론 한계) — README 에 명시. 진짜 OOS = paper trading 실측 |
| 6 | backtest_swing 이 15:20 진입 조건에 당일(장 마감 후 확정) 외인 수급 사용 → look-ahead | 전일(D-1) 수급으로 교체 |

### 프로그램 버그 수정

| # | 문제 | 수정 |
|---|---|---|
| 7 | 스케줄러 어디에도 macro_data 갱신이 없어 live_signal 이 옛 데이터로 동작 (6/05 에서 멈춰 있었음) | `update_macro_daily.py` 신설, run_paper.bat 첫 단계로 추가 |
| 8 | run_tick_collector.bat 이 bare `python` 호출 → 즉시 실패 시 5초마다 무한 재실행 (로그 4.4만 줄) | venv python 우선 + 연속 5회 즉사 시 중단 |
| 9 | paper_tracker `USE_AI=True` 시 미정의 변수 `skipped_by_ai` NameError | 해당 출력 제거 |
| 10 | `HighWithFiltersStrategy` 가 `time_stop_pct` 를 받기만 하고 미전달 → 설정해도 무시 | 전달 추가 (compare_exit_rules 재실행 권장 — 이전 비교 결과 무효일 수 있음) |
| 11 | paper_tracker 비X2 모드 청산이 entry+40일 (백테스트는 entry+39일) off-by-one | entry+39 로 통일 |
| 12 | make_trades_history_v2 의 vol/tv MA20 shift 가 종목 경계 무시 → feature 오염 | 그룹 내 shift 로 교체 (AI 모델 재학습 권장) |
| 13 | run_paper.bat EXITCODE 가 마지막 단계만 반영 | 단계별 실패 누적 |
| 14 | exit_rule_engine -2% 케이스 메시지 "강세 매도" 오기 | "급락 — 시가 매도" |
| 15 | 비용 관련: paper 0.33% vs 백테스트 0.43% | 불일치 아님 확인 (X2 매수 슬리피지 +2% 별도 반영) — 주석으로 문서화. 거래세 0.20%는 현행 0.15%보다 보수적 (유지) |

### 2026-06-11 추가 개선: 랭킹 슬롯 (적용됨) + 청산 그리드 (참고)

- **랭킹 슬롯**: 슬롯 부족 시 신호일 거래대금 큰 순으로 배정 (`StrategyTrade.score`).
  동일 매매셋 검증 — CAGR 동급(-1%p), **MDD -42.6% → -29.5%**, Sharpe 1.34 → 1.38. 기본 적용됨.
- **청산 오버레이 그리드** (`results/exit_grid_20260611.csv`): 이 표본(강세장 13개월)에선
  하드 손절(-15/-20%)은 CAGR만 깎고 MDD 개선 없음 (KOSDAQ 변동성에 whipsaw).
  유일하게 경합하는 옵션은 **트레일링 -12% (활성화 +15%)**: Sharpe 1.40, 승률 61.6%,
  단 CAGR -24%p. 꼬리위험 방어 목적이면 채택 가능, 아니면 무손절+랭킹 유지가 합리적.
  주의: 약세장 표본이 없어 손절의 진가치는 미검증.
- **갭하락 손절 체결 현실화**: stop 경로에서 시가 < 손절가면 시가 체결로 수정 (이전엔 손절가 체결 가정 = 낙관).

### 페이퍼 가상매매 재계산 (2026-06-11, 신호 1,597건)

| 방식 | CAGR | MDD | Sharpe |
|---|---|---|---|
| 구방식 (-30컷 + 원가평가) | +227.9% | -6.4% | +2.52 |
| **현재 시스템 (CA필터+MTM+랭킹)** | **+176.5%** | **-32.4%** | **+1.58** |
| + 거래대금 30억 룰 | +187.4% | -36.8% | +1.57 |

대시보드의 기존 KPI 는 구방식 기준이라 과대평가 — `dashboard_generator.py` 재실행 시에도
자체 계산 로직이 simulate_capital 을 쓰지 않으므로 수치 해석에 주의.

### 2026-06-11 추가: 수집 가드 + 신규 수집기

**자동화 변경 — `install_scheduler.ps1` 재실행 필수** (KIS_Recheck 신규):

| 시각 | 작업 | 내용 |
|---|---|---|
| 15:50 | KIS_Paper | (변경) macro 증분 갱신 → 신호 → 추적 → 대시보드 |
| **20:00** | **KIS_Recheck** (신규) | **당일 수집 검증 → 누락분 재수집 → 신호/대시보드 후속 갱신** |

`recollect_guard.py` 검사 항목: macro_daily / kis_daily / investor / index /
short / dart / us_market / credit / ai_features.
필수 항목(macro, kis_daily, us_market)이 재수집 후에도 누락이면 exit 1 + `logs/recheck_*.log` 에 기록.
macro 가 뒤늦게 채워진 날은 live_signal → paper_tracker → dashboard 를 자동 재실행해 신호 누락을 방지.

**신규 수집기**:

- `credit_collector.py` — 종목별 신용잔고 (KIS API). ⚠️ TR ID 는 공식 문서로
  재확인 못 한 후보값 — 첫 실행 시 자동 probe 후 실패하면 로그에 안내.
  실패 시 KIS 개발자센터에서 "신용잔고" API 의 URL/TR 확인 후
  `CANDIDATES` 리스트에 추가. `python credit_collector.py probe` 로 단독 검증 가능.
- `dart_fundamentals.py` — DART 재무제표 (매출/영업이익/순이익).
  `db/fundamentals/summary.csv` 누적 → 퀄리티 필터 개발용. 대상은 최근 신호 + 랭킹 상위 종목.
- `adjusted_probe.py` — CA필터 감지 결과를 pykrx 수정주가와 표본 대조 (주 1회 수동 권장).

**밀린 데이터 즉시 백필** (지금 바로 한 번):
```bat
run_recheck.bat
```
macro_data(6/05 이후), us_market(6/08 이후) 누락분을 자동 보충하고 신호를 재검사함.

**공매도 잔고 복구 시도**: 현재 pykrx 1.2.8 에서 잔고 API 가 깨져 거래량 fallback 중.
`pip install -U pykrx` 후 `python krx_collector.py both` 로 잔고 수집 복구 여부 확인.

### 2026-06-11 추가: AI 파이프라인 v4 (주간 학습)

**Stock_AI_Project/data/stock.db 판정**: korea_stocks 등 가격 테이블은 상장폐지 종목
누락(생존편향 — 2023-06 상장 1,642 중 133개 부재)으로 **라벨 생성에 사용 금지**.
news(2015~2026, 96만건 감성)와 macro_indicators(VIX·SOX·환율·KOSPI 11년)는 **피처로 채택**.

- `make_trades_history_v3.py` — 3개 전략 풀링(h500_40_MKT/h252_40/h500_20) →
  표본 1,597 → **8,340건**. 종목 피처 + 뉴스 감성 + 매크로 레짐 병합.
- `ai_trainer_v4.py` — 라벨 big-win(net≥+10%), purged split + 40일 embargo.
  첫 검증: AUC 0.545, **확률 5분위 스프레드 Q5−Q1 = +4.68%p** (사이징 근거로 유의미).
  단 단일 test 구간·강세장 표본 — 수개월 안정성 확인 전까지 USE_AI=False 유지.
- run_paper.bat: AI 학습은 **금요일만** 실행 (매일 재학습은 노이즈).
- **데이터 깊이 확장 (권장, 1회)**: `python pykrx_collector.py 8`
  → 8년치 백필 (2020 폭락·2022 약세장 포함, 기존 파일 자동 스킵, 수 시간 소요 — 밤에 실행).
  완료 후 금요일 학습에서 표본·레짐이 자동 확장됨.

### 수정 후 재실행 필요 목록

1. `python make_trades_history.py && python make_trades_history_v2.py` — CA필터/유니버스 반영 재생성
2. `python ai_trainer_v2.py && python ai_trainer_v3.py` — 교정된 데이터로 재학습
3. `python walkforward.py` — 교정된 유니버스 기준 견고성 재확인
4. `powershell -ExecutionPolicy Bypass -File .\install_scheduler.ps1` — 스케줄 재등록 (run_paper.bat 변경 반영)

---

단타 박스권 룰 v2 는 비용 한계로 break-even 미달 → 별건으로만 유지 (자동 백테스트).
