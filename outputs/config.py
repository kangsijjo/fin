"""
백테스트/자동매매 설정 파일 - 룰 v2 (스나이퍼 모드 적용)

룰 변경 시 RULE_V2 dict 만 수정하면 됩니다.
KIS API 인증은 .env 파일에서 자동으로 로드합니다.
"""

import os
import sys
from pathlib import Path

# ============================================================
# .env 파일 자동 로드
# ============================================================
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        # override=True       : .env 가 시스템 환경변수보다 우선 적용
        # encoding=utf-8-sig  : .env 에 BOM 이 있어도 첫 줄 변수명이 깨지지 않도록
        #                       (메모장 등이 UTF-8 저장 시 BOM 을 붙이는 경우 대비)
        load_dotenv(env_path, override=True, encoding="utf-8-sig")
    else:
        print(f"[WARN] .env 파일이 없습니다: {env_path}")
        print(f"       .env.example 을 참고해서 .env 를 생성하세요.")
except ImportError:
    print("[WARN] python-dotenv 미설치. `pip install python-dotenv` 권장.")
    print("       시스템 환경변수에서 KIS 키를 읽어옵니다.")


# ============================================================
# KIS API 환경 선택 및 인증 정보
# ============================================================
# KIS_ENV: "prod" (실전) 또는 "vps" (모의투자)
KIS_ENV = os.getenv("KIS_ENV", "vps").lower().strip()

if KIS_ENV == "prod":
    KIS_APP_KEY = os.getenv("KIS_PROD_APP_KEY", "").strip()
    KIS_APP_SECRET = os.getenv("KIS_PROD_APP_SECRET", "").strip()
    KIS_ACCOUNT = os.getenv("KIS_PROD_ACCOUNT", "").strip()
elif KIS_ENV == "vps":
    KIS_APP_KEY = os.getenv("KIS_MOCK_APP_KEY", "").strip()
    KIS_APP_SECRET = os.getenv("KIS_MOCK_APP_SECRET", "").strip()
    KIS_ACCOUNT = os.getenv("KIS_MOCK_ACCOUNT", "").strip()
else:
    print(f"[ERROR] KIS_ENV='{KIS_ENV}' 가 잘못됨. 'prod' 또는 'vps' 만 가능.")
    sys.exit(1)

# 인증 정보 누락 체크
if not KIS_APP_KEY or not KIS_APP_SECRET:
    print(f"[WARN] KIS_ENV='{KIS_ENV}' 의 인증 정보가 비어 있습니다.")
    print(f"       .env 파일의 다음 항목을 확인하세요:")
    if KIS_ENV == "prod":
        print(f"         - KIS_PROD_APP_KEY")
        print(f"         - KIS_PROD_APP_SECRET")
    else:
        print(f"         - KIS_MOCK_APP_KEY")
        print(f"         - KIS_MOCK_APP_SECRET")


# ============================================================
# 키움증권 REST API (모의투자 자동매매용)
# ============================================================
# KIWOOM_ENV: "mock" (모의투자, 기본) 또는 "prod" (실전 — kiwoom_trader 는
#             안전장치로 prod 에서 주문을 거부함. 실전 전환은 별도 작업.)
KIWOOM_ENV = os.getenv("KIWOOM_ENV", "mock").lower().strip()
if KIWOOM_ENV == "prod":
    KIWOOM_APP_KEY = os.getenv("KIWOOM_PROD_APP_KEY", "").strip()
    KIWOOM_APP_SECRET = os.getenv("KIWOOM_PROD_APP_SECRET", "").strip()
else:
    KIWOOM_APP_KEY = os.getenv("KIWOOM_MOCK_APP_KEY", "").strip()
    KIWOOM_APP_SECRET = os.getenv("KIWOOM_MOCK_APP_SECRET", "").strip()
KIWOOM_ACCOUNT = os.getenv("KIWOOM_ACCOUNT", "").strip()  # 참고용 (REST 는 앱키에 계좌 귀속)


# ============================================================
# 룰 v2 파라미터 (스나이퍼 모드 적용)
# ============================================================
RULE_V2 = {
    # 박스권 감지
    "box_window_minutes": 15,        # v2.6 (10 → 15) timeframe 더 확장 — 박스 형성 더 신뢰
    "box_max_pct": 2.5,              # (기존 2.0) 박스 최대 폭 (±%)
    "box_min_pct": 1.5,              # (기존 0.8) 수수료를 덮고 수익을 내기 위해 위아래 폭이 1.5% 이상인 넓은 박스만 타겟팅

    # 거래량 급증 신호
    "volume_baseline_minutes": 3,    # 직전 N분 평균 기준
    "volume_surge_multiplier": 2.0,  # 평균의 N배 이상이면 급증

    # 진입 (모드 A: 박스 하단 매수)
    "entry_offset_pct_of_box": 10.0, # (기존 5.0) 하단 10% 이내에 완전히 바짝 붙었을 때만 깐깐하게 매수

    # 청산
    "tp_first_pct": 1.0,             # v2.6 (0.8 → 1.0) 1차 익절 더 큼 — 비용 0.43% 대비 마진 확보
    "tp_first_ratio": 0.5,           # 1차 익절 비율 (50%)
    "trailing_drawdown_pct": 0.4,    # 트레일링 스탑 (고점 대비 -%)
    "time_exit_minutes": 20,         # v2.6 (15 → 20) 더 진득 — 박스 15분이면 결말도 15~20분에 더 걸림
    "stop_loss_pct": -2.0,           # (기존 -1.8) 잔파도 털림 방지를 위해 손절폭 소폭 확대 (-%)

    # v2.7 조기 종료 — 진입 후 절반 시점에 평가, 임계값 미만이면 즉시 종료
    "early_exit_enabled": True,
    "early_exit_minutes": 10,         # 진입 후 N분 시점에 평가 (time_exit_minutes / 2 권장)
    "early_exit_threshold_pct": -0.5, # 현재 손익이 이 % 이하면 즉시 조기 종료

    # 비용 가정 (보수적)
    "fee_buy_pct": 0.015,            # 매수 수수료 (%)
    "fee_sell_pct": 0.015,           # 매도 수수료 (%)
    "tax_kospi_pct": 0.18,           # 매도 시 거래세 (KOSPI)
    "tax_kosdaq_pct": 0.20,          # 매도 시 거래세 (KOSDAQ)
    "slippage_pct": 0.05,            # 슬리피지 (±%)
}


# ============================================================
# 종목 필터링
# ============================================================
TOP_N_STOCKS = 50                    # 거래대금 raw 상위 N개 (ETF 포함, 분석 단계에서 필터)
TOP_N_INVESTOR = 100                 # 외국인/기관 매매 데이터 수집 종목 수
MIN_MARKET_CAP_BILLION = 100         # 시가총액 최소 (억원). 0이면 필터 안 함.
EXCLUDE_PREFERRED = True             # 우선주 제외
EXCLUDE_ETF = True                   # ETF 제외
EXCLUDE_ADMIN = True                 # 관리종목/투자유의 제외


# ============================================================
# 데이터 저장 경로
# ============================================================
DATA_DIR = "./data"
RANKING_DIR = f"{DATA_DIR}/rankings"
MINUTE_DIR = f"{DATA_DIR}/minute_bars"   # (deprecated) 백테스트 호환용 폴더. 신규 저장은 db/minute/ 로 감
RESULT_DIR = "./results"

# ---- 통합 DB 폴더 ----
DB_DIR = "./db"
DB_INVESTOR_DIR = f"{DB_DIR}/investor"   # 외인/기관/개인 순매수 (일자별 CSV)
DB_DAILY_DIR    = f"{DB_DIR}/daily"      # 일봉 60일치 (일자별 CSV, 그날 풀의 전종목 묶음)
DB_MINUTE_DIR   = f"{DB_DIR}/minute"     # 분봉 (YYYY-MM/YYYYMMDD/CODE.csv)
DB_XLSX_DIR     = f"{DB_DIR}/xlsx"       # 월말 xlsx 합본 (시트=일자)
DB_INDEX_DIR    = f"{DB_DIR}/index"      # 시장지수 KOSPI/KOSDAQ 스냅숏 (일자별 CSV)
DB_CREDIT_DIR   = f"{DB_DIR}/credit"     # 신용잔고 (일자별 CSV, T-1)
DB_SHORT_DIR    = f"{DB_DIR}/short"      # 공매도 잔고 (일자별 CSV, T-1)
DB_DART_DIR     = f"{DB_DIR}/dart"       # DART 공시 (일자별 CSV)

# 일봉 수집 기간 (영업일 기준)
DAILY_LOOKBACK_DAYS = 60


# ============================================================
# 백테스트 신호 필터 (incremental, on/off 비교용)
# ============================================================
# 전일(D-1) 외인 순매수 > 0 인 종목만 진입 허용.
# False 면 필터 끄고 기존 룰만 적용 (효과 비교 기준선).
ENABLE_FOREIGN_FILTER = False

# 백테스트 시 ETF / 우선주 / 관리종목 제외 (data_collector._is_excluded 사용).
# 수집은 raw 50개 전부 받지만 시뮬레이션은 일반주만 돌림.
ENABLE_ETF_FILTER = True

for d in [DATA_DIR, RANKING_DIR, MINUTE_DIR, RESULT_DIR,
          DB_DIR, DB_INVESTOR_DIR, DB_DAILY_DIR, DB_MINUTE_DIR, DB_XLSX_DIR,
          DB_INDEX_DIR, DB_CREDIT_DIR, DB_SHORT_DIR, DB_DART_DIR]:
    os.makedirs(d, exist_ok=True)


# ============================================================
# 현재 설정 요약 출력 (디버그용)
# ============================================================
def print_summary():
    print("=" * 60)
    print(f" KIS 환경:    {KIS_ENV} ({'실전' if KIS_ENV == 'prod' else '모의투자'})")
    print(f" APP_KEY:    {KIS_APP_KEY[:8] + '...' if KIS_APP_KEY else '(미설정)'}")
    print(f" 계좌번호:    {KIS_ACCOUNT if KIS_ACCOUNT else '(미설정)'}")
    print(f" 룰 v2 박스: {RULE_V2['box_window_minutes']}분 / ±{RULE_V2['box_max_pct']}%")
    print(f" 거래량 급증: {RULE_V2['volume_surge_multiplier']}배")
    print(f" 1차 익절:   +{RULE_V2['tp_first_pct']}% / 트레일링 -{RULE_V2['trailing_drawdown_pct']}%")
    print(f" 시간 청산:  {RULE_V2['time_exit_minutes']}분")
    print(f" 손절:       {RULE_V2['stop_loss_pct']}%")
    print("=" * 60)


if __name__ == "__main__":
    print_summary()