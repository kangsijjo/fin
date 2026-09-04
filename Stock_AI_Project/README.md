# Stock AI Project

한국·미국 주식 자동매매 시스템. LightGBM 기반 5영업일 수익률 예측 + KIS Open Trading API 연동 (국내주식 + 해외주식).

---

## 시스템이 무엇을 하는가

매일 자동으로:
1. **데이터 수집** — 약 3000 종목의 일별 OHLCV + 외국인/기관 수급(KIS, 매일) + 신용잔고/대차거래(키움, 토요일 주 1회) + 미국 거시지표
2. **지표 계산** — 26개 피처 (이동평균·RSI·MACD·볼린저·변동성·거시·수급·신용/대차 등)
3. **모델 예측** — 섹터별 LightGBM 모델이 "5영업일 후 +2% 이상 오를 확률" 계산
4. **자동 매매** — Top-N 종목 매수 (기본 Top-1, config `top_n`) + 변동성 기반 사이징, 주문가능현금 기준
5. **리스크 관리** — 익절 +10% / 5영업일 시간 청산 / 모델 매도신호 조기청산 / 일일 손실 -2% 도달 시 신규매수 중단 (손절은 백테스트 근거로 비활성, config 로 재활성 가능)
6. **장중 모니터** — 1분마다 보유 종목 현재가 폴링 (갭다운 대응)
7. **주간 재학습** — 매주 일요일 모델 자동 갱신

사용자는 **PC만 켜두면** 무인 운영 됩니다.

---

## 시스템 구조

```
┌────────────────────────────────────────────────────────────────┐
│                  scheduler.py (무한 루프)                      │
│  - 매일/매주 시각마다 작업 자동 호출                            │
│  - 크래시 시 run_scheduler.bat 가 30초 후 자동 재시작           │
└────────────────────────────────────────────────────────────────┘
                  │
                  │ subprocess 호출
                  ↓
┌────────────────────────────────────────────────────────────────┐
│  데이터 수집 (collector)                                       │
│  ├─ main_collector.py        주가 (FinanceDataReader)          │
│  ├─ macro.py                 거시지표 (NASDAQ/SOX/VIX 등)      │
│  ├─ supply_demand.py         외국인/기관 매매 (KIS, 매일 증분)  │
│  ├─ kiwoom_api.py            키움 REST 클라이언트 (데이터 전용) │
│  ├─ kiwoom_extra.py          신용잔고/대차거래 (키움 전용 TR)   │
│  ├─ backfill_supply_kiwoom.py 수급 9년 백필 (키움 ka10059, 1회) │
│  └─ scanner.py               주도 섹터 자동 선택                │
└────────────────────────────────────────────────────────────────┘
                  ↓
┌────────────────────────────────────────────────────────────────┐
│  지표 계산 (processor)                                         │
│  └─ indicators.py            24개 피처를 batch 계산 후 DB 저장 │
└────────────────────────────────────────────────────────────────┘
                  ↓
┌────────────────────────────────────────────────────────────────┐
│  학습 + 백테스트 (models, trader)                              │
│  ├─ train.py                 LightGBM 분류 모델 학습           │
│  └─ backtest.py              주간 walk-forward 백테스트         │
│                              (purge gap 5일 + 클립 + inf 가드) │
└────────────────────────────────────────────────────────────────┘
                  ↓
┌────────────────────────────────────────────────────────────────┐
│  실거래 (trader)                                                │
│  ├─ auto_trader.py           매수 시그널 → KIS 매수             │
│  ├─ position_manager.py      포지션 기록 + 손절/익절/시간청산   │
│  ├─ intraday_monitor.py      장중 1분 폴링                      │
│  ├─ market_scanner.py        거래량/등락률 상위 누적 (분당)     │
│  ├─ risk.py                  변동성 기반 사이징 + 일일 손실 한도│
│  └─ kis_api.py               KIS API 래퍼 (토큰 캐시,          │
│                              국내+미국 시세/주문/잔고,          │
│                              시장가 청산, 주문가능현금 조회)    │
└────────────────────────────────────────────────────────────────┘
                  ↓
┌────────────────────────────────────────────────────────────────┐
│  검증 (trader)                                                  │
│  └─ validate_live_vs_backtest.py                                │
│     실거래 결과를 백테스트 예측과 비교                          │
└────────────────────────────────────────────────────────────────┘
```

---

## 설치

```bash
git clone <repo>
cd Stock_AI_Project
python -m venv venv
venv\Scripts\activate              # Windows
pip install -r requirements.txt
```

### `.env` 설정

```
KIS_MOCK_APP_KEY=...
KIS_MOCK_APP_SECRET=...
KIS_MOCK_ACCOUNT=계좌번호앞8자리
KIS_APP_KEY=...                    # 실전 (선택)
KIS_APP_SECRET=...
KIS_ACCOUNT=...
KRX_API_KEY=...                    # 일부 지수 API (선택)
DART_API_KEY=...                   # 공시 (선택, transformers 필요)

# 키움증권 REST API — 데이터 수집 전용 (수급 백필, 신용/대차)
KIWOOM_ENV=mock                    # mock | prod (시세성 TR 미지원 시 prod 권장)
KIWOOM_MOCK_APP_KEY=...
KIWOOM_MOCK_APP_SECRET=...
KIWOOM_PROD_APP_KEY=...
KIWOOM_PROD_APP_SECRET=...
```

---

## 사용 방법

### 첫 가동 (1회만)

```bash
python -m src.collector.main_collector       # 주가 백필 (오래 걸림)
python -m src.collector.macro                # 거시지표
python -m src.collector.supply_demand        # 수급 (KIS, 최근 30일)
python -m src.collector.backfill_supply_kiwoom   # 수급 과거 백필 (키움, 수 시간~)
python -m src.collector.kiwoom_extra --backfill  # 신용/대차 백필 (키움)
python -m src.processor.indicators all       # 26개 피처 생성
python -m src.models.train all               # 모델 학습
python -m src.trader.backtest all            # 백테스트
```

### 자동 운영

```bash
.\run_scheduler.bat                          # 무한 루프 + 자동 재시작
```

부팅 자동 등록: `Win+R` → `shell:startup` → `run_scheduler.bat` 바로가기 추가

### 검증

```bash
python -m src.trader.validate_live_vs_backtest    # 실거래 vs 백테스트 비교
```

---

## 자동 스케줄

| 시간 | 작업 |
|------|------|
| 매일 06:30 | 주가·수급·거시지표 수집 |
| 매일 06:50 | 주도 섹터 스캔 |
| 매일 09:00 | 한국 장중 모니터 + 시장 스캐너 가동 (15:30 종료) |
| 매일 10:05 | 한국 자동 매매 |
| 매일 22:20 | 미국 스캔 |
| 매일 22:30 | 미국 자동 매매 (USD 예수금 필요) |
| 매일 23:35 | 미국 장중 모니터 (~06:00) |
| 매주 토요일 07:00 | 데이터 갭 감지 + 자동 백필 |
| 매주 토요일 08:00 | 키움 신용/대차 주간 수집 (공백 감지 — 최근 30일 중 빠진 날짜만 호출) |
| 매주 일요일 07:00 | 모델 전체 재학습 |

키움 API 는 다른 시스템과 키를 공유하므로 **주말 전용** — 주중에는 스케줄러도 수동 스크립트도 키움을 호출하지 않는다 (수동 강제: `--force`).

매매·장중모니터 작업은 토/일에 자동 스킵됩니다 (2026-06-11).

---

## 자본 관리 / 리스크 룰

| 항목 | 기본값 | 설정 위치 |
|------|--------|----------|
| 매수/매도 임계값 (prob_threshold) | 0.55 | `config.yaml` (model 섹션) |
| 보유 기간 (holding_days) | 5 영업일 | `config.yaml` |
| 손절 (stop_loss) | 0 = 비활성 (2026-06-08, 백테스트 근거) | `config.yaml` |
| 익절 (take_profit) | +10% | `config.yaml` |
| 모델 매도신호 조기청산 | 활성 (2026-06-11, close_reason=model_sell) | `auto_trader.py` |
| 종목당 자본 비중 한도 | 30% | `config.yaml` (max_weight) |
| 목표 일일 변동성 | 2% | `config.yaml` (target_vol) |
| 일일 손실 한도 | -2% (도달 시 신규 매수 중단) | `config.yaml` (daily_loss_limit) |
| 동시 매수 슬롯 | Top-1 (2026-06-08, 백테스트 근거) | `config.yaml` (top_n) |

청산 매도는 시장가로 나갑니다 (지정가 미체결 orphan 방지, 2026-06-11).
매수 사이징은 예수금이 아닌 '미수없는 주문가능현금' 기준입니다.

---

## 왜 이렇게 만들었나

**감정을 배제한 데이터 기반 매매**가 목표입니다.

- 사람은 시장 변동성에 감정적으로 반응 (공포 매도, 욕심 매수)
- 같은 룰을 일관되게 적용하기 어려움
- 24시간 시장 모니터링 불가능

→ 모델이 일관된 룰로 판단, 시스템이 자동 매도/매수, 손실 한도가 자본 보호.

다만 **AI 가 항상 옳다는 가정은 X**. 검증 인프라(백테스트, walk-forward, permutation test, 실거래 vs 백테스트 비교)를 갖춰 **데이터로 진짜 실력을 측정** 하는 게 본 시스템의 핵심.

---

## 단점과 한계 (정직하게)

이 시스템에 대한 정직한 평가입니다. 모의투자 단계에서 반드시 인지해야 할 부분.

### 1. 모델 알파가 약함
- 9년 백테스트 baseline: 9개 한국 섹터 중 **1개(제약)만 양수 수익**
- Top-1 매수 평균 5일 수익: 약 -0.08% (반도체) ~ +0.58% (제약)
- 수수료/세금 round-trip 0.41% 빼면 사실상 0
- **현 단계에선 buy & hold 보다 못할 가능성이 큼**

### 2. 분류 모델의 종목 ranking 한계
- "5일 후 +2% 이상" 분류 학습 → 종목 간 절대 비교 어려움
- 매수 시그널 평균은 양수지만 **매일 Top-1만 사면 평균이 무너짐**
- 회귀 모델 전환이 정답이지만 큰 작업

### 3. 단기 모멘텀에 의존
- 24개 피처가 대부분 단기 기술지표 (5~60일 이동평균 등)
- 펀더멘털(PER/PBR/실적) 미반영
- 강세장에서 buy & hold 이기기 어려움

### 4. 무료 API 의 한계
- KIS API: 외국인/기관 매매는 최근 30일만 제공 → **키움 ka10059 백필로 해소 가능** (`backfill_supply_kiwoom`, 2026-06-12 추가. 단 백필 실행 전까지는 30일 한계 그대로)
- 호출 빈도 제한 (KIS 모의 ~2 req/s, 키움도 보수적으로 ~3 req/s 운영)
- 진정한 OCO 미지원 (장중 폴링 1분 간격으로 갭다운 보호 시도)

### 5. 잠자기/네트워크 의존
- PC 가 잠들면 매매 자체가 안 됨 → 잠자기 OFF 필수
- 인터넷 끊김 시 그 시점 작업 영구 누락
- Windows 자동 재부팅 (업데이트) 시 부팅 자동 등록 안 되어 있으면 멈춤

### 6. 데이터 위생 이슈
- FinanceDataReader 의 수정주가 처리 불완전 (액면분할 등에서 가격 점프)
- 이번 라운드에서 발견된 데이터 중복 (66%) 같은 침묵 오염 가능성

### 7. 시장 레짐 변화 미반영
- 모델이 강세장/약세장 구분 없이 같은 룰 적용
- VIX/KOSPI 변동률은 피처로 들어가지만 "레짐 자체"는 학습 안 함
- 약세장에서 손절 룰 덕분에 buy & hold 보다 강할 수 있지만, 강세장에서 100% 매수 대비 약함

### 8. 자본 분산 부족
- 매수 슬롯 Top-1 (2026-06-08 백테스트 근거) → 집중 리스크 큼
- 한 종목 급락이 그대로 전체 성과에 반영됨 (MDD -77% 가능)
- 진짜 분산은 동시 보유 10~30 종목 필요

### 9. 모의투자 ≠ 실거래
- 모의는 슬리피지·체결 지연·시장 임팩트 0
- 실전에서는 같은 시그널에 평균 0.1~0.3% 추가 비용
- 모의 결과가 좋아도 실전에서 안 좋을 수 있음

---

## 진단 / 디버깅 도구

| 파일 | 용도 |
|---|---|
| `diag_backtest.py` | 매수 시그널 평균/승률/분위 분포 |
| `diag_top1.py` | Top-1 평균 vs 전체 매수 평균 차이 |
| `diag_macro_shift.py` | macro 피처 시점 정렬 검증 |
| `diag_permutation.py` | features 의 leakage 단일-fold 검증 |
| `diag_permutation_nfold.py` | N-fold permutation (robust) |
| `diag_ta_library.py` | pandas-ta-classic 의 backward 검증 |
| `fix_dup_stocks.py` | korea_stocks 중복 행 정리 (1회) |
| `fix_dup_indicators.py` | korea_indicators 중복 행 정리 (1회) |
| `fix_date_format.py` | date 컬럼 포맷 통일 (1회) |
| `fix_requirements_encoding.py` | requirements.txt UTF-16→UTF-8 (1회) |

---

## 권장 운영 사이클

1. **0~2주**: 모의투자 첫 데이터 수집. 매수 발생/체결 확인.
2. **2주차 검증**: `validate_live_vs_backtest.py` 로 GAP 분석
3. **1~3개월**: 수급 데이터 누적 (KIS API 한도로 일별 +1일)
4. **3개월 후**: 수급 포함 재학습 → 새 모델 vs 옛 모델 A/B 비교
5. **6개월 후**: 통계적으로 의미 있는 거래 누적 → 실전 전환 결정

---

## 주의사항

- 이 시스템은 **투자 참고용**이며 손실 책임은 본인에게 있습니다
- **실전 투자 전 반드시 모의투자로 충분히 검증**하세요
- 모의 결과가 좋아도 실전에서 같은 결과 보장 X
- 백테스트 baseline 이 음수인 현재 단계에선 **실전 투자 금지**
- 모델 알파 강화 (KRX 수급 누적, DART 활성화, 회귀 전환 등) 후 재평가 권장
