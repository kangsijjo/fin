"""
키움증권 모의투자 자동 집행기 — v4: 안C' 포트폴리오 (신고가 6:4, 2026-10-18)

의존: pip install kiwoom-rest-api
인증: .env 의 KIWOOM_MOCK_APP_KEY / KIWOOM_MOCK_APP_SECRET (KIWOOM_ENV=mock)

명령:
  python kiwoom_trader.py status   # 예수금·잔고·미체결 출력
  python kiwoom_trader.py buy      # 전략별 우선순위로 신호 종목 매수
  python kiwoom_trader.py sell     # per-signal holding_days 기준 만기 종목 매도
  python kiwoom_trader.py daily    # sell → buy → status 일괄 (스케줄러용)

전략별 슬롯 구조 (총 10) — 안C' 포트폴리오 (2026-10-18):
  슬롯 1-6  →  high_500d_filt  (500일 신고가+거래량+시장강세, 20일 보유)  max=6  ← 우선
  슬롯 7-10 →  high_52w_filt   (52주 신고가+거래량+시장강세,  20일 보유)  max=4
  두 전략 모두 강도 필터 면제(STRENGTH_EXEMPT) — 백테스트와 같은 규칙. 후보 정렬만 강도순.

  ※ 변경 이력: 2026-06-17 for_high20_mkt(CAGR 기여 낮음) 제거, rsi_reversal +2슬롯, rsi_vol 신규 2슬롯
     2026-10-18 rsi_reversal·rsi_vol 제거 → high_500d_filt 6슬롯(사용자 결정, 설명서 §56).
       근거: 기간 백테스트 2021~25 순위 52·51위 → 2026 표본외 52·39위(두 기간 모두 최하위권).
       star_high_500d_20 은 9위 → 2위. 500일 신고가는 52주 신고가의 부분집합이라 두 버킷이
       같은 종목을 노리면 500일 쪽이 먼저 가져간다(같은 종목 중복 매수는 already/pos 로 차단).
     기존 rsi 보유분은 '레거시'로 원장 보유일대로 만기 청산된다(슬롯 배정엔 미포함).

운용 룰:
  - 매수: 신호 다음 영업일 09:01 시장가. 전략별 빈 슬롯 확인 후 우선순위 배정.
  - 매도: 진입일 포함 holding_days 영업일째 15:21 시장가. 전략마다 보유일 다름.
  - 멱등: 같은 날 같은 종목 중복 주문 방지 (db/kiwoom/orders_*.csv 로그 기준).
  - `daily` 명령: 12시 이전 → 매수, 이후 → 매도.

⚠️ 안전장치: KIWOOM_ENV=prod 면 주문 명령을 거부한다 (조회만 허용).
   실전 전환은 모의 검증 수개월 후 별도 논의 — 그때도 사용자가 직접 결정.

💰 본계좌 프로필(2026-11-02 §96): KIWOOM_PROFILE=real (run_kiwoom_real.bat) — 같은 로직을 실서버 키·db/kiwoom_real·
   본계좌 전용 설정으로 돌린다. 실제 주문은 모드 'live'(사용자가 대시보드에서 두 번 확인해 켬)일 때만, 기본은 드라이런.
   본계좌엔 사용자가 직접 산 종목이 있을 수 있어 프로그램 원장(수량 포함)에 있는 몫만 사고판다.
   매수는 시장가 대신 '현재가 +2% 지정가'(즉시 체결, 미수·상한가 기준 증거금 없음), 주문가능금액은 100% 현금 기준.

주문유형(trde_tp): 0 지정가 / 3 시장가
"""

import os
from fin_paths import KIWOOM_LOCK as _FIN_KIWOOM_LOCK   # 절대경로 단일 출처(2026-09-04)
import re as _re
import sys
import csv
import time
import json
import glob as _glob
from contextlib import contextmanager
from datetime import datetime

import pandas as pd


import kill_switch

def _retry_note():
    """크래시 알림에 붙일 '이후 어떻게 되는지' 안내.

    [2026-08-21 수정] 종전 문구는 "실행 중단(자동 재시도 없음)" 이었는데 사실과
    다르다 — 2026-07-21 에 run_kiwoom.bat 에 재시도 루프(5분 간격 2회)를 넣고
    이 문구를 안 고쳤다. 사용자가 '완전히 멈췄다'고 오해하게 만든다
    (08-21 KIS 쪽에서 실제로 그렇게 읽혔다).
    """
    return ("run_kiwoom.bat 이 5분 뒤 재시도합니다(최대 2회). "
            "재시도 시작/종료도 로그에 남으니, 로그에 attempt 2 가 없으면 "
            "재시도 대기 중 프로세스가 죽은 것입니다.")


# ── 키움 API 파일락 ──────────────────────────────────────────────────────────
# 우리 시스템(kiwoom_trader)과 Stock_AI_Project(kiwoom_extra) 가
# 동시에 키움 API에 접근하는 것을 방지하는 파일 기반 잠금.
# lock 파일: C:/fin/Stock_AI_Project/data/.kiwoom.lock
_LOCK_CANDIDATES = [
    os.getenv("KIWOOM_LOCK", ""),
    "../Stock_AI_Project/data/.kiwoom.lock",
    str(_FIN_KIWOOM_LOCK),                    # FIN_ROOT 기반(fin_paths) — 이관 시 자동 추적
]
_LOCK_PATH = next((p for p in _LOCK_CANDIDATES if p and os.path.dirname(p)
                   and os.path.isdir(os.path.dirname(p))), None)


def _lock_try_acquire(who: str):
    """O_CREAT|O_EXCL 원자적 잠금 시도. True=획득 / False=남이 보유 / None=쓰기불가(잠금 생략).

    [2026-07-07] 기존 '존재확인 후 open(w)' 는 확인~쓰기 사이 레이스로 두 프로세스가
    동시에 잠금을 '획득'할 수 있었음(09:01/09:03 트레이더 이중 기동 시 실위험).
    """
    try:
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            json.dump({"who": who, "ts": time.time(),
                       "started": datetime.now().isoformat()}, f)
        return True
    except FileExistsError:
        return False
    except Exception:
        return None


@contextmanager
def kiwoom_lock(timeout: int = 30):
    """키움 API 파일락 컨텍스트 매니저.

    다른 프로세스가 이미 잠금을 보유하면 timeout 초 대기 후 경고와 함께 계속.
    (완전 차단 대신 경고 — 토큰이 다른 파일에 저장되므로 충돌 위험이 낮음)
    [2026-07-07] 획득을 원자화(O_EXCL). 타임아웃으로 '잠금 없이 진행'한 경우
    종료 시 남의 락을 지우지 않도록 acquired 플래그로 구분(기존엔 무조건 삭제
    → 보유 프로세스의 잠금이 증발해 제3 프로세스가 끼어들 수 있었음).
    """
    if _LOCK_PATH is None:
        yield   # lock 경로를 못 찾으면 잠금 없이 진행
        return

    who = f"kiwoom_trader(pid={os.getpid()})"
    deadline = time.time() + timeout
    acquired = False
    while True:
        got = _lock_try_acquire(who)
        if got is True:
            acquired = True
            break
        if got is None:   # 잠금파일 생성 불가(권한 등) — 잠금 없이 진행
            break
        try:   # 남이 보유 중 — stale 판정/대기
            with open(_LOCK_PATH, "r") as f:
                info = json.load(f)
            age = time.time() - info.get("ts", 0)
            if age > 300:   # 5분 이상 된 stale lock → 강제 해제 후 재시도
                # 삭제 직전 재확인 — 판정~삭제 사이에 다른 대기자가 stale 을 지우고
                # '새 락'을 걸었으면 건드리지 않는다(이중 진입 레이스 축소, 2026-07-10)
                with open(_LOCK_PATH, "r") as f2:
                    if json.load(f2).get("ts") != info.get("ts"):
                        continue
                os.remove(_LOCK_PATH)
                continue
            print(f"[kiwoom_lock] 잠금 대기 중 (보유: {info.get('who','?')}, {age:.0f}초 경과)...")
        except FileNotFoundError:
            continue   # 방금 해제됨 → 즉시 재시도
        except Exception:
            break   # lock 파일 손상 → 무시하고 진행
        if time.time() > deadline:
            print("[kiwoom_lock] 대기 시간 초과 — 잠금 무시하고 진행")
            break
        time.sleep(5)

    try:
        yield
    finally:
        if acquired:   # 내가 획득한 잠금만 해제(남의 락 오삭제 방지)
            try:
                os.remove(_LOCK_PATH)
            except Exception:
                pass

import config

# 콘솔 인코딩 방탄(2026-07-17) — KIS 쪽과 동일: bat 밖 실행(cp949)에서 이모지 print
# 가 UnicodeEncodeError 로 즉사하는 것 방지.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# [2026-11-02 §96] 키움 본계좌(실계좌) 프로필 — KIWOOM_PROFILE=real 이면 같은 로직을 실서버 키·별도 폴더·본계좌 설정으로.
#   실제 주문은 모드 'live'(사람이 켬)일 때만, 그 외엔 드라이런(냈을 주문만 dryrun_YYYYMMDD.csv 에 기록). real_account 참조.
PROFILE = (os.getenv("KIWOOM_PROFILE") or "mock").strip().lower()
REAL = PROFILE == "real"
SIGNALS_CSV = "./db/kiwoom_real/signals.csv" if REAL else "./paper_signals.csv"
ORDERS_DIR = "./db/kiwoom_real" if REAL else "./db/kiwoom"
ACCOUNT_KEY = "kiwoom_real" if REAL else "kiwoom"         # live_config 계좌
LABEL = "키움 본계좌" if REAL else "키움 안C"               # 알림 표기 — 실계좌 알림이 모의와 헷갈리지 않게
TAG = "키움 본계좌" if REAL else "키움"
STRENGTH_ACCOUNT = "real_kiwoom" if REAL else "kiwoom_안C"   # 강도 기록 계좌(본계좌는 'kiwoom' 으로 시작 안 함 — 모의 집계와 분리)
if REAL:
    import real_account as _RA
    MODE = _RA.get_mode()            # 실행마다 한 번만 읽는다(도중에 바뀌어도 이번 실행은 같은 모드)
    os.makedirs(ORDERS_DIR, exist_ok=True)
    # 공용 키움 락(모의·KIS·수집기)을 기다리면 09:00 진입이 늦어진다 — 본계좌는 다른 키·서버라 자기 실행끼리만 막는다
    _LOCK_PATH = os.path.abspath(os.path.join(ORDERS_DIR, ".real_trader.lock"))
else:
    MODE = "mock"
DRY = REAL and MODE != "live"        # 드라이런 — 주문·원장 변경 없음
HOLDING_DAYS_DEFAULT = 40   # 구버전 CSV 호환 fallback
MAX_CONCURRENT = 10

# 전략별 최대 슬롯 수 (합계 = MAX_CONCURRENT) — 안C 포트폴리오 (2026-06-17)
# [2026-10-21] 전략 구성은 live_config(대시보드 '모의매매 전략 설정')가 단일 출처 — 파일이 없거나 깨지면
# 현행값(기본값: high_500d_filt 6 / high_52w_filt 4, 둘 다 강도 면제, high_52w_filt 익절 +50%)과 같다.
import live_config as _LC
_ACC = _LC.account(ACCOUNT_KEY)
STRATEGY_MAX_SLOTS = dict(_ACC["slots"])
# 매수 우선순위 (앞 전략이 빈 슬롯 먼저 채움) — 설정 화면의 위→아래 순서
STRATEGY_PRIORITY = list(_ACC["priority"])
# 강도 필터 면제(2026-10-18 사용자 결정) — 7월 이후 high_52w_filt 신호 98건 중 5.7 이상 0건(최고 5.68)
# 이라 5.7 을 적용하면 신고가 전략은 사실상 매수 불가. 백테스트(강도 필터 없음) 규칙과 맞춘다.
# kis_trader.STRENGTH_EXEMPT 와 함께 ai_paper_trader.STRENGTH_EXEMPT 의 합집합(테스트로 고정).
STRENGTH_EXEMPT = set(_ACC["exempt"])
ORDER_TYPE_BUY = "3"     # 시장가 (모의서버는 지정가/시장가만 지원)
ORDER_TYPE_SELL = "3"    # 시장가 — 15:21 주문 시 마감 동시호가 참여 ≈ 종가 체결
ORDER_TYPE_LIMIT = "0"   # 지정가 (익절 목표가 매도용)

# 수익실현(익절) 목표 — profit_sweep.py 장중 지정가(limit) 의미론 검증 채택(2026-07-02 사용자 결정).
#   rsi_vol +20%: full Δ+0.392%p / fwd Δ+0.611%p (PF 1.15→1.24, +15~50% 전 구간 양수)
#   high_52w_filt +50%: full Δ+0.318 / fwd Δ+0.347 (50~80 구간도 전부 양수 — 상향 조정 여지)
#   KIS 3전략·rsi_reversal 은 검증 결과 효과 없음/손해라 미적용.
# KRX 주문은 '당일 유효' + 가격제한폭(±30%) 내에서만 가능 → 한 번 걸어두기가 불가능.
# → 매일 아침(cmd_daily) 보유종목에 당일 지정가를 재발주(자가치유). 만기일 15:21 은
#   cmd_sell 이 미체결 지정가를 취소(kt10003) 후 시장가 청산(백스톱 유지).
PROFIT_TARGET = dict(_ACC["take"])   # 기본 {"rsi_vol": 0.20(옛 보유분용), "high_52w_filt": 0.50}
# [2026-10-21] 전략별 손절 — 15:21 매도 실행 때 평가손익률(브로커 매입평균 기준)이 이 값 이하면 마감 동시호가 매도.
# (서킷브레이커와 같은 판정 방식. KIS 는 '전일 종가 판정 → 다음날 시가'로 판정 시점이 다르다 — 설명서 §60)
STRATEGY_STOP = dict(_ACC["stop"])
# high_500d_filt 는 익절 미적용 — 검증 안 됨, 백테스트(star_high_500d_20)도 익절 없음
MIN_ORDER_AMOUNT = 100_000   # 슬롯당 이보다 작으면 주문 생략

# 재해성 손실 서킷브레이커(2026-07-10 사용자 결정) — 브로커 평가손익률(매입평균 기준)이
# 이 값 이하이면 만기 전이라도 15:21 cmd_sell 에서 시장가 청산.
# ※ 목적은 '평균수익 최적화'가 아니라 상폐/거래정지류 꼬리위험 보험. stop_sweep 백테스트는
#   무손절 우위였으나 (a) 풀이 레거시(h252/h500)라 안C 미대표 (b) 액면분할 무보정으로
#   딥레벨 가짜 발동(-40% 발동률 9~20%)이 섞여 판단 근거로 부적합 판정. 라이브 실측
#   3주간 1건(-51% JW신약) — 발동은 드물어야 정상이며 브로커 손익률은 분할 자동보정이라
#   가짜 발동 없음. None 으로 바꾸면 비활성.
CIRCUIT_BREAKER_PCT = -40.0

# 강도 필터(사용자 결정 2026-07-02 도입, 2026-07-17 재매핑): score_ic(0~10)가 이 값
# 미만인 신호는 매수 스킵. 신호 '기록'은 전량 유지, '집행'만 거른다.
# [2026-07-17 6.0→5.7] 강도 잣대 교체(DB피처 복구+라이브 6전략 IC)로 분포가 하향 이동
# — 6.0 은 옛 분포의 상위 ~25% 지점이었으나 새 분포에선 상위 ~7%가 되어 매수가 과도
# 축소됨. 실현수익 확정 946건 소급 비교에서 새 잣대의 75퍼센타일 = 5.68 → 5.7 로
# 재매핑해 '상위 25% 선별'이라는 검증된 원래 의도를 유지(새 잣대 변별력: 순위상관
# +0.082→+0.172, 상·하위25% 스프레드 +1.55→+3.45%p — 신 잣대가 2배 이상 우수).
# [2026-08-09 6.0→5.7 재조정] 8월 실측: 6.0 에서 5거래일 중 4일이 매수 0건, 신호 24건 중
# 2건만 통과해 계좌가 전액 현금이 됨(기회비용 과다). 5.7 로 복귀. ※ 단 8월 신호는 최대
# 강도가 5.63 이라 5.7 로도 즉시 매수가 재개되진 않는다 — 분포 자체가 하락 중
# (월평균 5.73→5.28→4.92, KOSPI 20일 −16.7% 급락장). 절대 임계의 구조적 한계이며
# 백분위 기준 전환을 검토 중.
# [2026-07-21 5.7→6.0(폐기)] "모의계좌는 강도 6 이상만 체결" 원칙을 문자 그대로
# 적용하기로 함(KIS 도 동일 6.0 신설 — 양 계좌 통일). 재매핑 근거(상위 25% 유지)보다
# '약속한 숫자'를 우선한다는 사용자 판단. 고지된 단점: 새 분포에서 6.0 이상은 상위 ~5%
# (7월 키움 신호 595건 중 28건)라 매수 빈도가 크게 줄고, 8월 초 예정 강도 재점검용
# forward 표본 축적도 느려짐. 강도 기록 없는 신호는 통과(fail-open) — 기록 실패로
# 매매가 전면 중단되는 것을 막기 위함(이 경로로는 6 미만도 통과 가능).
MIN_STRENGTH_SCORE = _ACC["min_strength"]     # 기본 5.7
STRENGTH_VERSION = _ACC["strength_version"]   # [2026-10-22] v1(공통) / v2(전략별) — 기본 v1
# [2026-10-23 §71] 전략별 강도 기준(대시보드에서 전략마다 고름, 비우면 계좌 기본값 MIN_STRENGTH_SCORE)
STRENGTH_TH = dict(_ACC["min_strength_by"])
# [2026-10-31 §91] 상한가 마감 신호 제외 — 설정에서 켠 전략(라이브 이름)은 신호일에 상한가(+29.5%↑)로 마감한 종목을 사지 않는다
SKIP_LIMITUP = set(_ACC.get("skip_limitup") or set())
# [§96.6] 본계좌 전략별 총액 상한(원, 라이브 이름) — 있으면 그 전략 예산 = (상한 − 이미 산 금액) ÷ 그 전략 남은 빈 칸,
# 없으면 종전처럼 주문가능금액 ÷ 남은 빈 칸. 모의는 항상 비어 있음.
STRATEGY_CAP = dict(_ACC.get("cap") or {}) if REAL else {}


def _th(strat):
    return STRENGTH_TH.get(strat, MIN_STRENGTH_SCORE)


# [2026-10-26 §75] 칸 방식 — "slots"(전략별 칸) | "pool"(칸 없음: 조건 넘은 신호를 전체 MAX_CONCURRENT 까지,
# 같은 날 후보는 전략 안 순위(그 전략 과거 강도 중 상위 몇 %) 높은 순). 대시보드 '모의매매 전략 설정'에서 고름.
SLOT_MODE = _ACC.get("slot_mode", "slots")


def _pool_order(cands, strength):
    """칸 없음 모드: 여러 전략 후보를 한 줄로 — 전략 안 순위(백분위) 높은 순, 점수 없는 후보(면제·무기록)는 맨 뒤."""
    import strength_model as _SM
    import live_config as _LCm

    def key(sig):
        st = str(sig.get("strategy", ""))
        sc = strength.get((str(sig.get("code", "")).zfill(6), st))
        try:
            p = _SM.rank_pct(_LCm.engine_name(st), st, sc, STRENGTH_VERSION)
        except Exception:
            p = None if sc is None else float(sc) * 10
        return (p is None, -(p or 0.0))
    return sorted(cands, key=key)



def _strength_gate(strat, sc):
    """주문 직전 강도 게이트 → (통과여부, 사유). 면제 전략은 강도와 무관하게 통과(kis_trader 와 동일 규약)."""
    if strat in STRENGTH_EXEMPT:
        return True, (f"강도 면제({sc:.2f})" if sc is not None else "강도 면제(무기록)")
    if sc is None:
        return False, "강도 확인 불가(재계산 실패) → 차단"
    th = _th(strat)
    if sc < th:
        return False, f"강도 {sc:.2f} < {th:g}"
    return True, f"강도 {sc:.2f} ≥ {th:g}"


# ------------------------------------------------------------
# 공용
# ------------------------------------------------------------
class KiwoomBundle:
    """order/account(+본계좌 시세 info) 클라이언트 묶음."""
    def __init__(self, order, acct, info=None):
        self.order = order
        self.acct = acct
        self.info = info     # [§96] 본계좌 지정가 매수용 현재가(ka10001)


_API_BUNDLE = None   # 프로세스 1회 인증 캐시 — 아래 get_api 토큰 재발급 방지(2026-07-21)


def get_api():
    # [2026-07-21] 한 실행에서 매도·매수·익절·상태조회가 각각 get_api() 를 호출해
    # 매번 새 토큰을 발급 → 키움 모의서버의 '토큰 발급' rate limit(HTTP 429)에 걸려
    # cmd_status 가 즉사(daily-am 크래시 실사례, 매매 자체는 이미 완료된 뒤였음).
    # 토큰은 실행 수초 동안 유효하므로 프로세스 생애 1회만 발급·재사용한다(각 스케줄
    # 실행은 새 프로세스라 만료 걱정 없음). 어제 고친 '주문 429'(_order_with_retry)와는
    # 다른 엔드포인트라 그 방어로는 못 잡던 경로.
    global _API_BUNDLE
    if _API_BUNDLE is not None:
        return _API_BUNDLE
    if REAL:
        # [§96] 본계좌 = 실서버 키(.env KIWOOM_PROD_APP_KEY — 앱키에 계좌가 묶여 있음). .env 의 KIWOOM_ENV 와 무관.
        _key = os.getenv("KIWOOM_PROD_APP_KEY", "").strip()
        _sec = os.getenv("KIWOOM_PROD_APP_SECRET", "").strip()
        _sandbox, _env = "false", "real"
        if not _key or not _sec:
            print("[ERROR] .env 에 KIWOOM_PROD_APP_KEY / KIWOOM_PROD_APP_SECRET 없음 — 본계좌 연결 불가")
            sys.exit(1)
    else:
        _key, _sec = config.KIWOOM_APP_KEY, config.KIWOOM_APP_SECRET
        _sandbox, _env = ("false" if config.KIWOOM_ENV == "prod" else "true"), config.KIWOOM_ENV
        if not _key or not _sec:
            print("[ERROR] .env 에 KIWOOM_MOCK_APP_KEY / KIWOOM_MOCK_APP_SECRET 없음")
            sys.exit(1)
    # 라이브러리가 import 시점에 환경변수를 읽으므로 import 전에 주입
    os.environ["KIWOOM_API_KEY"] = _key
    os.environ["KIWOOM_API_SECRET"] = _sec
    os.environ["KIWOOM_USE_SANDBOX"] = _sandbox
    try:
        from kiwoom_rest_api.config import get_base_url
        from kiwoom_rest_api.auth.token import TokenManager
        from kiwoom_rest_api.koreanstock.order import Order
        from kiwoom_rest_api.koreanstock.account import Account
        from kiwoom_rest_api.koreanstock.stockinfo import StockInfo
    except ImportError:
        print("[ERROR] kiwoom-rest-api 미설치 → pip install kiwoom-rest-api")
        sys.exit(1)
    base = get_base_url()
    tm = TokenManager()
    # 첫 발급도 429 재시도 — 모의서버 순단으로 최초 발급이 429 날 수 있음(미접수라 안전).
    for _i in range(3):
        try:
            tm.get_token()   # 토큰 발급 검증 (실패 시 예외)
            break
        except Exception as _e:
            if "429" in str(_e) and _i < 2:
                print(f"[kiwoom] 토큰 발급 429 — 재시도 {_i+1}/2 (3초 대기)")
                time.sleep(3.0)
                continue
            raise
    print(f"[kiwoom] 토큰 발급 OK (env={_env}, {base})")
    if REAL and "mockapi" in str(base):      # 실계좌 프로필이 모의 서버로 붙는 일은 없어야 한다(설정 꼬임 방어)
        print("[ABORT] 본계좌 프로필인데 모의 서버 주소 — 중단")
        sys.exit(1)
    _API_BUNDLE = KiwoomBundle(Order(base_url=base, token_manager=tm),
                               Account(base_url=base, token_manager=tm),
                               StockInfo(base_url=base, token_manager=tm))
    return _API_BUNDLE


def guard_mock_only():
    if REAL:
        return   # [§96] 본계좌 프로필은 모드(드라이런/실주문 — 사람이 켬)가 주문을 통제한다
    if config.KIWOOM_ENV == "prod":
        print("[ABORT] KIWOOM_ENV=prod — 실전 계좌 주문은 이 스크립트에서 차단됨.")
        print("        모의 검증 후 실전 전환은 별도로 진행하세요 (KIWOOM_ENV=mock 으로 변경).")
        sys.exit(1)


def _assert_order_ok(r, what):
    """키움 REST '동기' 응답의 성공 검증 — 주문 거부 침묵 방지(2026-07-11, critical).

    kiwoom-rest-api 의 동기 process_response 는 HTTP 200 이면 body 의 return_code 를
    검사하지 않고 dict 를 그대로 반환한다(비동기 경로만 return_code!=0 → APIError 승격).
    그래서 '거부된 주문'(증거금 부족 등)이 예외 없이 돌아와 ok=True 로 기록되고 —
    유령 원장 행 + 멱등키 오염(당일 재시도 차단) + 허위 체결 알림 + 매도 거부 시
    원장 삭제(청산 지연)로 번진다. 여기서 return_code!=0 을 예외로 승격한다."""
    if isinstance(r, dict):
        rc = r.get("return_code")
        if rc is not None and str(rc) != "0":
            raise RuntimeError(f"{what} 거부: return_code={rc} {r.get('return_msg', '')}")
    return r


def _order_with_retry(fn, what, retries=2, wait=1.5):
    """주문 호출 래퍼 — 초당 주문한도(HTTP 429) 거부 시 대기 후 재시도(2026-07-14).

    실사례: 07-13 15:21 서킷브레이커 3건 연속 발사 중 1건만 성공, 2건이 HTTP 429 로
    거부돼 재해성 손실 종목(-49%)의 청산이 하루 밀림. 429 는 '주문 미접수' 거부라
    재시도해도 중복 체결 위험이 없다(그 외 예외는 재시도 금지 — 중복 주문 위험)."""
    last = None
    refreshed = False
    for i in range(retries + 1):
        try:
            return _assert_order_ok(fn(), what)
        except Exception as e:
            last = e
            if "429" in str(e) and i < retries:
                print(f"    [재시도] {what} 초당한도(429) — {wait * (i + 1):.1f}초 대기 후 {i + 2}/{retries + 1}")
                time.sleep(wait * (i + 1))
                continue
            # [§96] 인증 실패(8005 토큰 무효) — 같은 앱키로 다른 프로그램이 토큰을 재발급하면 이전 토큰이 죽는다
            # (Stock_AI kiwoom_api 2026-09-08 실측). 인증에서 거절된 주문은 접수되지 않은 것이라 토큰을 새로 받아
            # 한 번만 재시도해도 중복 주문이 없다(접수됐다면 return_code 0 이라 여기 오지 않음).
            if not refreshed and _is_auth_error(e) and _API_BUNDLE is not None:
                refreshed = True
                print(f"    [재시도] {what} 인증 실패(토큰 무효 의심) — 토큰을 새로 받아 1회 재시도")
                try:
                    _API_BUNDLE.order.token_manager._access_token = None
                except Exception:
                    pass
                continue
            raise
    raise last


def _is_auth_error(e):
    """키움 인증 계열 오류(본문 return_code=3 · 8005/8050 · Token/토큰 문구) — Stock_AI kiwoom_api 와 같은 판정."""
    m = str(e)
    return "return_code=3 " in m or "8005" in m or "8050" in m or "Token" in m or "토큰" in m


def _norm_stk_code(v):
    """키움 잔고 종목코드 정규화 — 접두어 'A'(A005930) 만 제거.

    [2026-07-11, critical] 기존 replace('A','') 는 모든 위치의 A 를 지워 알파벳 포함
    신형 코드를 파괴('0001A0'→'000010'): 중복매수 가드 무력화 + 만기매도 불능 +
    원장 오염. 실데이터에 0001A0(덕양에너젠) 신호 8건 실재."""
    s = str(v or "").strip()
    s = _re.sub(r"^A(?=\d)", "", s)
    return s.zfill(6)


def _pick(d, *cands, default=None):
    """응답 dict 에서 후보 키 중 존재하는 첫 값을 반환 (스키마 방어)."""
    for k in cands:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _to_int(v, default=0):
    try:
        return int(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return default


def _tick_floor(p):
    """KRX 호가단위로 내림 — 지정가 유효가격(2023 코스피/코스닥 통합 호가체계)."""
    p = float(p)
    if p < 2_000:    t = 1
    elif p < 5_000:  t = 5
    elif p < 20_000: t = 10
    elif p < 50_000: t = 50
    elif p < 200_000: t = 100
    elif p < 500_000: t = 500
    else:            t = 1_000
    return int(p // t * t)


def _tick_ceil(p):
    """[§96] KRX 호가단위로 올림 — 본계좌 지정가 매수가(가격대 경계값은 위 단위의 배수라 그대로 유효)."""
    import math
    p = float(p)
    t = 1 if p < 2_000 else 5 if p < 5_000 else 10 if p < 20_000 else 50 if p < 50_000 \
        else 100 if p < 200_000 else 500 if p < 500_000 else 1_000
    return int(math.ceil(p / t - 1e-9) * t)


# [§96] 본계좌 매수 = '현재가 +2% 지정가' — 시장가는 증거금을 상한가로 잡거나(100% 계좌) 갭 체결로 미수가 날 수 있어
# 실계좌에선 쓰지 않는다. +2% 안에서는 즉시 체결(그때 매도호가)되고, 수량은 지정가 기준이라 현금을 넘지 않는다.
REAL_BUY_SLIP = 0.02
REAL_BUDGET_PAD = 0.995      # 수수료·반올림 여유 — 마지막 칸이 현금 전부를 쓸 때도 1원도 넘지 않게


def _real_limit_price(cur, upper=0):
    """현재가 → 지정가(호가 올림, 상한가 넘지 않게)."""
    px = _tick_ceil(float(cur) * (1 + REAL_BUY_SLIP))
    if upper and px > upper:
        px = int(upper)
    return px


def _quote(api, code):
    """[§96] 본계좌 현재가·상한가(ka10001). 실패하면 None — 가격을 모르면 사지 않는다."""
    try:
        r = _assert_order_ok(api.info.basic_stock_information_request_ka10001(stock_code=str(code).zfill(6)),
                             "현재가 조회")
        cur = abs(_to_int(_pick(r, "cur_prc", default=0)))
        upl = abs(_to_int(_pick(r, "upl_pric", default=0)))
        return {"cur": cur, "upper": upl} if cur > 0 else None
    except Exception as e:
        print(f"    [warn] {code} 현재가 조회 실패: {str(e)[:120]}")
        return None


def _to_float(v, default=0.0):
    try:
        return round(float(str(v).replace(",", "").replace("+", "").strip()), 2)
    except Exception:
        return default


def log_order(row):
    os.makedirs(ORDERS_DIR, exist_ok=True)
    path = f"{ORDERS_DIR}/orders_{datetime.today():%Y%m%d}.csv"
    exists = os.path.exists(path)
    fields = ["time", "side", "code", "name", "strategy", "qty", "price",
              "order_type", "ok", "order_no", "msg"]
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def _dry_record(side, code, name, strategy, qty, price, msg=""):
    """[§96 본계좌 드라이런] 실제 주문 대신 '냈을 주문'만 기록 — dryrun_YYYYMMDD.csv(주문로그·원장과 분리) + 텔레그램 묶음."""
    os.makedirs(ORDERS_DIR, exist_ok=True)
    path = f"{ORDERS_DIR}/dryrun_{datetime.today():%Y%m%d}.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["time", "side", "code", "name", "strategy", "qty", "price", "msg"])
        if new:
            w.writeheader()
        w.writerow({"time": datetime.now().strftime("%H:%M:%S"), "side": side, "code": code, "name": name,
                    "strategy": strategy, "qty": qty, "price": int(price or 0), "msg": msg})
    print(f"    [드라이런] {side} {code} {name} {qty}주 @ {int(price or 0):,} — 실제 주문 안 함 ({msg})")
    if side in ("buy", "sell"):
        try:
            import notifier
            notifier.queue_fill(side, name, code, qty, price)
        except Exception:
            pass


def _inflight(side, code, qty, price):
    """[§96 본계좌 실주문] 주문 전송 '직전' 기록 — 전송 뒤 기록하다 죽어도(엑셀이 CSV 를 잡고 있는 등) 재시도가
    같은 종목을 또 주문하지 않게 today_ordered_codes 가 이 파일도 본다. 기록이 실패하면 주문도 보내지 않는다(예외 전파)."""
    if not REAL:
        return
    os.makedirs(ORDERS_DIR, exist_ok=True)
    path = f"{ORDERS_DIR}/inflight_{datetime.today():%Y%m%d}.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "side", "code", "qty", "price"])
        w.writerow([datetime.now().strftime("%H:%M:%S"), side, str(code).zfill(6), int(qty), int(price or 0)])


def _inflight_codes(side=None):
    path = f"{ORDERS_DIR}/inflight_{datetime.today():%Y%m%d}.csv"
    if not REAL or not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, dtype={"code": str})
        if side:
            df = df[df["side"] == side]
        return set(df["code"].astype(str).str.zfill(6))
    except Exception as e:
        # 읽을 수 없으면 '이미 주문했을 수도' — 본계좌는 매수를 멈추는 쪽(호출부가 처리하도록 표식)
        print(f"[warn] 전송 기록 파싱 실패: {e}")
        return {"__unreadable__"}


# ─── 슬롯 관리 헬퍼 ──────────────────────────────────────────────────────────

def get_signal_strategy_map():
    """paper_signals.csv 에서 {code → strategy} 맵 반환 (최신 신호 기준).

    같은 code 가 여러 전략에 있으면 signal_date 최신 것을 우선.
    """
    if not os.path.exists(SIGNALS_CSV):
        return {}
    try:
        s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
        s["code"] = s["code"].astype(str).str.zfill(6)
        if "strategy" not in s.columns:
            return {c: "high_52w_filt" for c in s["code"].unique()}
        s = s.sort_values("signal_date", ascending=False)
        s = s.drop_duplicates("code", keep="first")
        return dict(zip(s["code"], s["strategy"]))
    except Exception:
        return {}


def count_slots_by_strategy(pos_codes, strategy_map):
    """보유 종목 코드 목록 → 전략별 현재 슬롯 사용 수 dict.

    구버전 신호(for_high20_mkt 등 현재 슬롯에 없는 전략)는
    총 슬롯 카운트(MAX_CONCURRENT)에만 포함시켜 새 매수를 억제.
    주력 슬롯에 오집계하지 않도록 legacy 키를 분리.
    """
    counts = {k: 0 for k in STRATEGY_MAX_SLOTS}
    counts["_legacy"] = 0   # 제거된 전략 보유 종목 집계용 (슬롯 배정에는 불포함)
    for code in pos_codes:
        strat = strategy_map.get(str(code).zfill(6), "high_52w_filt")
        if strat in STRATEGY_MAX_SLOTS:
            counts[strat] += 1
        else:
            counts["_legacy"] += 1   # for_high20_mkt 등 레거시 보유 → 별도 집계
    return counts


def today_ordered_codes(side=None):
    """오늘 이 side 로 주문한 종목(멱등키). [§96] 본계좌는 '전송 직전 기록(inflight)'도 포함 — 로그 쓰기 실패 뒤
    재시도가 같은 종목을 또 주문하지 않게(이 경우 '__unreadable__' 표식이 섞이면 호출부가 그 동작을 멈춘다)."""
    codes = _today_logged_codes(side)
    if REAL:
        codes |= _inflight_codes(side)
    return codes


def _today_logged_codes(side=None):
    path = f"{ORDERS_DIR}/orders_{datetime.today():%Y%m%d}.csv"
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, dtype={"code": str})
        if side:
            df = df[df["side"] == side]
        ok = df["ok"].astype(str).str.lower().isin(("true", "1"))  # CSV 재로드 시 문자열 대응
        return set(df[ok]["code"].astype(str).str.zfill(6))
    except Exception as e:
        # 0바이트/손상 주문로그 1개로 당일 매수·매도 전체가 크래시하지 않게(KIS 판과 동일).
        # 단 멱등키가 꺼진 채 진행되므로 침묵하지 않고 텔레그램으로 알린다.
        print(f"[warn] 주문로그 파싱 실패(멱등키 비활성 — 중복주문 주의): {e}")
        try:
            import notifier
            notifier.safe_send(f"⚠ [{TAG}] 오늘 주문로그 손상 — 중복주문 가드 비활성 상태로 진행: {str(e)[:80]}")
        except Exception:
            pass
        return {"__unreadable__"} if REAL else set()     # [§96] 본계좌는 '모르면 주문 안 함'


# ------------------------------------------------------------
# 계좌 조회
# ------------------------------------------------------------
def get_deposit(api):
    """주문가능 예수금 (원). 스키마 모르면 raw 출력. (kt00001, qry_tp 3=추정조회)"""
    try:
        r = api.acct.deposit_detail_status_request_kt00001(qry_tp="3")
    except Exception:
        r = api.acct.deposit_detail_status_request_kt00001(qry_tp="2")
    if REAL:
        return _real_orderable(r)
    for key in ("ord_alow_amt", "ord_alowa", "100stk_ord_alow_amt",
                "entr", "prsm_dpst_aset_amt", "pymn_alow_amt"):
        v = _pick(r, key)
        if v is not None:
            return _to_int(v)
    print(f"[warn] 예수금 필드 식별 실패 — raw keys: {list(r.keys())[:25]}")
    return 0


_REAL_CASH = {"uncl": 0, "basis": ""}     # [§96] 마지막 예수금 조회의 미수금·기준(대시보드·매수 차단용)


def _real_orderable(r):
    """[§96] 본계좌 주문가능금액 = '100% 종목 주문가능금액'(현금만 — 미수를 쓰지 않는 금액).
    ord_alow_amt 는 증거금률에 따라 현금보다 클 수 있어(미수 가능) 쓰지 않는다. 100% 값이 없으면
    주문가능·D+2 추정예수금·예수금 중 가장 작은 값. 현금 미수금이 있으면 기록해 두고 매수를 막게 한다.
    응답이 오류(return_code≠0, 예: 토큰 무효)면 예외 — 0원으로 착각해 조용히 안 사는 일을 막고 재시도에 맡긴다."""
    _assert_order_ok(r, "예수금 조회")
    v = _pick(r, "100stk_ord_alow_amt")
    if v is not None:
        amt, basis = _to_int(v), "100% 종목 주문가능금액"
    else:
        c = [_to_int(_pick(r, k)) for k in ("ord_alow_amt", "d2_entra", "entr") if _pick(r, k) is not None]
        amt, basis = (min(c) if c else 0), "주문가능·D+2·예수금 중 최소(100% 값 없음)"
        print(f"[warn] 100% 종목 주문가능금액 필드 없음 — {basis} 사용. raw keys: {list(r.keys())[:25]}")
    _REAL_CASH["uncl"] = max(_to_int(_pick(r, "ch_uncla_tot", default=0)), _to_int(_pick(r, "ch_uncla", default=0)))
    _REAL_CASH["basis"] = basis
    return max(0, amt)


def get_positions(api):
    """보유 종목 dict: code -> {qty, name, ...}. (kt00018) 스키마 방어적 파싱.

    [§96] 연속조회(cont-yn/next-key) — 보유가 한 번에 다 안 오면 이어 받는다. 본계좌엔 직접 산 종목이 섞여 많을 수
    있고, 목록이 잘리면 '잘린 뒤쪽 종목 = 팔렸다'로 오판한다. 둘째 쪽부터 응답이 이상하면 예외(부분 목록으로 판단 금지)."""
    pos = {}
    cont, nkey = "N", ""
    for page in range(20):
        r = api.acct.account_evaluation_balance_detail_request_kt00018(
            query_type="1", domestic_exchange_type="KRX", cont_yn=cont, next_key=nkey)
        items = None
        for key in ("acnt_evlt_remn_indv_tot", "stk_acnt_evlt_prst", "output", "list"):
            if isinstance(r, dict) and isinstance(r.get(key), list):
                items = r[key]
                break
        if items is None:
            if page or REAL:   # [§96] 본계좌는 잔고를 모르면 진행하지 않는다(빈 잔고로 착각하면 직접 산 종목을 또 사거나 칸을 잘못 셈)
                raise RuntimeError(f"잔고 조회 {page + 1}쪽 응답 이상(return_code={r.get('return_code')} "
                                   f"{str(r.get('return_msg', ''))[:80]}) — 부분·빈 목록으로 판단하지 않음")
            print(f"[warn] 잔고 리스트 식별 실패 — raw keys: {list(r.keys())[:20]}")
            return {}
        for it in items:
            code = _norm_stk_code(_pick(it, "stk_cd", "stock_code", default=""))
            qty = _to_int(_pick(it, "rmnd_qty", "hldg_qty", "qty", default=0))
            name = _pick(it, "stk_nm", "stock_name", default="")
            if code and qty > 0:
                # 현재가/매입가는 kiwoom 이 방향부호(+/-)를 붙여 줄 때가 있어 abs 로 정규화.
                # 평가손익/수익률은 부호 유지.
                pos[code] = {
                    "qty": qty, "name": name,
                    "price":     abs(_to_int(_pick(it, "cur_prc", "prpr", default=0))),
                    "avg_price": abs(_to_int(_pick(it, "pur_pric", "pchs_avg_pric", "buy_uv", default=0))),
                    "evlt_amt":  abs(_to_int(_pick(it, "evlt_amt", "evlu_amt", default=0))),
                    "pnl":       _to_int(_pick(it, "evltv_prft", "evlt_pl", "evlu_pfls_amt", default=0)),
                    "pnl_pct":   _to_float(_pick(it, "prft_rt", "pl_rt", "evlu_pfls_rt", default=0)),
                    "pred_close": abs(_to_int(_pick(it, "pred_close_pric", default=0))),   # [§96] 일일 손실 판정 기준
                }
        c = str(r.get("cont-yn") or "").strip().upper()
        k = str(r.get("next-key") or "").strip()
        if c != "Y" or not k:
            break
        cont, nkey = "Y", k
        time.sleep(0.3)
    return pos


def _snapshot_codes():
    """직전 snapshot.json 의 보유 종목코드 집합 — 잔고 정합성 비교 기준.
    (키움은 KIS 의 kis_positions.csv 같은 독립 진입추적 원장이 없어 스냅샷을 기준으로 쓴다.)
    """
    try:
        d = json.load(open(f"{ORDERS_DIR}/snapshot.json", encoding="utf-8"))
        return set(str(x.get("code", "")).zfill(6) for x in d.get("positions", []) if x.get("code"))
    except Exception:
        return set()


def cmd_reconcile():
    """잔고 정합성 점검 — 브로커 보유 vs 직전 스냅샷(+진입가 원장). 불일치 시 텔레그램 경고."""
    api = get_api()
    pos = get_positions(api)
    broker = set(str(c).zfill(6) for c in pos.keys())
    ledger = set(load_kiwoom_positions().keys())
    if ledger:
        lo = sorted(broker - ledger)   # 브로커엔 있는데 원장에 없음(ensure 가 자가치유)
        ls = sorted(ledger - broker)   # 원장엔 있는데 브로커에 없음(수동매도/체결 미반영?)
        print(f"[reconcile/원장] 브로커만: {lo or '없음'} / 원장만: {ls or '없음'}")
    snap = _snapshot_codes()
    drift_out = sorted(broker - snap)   # 브로커엔 있는데 스냅샷에 없음
    drift_in = sorted(snap - broker)    # 스냅샷엔 있는데 브로커 없음
    print(f"[reconcile] 브로커 보유 {len(pos)}건 / 스냅샷대비 추가{drift_out} 누락{drift_in}")
    if not pos and snap:
        msg = f"⛔ [{TAG} reconcile] 브로커 잔고 0 인데 직전 스냅샷엔 보유 있음 — API 누락 의심"
        print(msg)
        try:
            import notifier; notifier.safe_send(msg)
        except Exception:
            pass
        return False
    if drift_out or drift_in:
        try:
            import notifier
            notifier.safe_send(f"⚠ [{TAG} 잔고] 스냅샷 대비 변동 추가{drift_out}/누락{drift_in} — 확인 권장")
        except Exception:
            pass
    return True


def cmd_status():
    api = get_api()
    dep = get_deposit(api)
    pos = get_positions(api)
    ensure_kiwoom_ledger(pos)   # 진입가 원장 자가치유(최초 시드 포함)
    _log_slippage(pos)          # 실체결 슬리피지 누적(매입평균 vs 신호종가)
    securities = sum(p.get("evlt_amt", 0) for p in pos.values())   # 보유 평가금액 합
    eval_pnl   = sum(p.get("pnl", 0) for p in pos.values())        # 평가손익 합
    total_eval = dep + securities                                  # 총평가 = 예수금 + 보유평가
    print(f"\n[총평가금액] {total_eval:,} 원  (예수금 {dep:,} + 보유평가 {securities:,})")
    print(f"[평가손익] {eval_pnl:+,} 원")
    print(f"[보유 종목] {len(pos)} / {MAX_CONCURRENT} 슬롯")
    for c, p in pos.items():
        print(f"  {c} {p['name']}: {p['qty']:,}주  현재 {p.get('price',0):,}  손익 {p.get('pnl',0):+,}({p.get('pnl_pct',0):+.2f}%)")

    # 대시보드용 스냅샷 저장
    try:
        import json
        os.makedirs(ORDERS_DIR, exist_ok=True)
        snap = {"date": datetime.today().strftime("%Y%m%d"),
                "time": datetime.now().strftime("%H:%M"),
                "env": "real" if REAL else config.KIWOOM_ENV,
                "deposit": dep,
                "total_eval": total_eval, "securities": securities, "eval_pnl": eval_pnl,
                "positions": [{"code": c, "name": p["name"], "qty": p["qty"],
                               "price": p.get("price", 0), "avg_price": p.get("avg_price", 0),
                               "pnl": p.get("pnl", 0), "pnl_pct": p.get("pnl_pct", 0)}
                              for c, p in pos.items()]}
        if REAL:   # [§96] 어떤 종목이 프로그램 몫인지·오늘 손익·주문가능금액 기준을 함께 남긴다(대시보드 본계좌 탭)
            own = _own(pos)
            for x in snap["positions"]:
                x["bot_qty"] = int(own.get(x["code"], {}).get("qty", 0))
                x["bot"] = x["bot_qty"] > 0
            pnl, n = _RA.day_pnl(own, today_ordered_codes("buy"), _prev_close_map())
            snap.update(profile="real", mode=MODE, deposit_basis=_REAL_CASH["basis"], uncl=_REAL_CASH["uncl"],
                        own_n=len(own), day_pnl=pnl,
                        own_eval=int(sum(_to_int(p.get("qty", 0)) * float(p.get("price", 0) or 0) for p in own.values())))
        with open(f"{ORDERS_DIR}/snapshot.json", "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        hist = f"{ORDERS_DIR}/equity_history.csv"
        new = not os.path.exists(hist)
        with open(hist, "a", newline="", encoding="utf-8-sig") as f:
            if new:
                f.write("date,time,deposit,n_positions\n")
            f.write(f"{snap['date']},{snap['time']},{dep},{len(pos)}\n")
    except Exception as e:
        print(f"[warn] 스냅샷 저장 실패: {e}")
    try:
        unfilled = api.acct.unfilled_orders_request_ka10075(
            all_stk_tp="0", trde_tp="0", stex_tp="0")
        n = len(unfilled.get("oso", unfilled.get("output", []))) if isinstance(unfilled, dict) else 0
        print(f"[미체결] {n} 건")
    except Exception as e:
        print(f"[미체결 조회 실패] {e}")


# ------------------------------------------------------------
# 매수 — 오늘 신호
# ------------------------------------------------------------
def latest_macro_date():
    """macro_data 의 최신 영업일 (09:01 실행 시 = 어제 = 신호일)."""
    import glob as _g
    files = sorted(_g.glob("./macro_data/daily/*.csv"))
    return os.path.basename(files[-1])[:-4] if files else None


def _strength_map(sigs):
    """db/signal_strength_log.csv → {(code, strategy): score_ic} (해당 신호일·키움 계좌만).

    강도 필터(MIN_STRENGTH_SCORE)용. 파일 없음/파싱 실패 시 빈 dict(=필터 미적용, fail-open).
    """
    path = "./db/signal_strength_log.csv"
    if not os.path.exists(path) or not sigs:
        return {}
    try:
        dates = {str(s.get("signal_date", "")).replace("-", "") for s in sigs}
        df = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
        df["signal_date"] = df["signal_date"].astype(str).str.replace("-", "")
        df = df[df["signal_date"].isin(dates)]
        if "account" in df.columns:   # 키움 계좌 기록만 (KIS 기록과 혼동 방지) — [§96] 본계좌는 자기 기록(real_kiwoom)만
            _acc = df["account"].astype(str).str.lower()
            df = df[(_acc == STRENGTH_ACCOUNT) if REAL else _acc.str.startswith("kiwoom")]
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["score_ic"] = pd.to_numeric(df["score_ic"], errors="coerce")
        # [2026-10-22] 강도 방식(설정): v2 면 전략별 점수(score_v2)를 쓰고, v2 모델이 없는 전략만 v1 로 대체
        if STRENGTH_VERSION == "v2" and "score_v2" in df.columns:
            _v2 = pd.to_numeric(df["score_v2"], errors="coerce")
            df["score_ic"] = _v2.where(_v2.notna(), df["score_ic"])
        out = {}
        for _, r in df.iterrows():
            if pd.notna(r["score_ic"]):
                out[(str(r["code"]), str(r.get("strategy", "")))] = float(r["score_ic"])
        return out
    except Exception as e:
        print(f"[warn] 강도 로그 로드 실패(필터 미적용): {e}")
        return {}


def verify_strength(sigs, strength, account=STRENGTH_ACCOUNT):
    """[매매 전 사전점검] 강도 재확인 — 후보 전원의 score_ic 가 확보됐는지 검증한다.

    (2026-07-21 사용자 요청 "매매 시행 전 강도 재확인 후 매매")
    무기록 후보가 있으면 그 자리에서 strength_logger 로 즉석 재계산한다(실측 6~10초 —
    로그 기록까지 되므로 대시보드/감사와도 정합). 재계산 후에도 없으면 호출부가 차단
    (fail-closed) — 종전엔 무기록이 무필터 통과(fail-open)라 6 미만 체결의 잔여 경로였음.

    반환: (갱신된 strength map, 여전히 무기록인 {(code, strategy)} 집합)
    """
    need = {(str(s.get("code", "")).zfill(6), str(s.get("strategy", ""))) for s in sigs}
    missing = {k for k in need if k not in strength}
    if not missing:
        print(f"[verify] 강도 재확인 OK — 후보 {len(need)}건 전원 기록 확보")
        return strength, set()

    print(f"[verify] 강도 무기록 {len(missing)}건 — 즉석 재계산 시도(약 10초)")
    try:
        from strategies.daily_loader import load_macro_daily
        import strength_logger
        df = load_macro_daily()
        last_date = str(sigs[0].get("signal_date", "")).replace("-", "")
        strength_logger.log_strength(account, df, last_date, sigs, verbose=False)
        strength = _strength_map(sigs)      # 기록 후 재조회
    except Exception as e:
        print(f"[verify] 강도 재계산 실패: {e}")

    still = {k for k in need if k not in strength}
    if still:
        # [2026-08-09 문구 개선] 이건 '고장'이 아니라 안전장치가 의도대로 작동한 것 —
        # 종전 문구("재확인 실패…점검 요망")가 사고처럼 읽혀 불필요한 불안을 줬다.
        # 무슨 일이 있었고 조치가 필요한지를 분명히 쓴다.
        msg = (f"ℹ️ [{account}] 강도 확인 안 된 후보 {len(still)}건은 안전하게 매수 제외했습니다"
               f"(나머지 후보는 정상 진행). 매매 이상 아님 — 며칠 반복되면 강도 로깅 점검")
        print(f"[verify] {msg}")
        try:
            import notifier
            notifier.safe_send(msg)
        except Exception:
            pass
    else:
        print(f"[verify] 강도 재계산 완료 — 후보 {len(need)}건 전원 확보")
    return strength, still


def todays_signals():
    """원본 모드: '직전 영업일 신호'를 다음날 아침 매수 — 최신 macro 일자의 신호.

    Returns list of dicts; each dict contains 'strategy' and 'holding_days'.
    """
    if not os.path.exists(SIGNALS_CSV):
        return []
    s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    s["signal_date"] = s["signal_date"].astype(str)
    s["code"] = s["code"].astype(str).str.zfill(6)
    # v2 schema 호환
    if "strategy" not in s.columns:
        s["strategy"] = "high_52w_filt"
    if "holding_days" not in s.columns:
        s["holding_days"] = HOLDING_DAYS_DEFAULT
    s["holding_days"] = pd.to_numeric(s["holding_days"], errors="coerce").fillna(HOLDING_DAYS_DEFAULT).astype(int)

    target_raw = latest_macro_date()   # "YYYYMMDD" (파일명 기반)
    if not target_raw:
        return []
    # [fix 2026-06-23] CSV signal_date 는 'YYYYMMDD'(대시 없음)인데, 과거엔 target 을
    #   'YYYY-MM-DD'(대시)로 바꿔 비교 → 항상 0건 매칭 → 매수가 한 번도 안 나가던 버그.
    #   양쪽 대시 제거 후 비교(형식 무관 안전).
    s["signal_date"] = s["signal_date"].str.replace("-", "", regex=False)
    target = target_raw.replace("-", "")
    today_raw = datetime.today().strftime("%Y%m%d")
    if target_raw == today_raw:
        return []
    print(f"[buy] 신호 기준일: {target}")
    if not _signals_fresh(target_raw, LABEL):
        return []
    return s[s["signal_date"] == target].to_dict("records")


def _signals_fresh(latest_date, label):
    """유니버스 신선도 게이트. 밀렸으면 알리고, 심하면 매수를 막는다(2026-08-20).

    latest_macro_date() 는 '폴더의 마지막 파일'일 뿐이라 수집이 밀리면 며칠 묵은
    신호로 조용히 매수하게 된다. 상세 근거는 market_calendar 모듈 독스트링 참조.
    판정 자체가 실패하면 종전대로 진행한다(fail-open — 달력 오류로 매매를 멈추지 않음).
    """
    try:
        import market_calendar as mc
        allowed, msg = mc.check_signal_freshness(latest_date, label)
    except Exception as e:
        print(f"[buy][warn] 유니버스 신선도 판정 생략({type(e).__name__}: {e})")
        return True
    if msg:
        print(msg)
        try:
            import notifier
            notifier.safe_send_once(msg, f"stale:{label}:{latest_date}")   # 예열·매매·재시도마다 같은 경보 반복 방지
        except Exception:
            pass
    if not allowed:                     # [§81] 13:00 자동 복구(recover_stale.py)가 이 기록을 보고 다시 산다
        try:
            mc.mark_stale_skip(label, latest_date)
        except Exception:
            pass
    return allowed



# ── 일일 후보수 게이트 (2026-08-27 신설, **모니터 전용**) ─────────────────────
# 발견: 슬롯 10개 제약이 비대칭으로 작동한다. 후보가 적은 날(약한 국면)은 전부 사고
#   후보가 많은 날(강한 국면)은 10건만 산다 → 나쁜 날에 과대 가중된다.
#   실측: 강도 >=5.7 신호의 per-trade 평균은 +1.72% 인데, 슬롯 10으로 실제로 살 수
#   있는 것만 보면 -0.75% 다. 모의계좌 실적 -8.4% 와 일관된다.
# 대책 후보: "그날 >=5.7 후보가 N건 미만이면 아예 사지 않는다."
#   워크포워드(임계를 이전 연도로만 선택, 2021~2026 OOS): +1.30% vs 무게이트 -0.71%
#   = +2.01%p. N>=12 는 8년 전부에서 무게이트를 이겼고 최악 연도에도 지지 않았다.
# 다만 매수 가능일이 연 120일 -> 25일로 급감하므로, 손절/익절 때와 동일한 절차를 따른다:
#   Phase 1 **모니터 전용 2~4주**(차단하지 않고 '걸렸을 것'만 기록) -> Phase 2 상수 1줄로 활성화.
GATE_MIN_CANDIDATES = 12      # 후보가 이 수 미만이면 '게이트 걸림'으로 기록
GATE_ENFORCE = False          # True 로 바꾸면 실제 차단. Phase 1 동안 False 유지.
GATE_LOG = f"{ORDERS_DIR}/gate_monitor.csv" if REAL else "./db/gate_monitor.csv"   # [§96] 본계좌는 따로


def _gate_check(n_pass: int, n_signals: int) -> bool:
    """후보수 게이트 판정 + 기록. 반환: 매수 진행 여부(모니터 모드면 항상 True).

    n_pass: 그날 강도 임계를 통과한 후보 수(중복 종목 제거 전 기준 — 라이브와 동일)
    """
    blocked = n_pass < GATE_MIN_CANDIDATES
    mode = "ENFORCE" if GATE_ENFORCE else "MONITOR"
    if blocked:
        print(f"[gate][{mode}] 오늘 강도통과 후보 {n_pass}건 < 기준 {GATE_MIN_CANDIDATES}건 "
              + ("— 신규매수 생략" if GATE_ENFORCE else "— 기록만(차단 안 함)"))
    else:
        print(f"[gate][{mode}] 오늘 강도통과 후보 {n_pass}건 ≥ 기준 {GATE_MIN_CANDIDATES}건 — 통과")
    try:
        import csv as _csv
        new = not os.path.exists(GATE_LOG) or os.path.getsize(GATE_LOG) == 0
        os.makedirs(os.path.dirname(GATE_LOG), exist_ok=True)
        with open(GATE_LOG, "a", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            if new:
                w.writerow(["date", "time", "n_signals", "n_pass",
                            "threshold", "blocked", "enforced"])
            w.writerow([datetime.now().strftime("%Y%m%d"),
                        datetime.now().strftime("%H:%M:%S"),
                        n_signals, n_pass, GATE_MIN_CANDIDATES,
                        int(blocked), int(GATE_ENFORCE)])
    except Exception as e:
        print(f"[gate][warn] 기록 실패(무시): {e}")
    return (not blocked) if GATE_ENFORCE else True


def _alert_buy_failures(failures, n_placed):
    """매수 주문 실패 텔레그램 통보 (2026-09-16 신설).

    매도 실패(_기존 1591행_)만 알리고 매수 실패는 [실패] 한 줄이던 비대칭을 없앤다.
    **시도한 주문이 전부 실패했으면 계좌 단위 사고로 의심**한다 — 종목 사유(증거금
    부족 등)는 보통 일부만 실패하지만, 계좌가 막히면 예외 없이 전부 막힌다.
    """
    lines = [f"  {c} {n} — {m}" for c, n, m in failures[:5]]
    if len(failures) > 5:
        lines.append(f"  … 외 {len(failures) - 5}건")
    all_failed = (n_placed == 0)
    head = (f"🚨 [{LABEL}] 매수 주문 {len(failures)}건 **전부 실패** — 계좌 단위 사고 의심"
            if all_failed else
            f"⚠ [{LABEL}] 매수 주문 {len(failures)}건 실패 (성공 {n_placed}건)")
    tail = (("\n  강도 임계를 통과한 종목을 **살 수 없는 상태**입니다. "
             + ("본계좌 주문가능금액·주문 제한·앱키(실서버) 상태를 키움 HTS/앱에서 확인하세요." if REAL else
                "모의계좌 기간만료·재발급·계좌성격 변경을 확인하세요.")) if all_failed else "")
    print(f"⚠ [{TAG}] 매수 실패 {len(failures)}건")
    try:
        import notifier
        notifier.safe_send(head + "\n" + "\n".join(lines) + tail)
    except Exception:
        pass


def _real_buy_allowed():
    """[§96] 본계좌 신규 매수 관문 — 본계좌 정지(손실 한도·수동·상태파일 이상) / 설정 파일 문제 / 전체 정지 스위치.
    막혀도 프로세스를 끝내지 않는다(익절 지정가·잔고 스냅샷은 그대로 — 모의의 guard_buy 는 exit 0)."""
    on, st = _RA.halt_status()
    if on:
        print(f"[buy] 본계좌 신규 매수 정지 중 — {(st or {}).get('reason_text', '?')} {(st or {}).get('detail', '')} → 매수 생략(매도는 계속)")
        return False
    if _ACC.get("fallback"):
        msg = (f"⛔ [{LABEL}] 설정 파일 문제로 기본값이 대신 읽혔습니다({_ACC['fallback'][:120]}) — "
               "본계좌는 고른 적 없는 설정으로 사지 않습니다. 대시보드 설정을 확인·저장하세요(매도는 계속).")
        print(msg)
        try:
            import notifier
            notifier.safe_send_once(msg, f"real_cfg_fallback:{datetime.now():%Y%m%d}")
        except Exception:
            pass
        return False
    on, st, err = kill_switch.status()
    if on:
        print(f"[buy] 전체 정지 스위치 작동 중({(st or {}).get('reason_text', '?')}) — 본계좌 매수도 생략(매도는 계속)")
        return False
    return True


def cmd_buy():
    guard_mock_only()
    # [2026-08-27] 3층 안전장치 — 계좌 단위 정지 상태면 신규매수만 건너뛴다.
    #   매도·만기청산·손절은 이 게이트를 타지 않는다.
    if REAL:
        if not _real_buy_allowed():
            return
    else:
        kill_switch.guard_buy(LABEL)
    sigs = todays_signals()
    if not sigs:
        print("[buy] 오늘 신호 없음 — 종료")
        return
    if SKIP_LIMITUP:                              # [§91] 전략별 '상한가 마감 신호 제외'
        import signal_filters as _SF
        sigs, _lu_dropped = _SF.drop_limitup(sigs, SKIP_LIMITUP, label="buy")
        if not sigs:
            print("[buy] 남은 신호 없음(상한가 마감 신호 제외) — 종료")
            return
    api = get_api()
    pos = get_positions(api)

    # ── 정합성 게이트(오주문 방지) ──────────────────────────────────────────
    # get_positions 는 스키마 인식 실패/일시 글리치 시 조용히 {} 를 반환할 수 있다.
    # 브로커 0건인데 직전 스냅샷엔 보유가 있었으면 = 응답 누락 의심 → 슬롯 오판으로
    # 이미 보유한 종목에 과다 매수가 날 수 있으므로 강제 중단.
    if not pos and (_snapshot_codes() or load_kiwoom_positions()):
        print("[buy] 잔고 응답 의심(브로커 0 / 스냅샷 또는 원장엔 보유 있음) — 오주문 방지 위해 매수 중단")
        try:
            import notifier
            notifier.safe_send(f"⛔ [{TAG} buy 중단] 잔고 응답이 비어 있음(스냅샷은 보유). API 누락 의심 → 매수 스킵.")
        except Exception:
            pass
        return

    already = today_ordered_codes("buy")
    dep = get_deposit(api)
    if REAL:
        if "__unreadable__" in already:
            msg = f"⛔ [{LABEL}] 오늘 주문 기록을 읽지 못해 중복 주문 위험 — 오늘 신규 매수 중단(파일 확인 요망)"
            print(msg)
            try:
                import notifier
                notifier.safe_send(msg)
            except Exception:
                pass
            return
        if _REAL_CASH["uncl"] > 0:
            msg = (f"⛔ [{LABEL}] 현금 미수금 {_REAL_CASH['uncl']:,}원이 있습니다 — 미수가 정리될 때까지 신규 매수 중단"
                   "(반대매매 위험). 키움 HTS/앱에서 확인하세요.")
            print(msg)
            try:
                import notifier
                notifier.safe_send_once(msg, f"real_uncl:{datetime.now():%Y%m%d}")
            except Exception:
                pass
            return
        print(f"[buy] 본계좌 주문가능금액 {dep:,}원 ({_REAL_CASH['basis']}) · 모드 {MODE}{' — 드라이런(주문 안 냄)' if DRY else ''}")
    strength = _strength_map(sigs)   # {(code,strategy): score_ic} — 강도 필터용
    if strength:
        n_weak = sum(1 for s in sigs
                     if str(s.get("strategy", "")) not in STRENGTH_EXEMPT
                     and strength.get((str(s.get("code", "")).zfill(6),
                                       str(s.get("strategy", ""))), 99) < _th(str(s.get("strategy", ""))))
        print(f"[buy] 강도 필터: 강도 < 기준({MIN_STRENGTH_SCORE:g}, 전략별 {STRENGTH_TH}) 스킵 예정 {n_weak}건 / 기록 {len(strength)}건"
              f" (면제: {', '.join(sorted(STRENGTH_EXEMPT))})")
    else:
        print("[buy] 강도 기록 없음 — 재확인 단계에서 재계산 시도")
    # 매매 전 사전점검 ①: 강도 재확인(무기록이면 즉석 재계산, 그래도 없으면 차단)
    strength, _unknown_strength = verify_strength(sigs, strength)

    # 매매 전 사전점검 ③: 일일 후보수 게이트(2026-08-27, 현재 모니터 전용).
    #   강도가 최종 확정된 이 시점에서만 '오늘 몇 건이 통과했나'를 정확히 셀 수 있다.
    _n_pass = sum(1 for _sg in sigs
                  if _strength_gate(str(_sg.get("strategy", "")),
                                    strength.get((str(_sg.get("code", "")).zfill(6),
                                                  str(_sg.get("strategy", "")))))[0])
    if not _gate_check(_n_pass, len(sigs)):
        print("[buy] 후보수 게이트로 오늘 신규매수 생략 — 종료")
        return

    # ── 전략별 슬롯 현황 ──────────────────────────────────────────────────────
    # 원장(매수 당시 전략)으로 덮어씀 — '최신 신호' 전략맵은 보유 중 종목이 다른
    # 전략으로 재신호되면 슬롯을 오배정(2026-07-07, 익절 오배정과 동일 클래스).
    strategy_map = get_signal_strategy_map()
    for _c, _v in load_kiwoom_positions().items():
        if _v.get("strategy"):
            strategy_map[_c] = _v["strategy"]
    # [§96] 칸은 '프로그램 몫'으로만 센다 — 본계좌에서 사용자가 직접 산 종목은 칸을 차지하지 않는다(모의는 계좌 전체가 프로그램 몫)
    held = _own(pos)
    slot_used = count_slots_by_strategy(held.keys(), strategy_map)
    legacy_used = slot_used.get("_legacy", 0)   # 레거시 전략(for_high20_mkt 등) 보유 수
    slot_avail = {k: max(0, STRATEGY_MAX_SLOTS[k] - slot_used[k])
                  for k in STRATEGY_MAX_SLOTS}
    # 전체 가용 슬롯 = 총 슬롯 상한 - 현재 사용(현 전략 + 레거시)
    total_avail = max(0, sum(slot_avail.values()) - legacy_used)
    if SLOT_MODE == "pool":                      # 칸 없음: 전략 구분 없이 전체 상한까지(보유 전부 포함)
        total_avail = max(0, MAX_CONCURRENT - len(held))

    print(f"[buy] 오늘 신호 {len(sigs)}건  예수금 {dep:,}원")
    if SLOT_MODE == "pool":
        print(f"  칸 방식: 칸 없음 — 보유 {len(held)}/{MAX_CONCURRENT}, 빈 칸 {total_avail}개 (전략별 보유 "
              + " / ".join(f"{k}={slot_used[k]}" for k in STRATEGY_PRIORITY) + ")")
    else:
        print(f"  전략별 슬롯: "
              + " / ".join(f"{k}={slot_used[k]}/{STRATEGY_MAX_SLOTS[k]}"
                           for k in STRATEGY_PRIORITY))
    if total_avail <= 0:
        print("[buy] 모든 슬롯 가득 — 주문 없음")
        return

    # ── 거래대금 랭킹 맵 ─────────────────────────────────────────────────────
    tv_map = {}
    sig_csv = f"./macro_data/daily/{latest_macro_date()}.csv"
    if os.path.exists(sig_csv):
        md = pd.read_csv(sig_csv, encoding="utf-8-sig", dtype={"code": str})
        md = md.rename(columns={"거래대금": "trading_value"})
        md["code"] = md["code"].astype(str).str.zfill(6)
        if "trading_value" in md.columns:
            tv_map = dict(zip(md["code"], md["trading_value"]))

    # ── 전략별로 신호 분류 + 강도(score_ic) 내림차순 정렬(무기록은 tv순 폴백) ──
    #    (2026-07-06: tv 상위 우선의 역선택 확정으로 변경 — _order_candidates 주석 참조)
    sigs_by_strat = {k: [] for k in STRATEGY_PRIORITY}
    for sig in sigs:
        strat = str(sig.get("strategy", "high_52w_filt"))
        if strat not in sigs_by_strat:
            # [2026-10-18] 종전엔 모르는 전략을 high_52w_filt 로 바꿔 담았다 — 이제 high_52w_filt 는
            # 강도 면제라, 교체 직후 남은 rsi 신호가 강도 검사 없이 매수될 수 있다 → 건너뛴다.
            print(f"    [skip] {sig.get('code')} — 운용 중이 아닌 전략({strat}) 신호")
            continue
        sigs_by_strat[strat].append(sig)
    for strat in STRATEGY_PRIORITY:
        sigs_by_strat[strat] = _order_candidates(sigs_by_strat[strat], strength, tv_map, strat)

    # ── 매매 전 사전점검 ②: 잔고 재확인 ────────────────────────────────────
    # (2026-07-21 사용자 요청) 매수 직전 시점의 실제 예수금을 다시 조회한다. cmd_buy
    # 진입 후 슬롯·정렬 계산 사이에 시간이 흐르고, daily-am 은 앞단 손절매도 체결이
    # 뒤늦게 반영될 수 있어 초기 조회값이 낡을 수 있다. 실패 시 초기값 유지(보수적).
    try:
        _dep2 = get_deposit(api)
        if _dep2 != dep:
            print(f"[verify] 잔고 재확인: 예수금 {_dep2:,}원 (직전 조회 {dep:,}원 → 갱신)")
        else:
            print(f"[verify] 잔고 재확인 OK — 예수금 {_dep2:,}원")
        dep = _dep2
    except Exception as e:
        print(f"[verify] 잔고 재조회 실패({e}) — 직전 조회값 {dep:,}원으로 진행")

    # ── 우선순위대로 슬롯 채우기 ─────────────────────────────────────────────
    n_placed = 0
    remaining_dep = dep
    placed_per_strat = {k: 0 for k in STRATEGY_PRIORITY}  # 전략별 실제 배정 수 추적
    # [2026-09-16] 매수 실패도 리스크 사건이다 — 청산 실패(1591행)와 동일하게 알린다.
    # 실사고: 09-08 키움 모의계좌가 초기화되며 '개인공매도이수전용'으로 바뀌어 매수가
    # RC5006 으로 거부됐는데, [실패] 한 줄 + exit 0 이라 아무도 몰랐다. 강도 통과가
    # 드문 국면이라 '안 사는 것'과 '못 사는 것'이 겉으로 구분되지 않아 더 위험하다.
    buy_failures = []      # [(code, name, msg)]
    # [§96.6] 전략 총액 상한용 — 이미 산 금액(프로그램 몫, 매입가 기준)과 이번 실행에서 주문한 금액
    invested, spent = {}, {}
    if STRATEGY_CAP:
        for _c, _p in held.items():
            _s = strategy_map.get(str(_c).zfill(6), "")
            invested[_s] = invested.get(_s, 0) + _to_int(_p.get("qty", 0)) * float(_p.get("avg_price", 0) or 0)
        print("  [금액 상한] " + " / ".join(f"{k} {v:,}원(이미 {invested.get(k, 0):,.0f}원)" for k, v in STRATEGY_CAP.items()))

    if SLOT_MODE == "pool":   # [§75] 한 줄로: 전략 안 순위 높은 순, 전체 빈 칸까지
        _plan = [(None, _pool_order([s for k in STRATEGY_PRIORITY for s in sigs_by_strat.get(k, [])], strength),
                  total_avail)]
    else:
        _plan = [(k, sigs_by_strat.get(k, []), slot_avail[k]) for k in STRATEGY_PRIORITY]
    for _grp, strat_sigs, avail in _plan:
        if n_placed >= total_avail:   # 전역 동시보유 상한(레거시 반영) 도달 — 과다매수 방지(2026-06-29)
            break
        if avail <= 0:
            continue
        print(f"\n  [{_grp or '칸 없음'}] 빈{'슬롯' if _grp else ' 칸'} {avail}개  후보 {len(strat_sigs)}건"
              + ("" if _grp else "(전략 안 순위 높은 순)"))

        placed_this_strat = 0
        for sig in strat_sigs:
            strat = _grp or str(sig.get("strategy", ""))
            # 전략별 빈슬롯 OR 전역 가용슬롯(레거시 반영) 중 먼저 소진되면 중단
            if placed_this_strat >= avail or n_placed >= total_avail:
                break
            code = str(sig["code"]).zfill(6)
            name = str(sig.get("name", ""))
            close = float(sig.get("entry_price_close", 0) or 0)

            if code in pos or code in already:
                print(f"    [skip] {code} {name} — 이미 보유/주문됨")
                continue
            # NaN 방어: 'close <= 0' 은 NaN 에서 False 라 통과 후 int(NaN) 크래시로
            # cmd_buy 전체가 죽음 — 'not (close > 0)' 은 NaN 도 걸러냄(2026-07-10)
            if not (close > 0):
                continue

            # 강도 필터 — 주문 직전 최종 게이트(2026-07-02 도입, 2026-07-21 fail-closed).
            # verify_strength 가 이미 무기록을 재계산했다 → 여기서도 None 이면 확인 불가로
            # 간주하고 차단한다(종전 fail-open 이 6 미만 체결의 잔여 경로였음).
            _sc = strength.get((code, strat))
            _ok, _why = _strength_gate(strat, _sc)
            if not _ok:
                print(f"    [skip] {code} {name} — {_why}")
                continue

            # 잔여 빈 슬롯 수로 예산 균등 분배 (각 전략의 실제 배정 수 반영)
            # placed_per_strat[k] 가 이미 현재 전략 포함 모든 배정 수를 추적하므로
            # placed_this_strat 이중 차감 불필요 (버그 수정 2026-06-18)
            total_slots_remaining = (total_avail - n_placed) if _grp is None else sum(
                max(0, slot_avail[k] - placed_per_strat[k])
                for k in STRATEGY_PRIORITY
            )
            if total_slots_remaining <= 0:
                break
            budget = remaining_dep / total_slots_remaining
            _cap = STRATEGY_CAP.get(strat)
            if _cap is not None:   # [§96.6] 전략 총액 상한 — 남은 상한을 그 전략 남은 빈 칸으로(칸 없음 모드는 균등 몫과 남은 상한 중 작은 값)
                room = _cap - invested.get(strat, 0) - spent.get(strat, 0)
                if room < MIN_ORDER_AMOUNT:
                    print(f"    [skip] {code} {name} — {strat} 금액 상한 {_cap:,}원 도달(남은 {max(0, room):,.0f}원)")
                    continue
                if _grp is None:
                    budget = min(budget, room)
                else:
                    budget = min(remaining_dep, room / max(1, slot_avail[strat] - placed_per_strat[strat]))
            _lim_msg = ""
            if REAL:   # [§96] 현재가 +2% 지정가 — 수량은 지정가 기준(현금 100%, 미수 없음). 가격을 모르면 사지 않는다.
                _q = _quote(api, code)
                if not _q:
                    buy_failures.append((code, name, "현재가 조회 실패 — 주문 안 함"))
                    continue
                px = _real_limit_price(_q["cur"], _q.get("upper"))
                qty = int((budget * REAL_BUDGET_PAD) // px)
                _lim_msg = f"지정가 {px:,} (현재가 {_q['cur']:,} +{REAL_BUY_SLIP * 100:g}%)"
            else:
                px = close
                qty = int((budget * 0.97) // close)   # 갭상승 대비 3% 여유
            if qty < 1 or qty * px < MIN_ORDER_AMOUNT:
                print(f"    [skip] {code} {name} — 예산 부족 ({budget:,.0f}원)")
                continue

            # 주문 직전 잔고 충분성 최종 확인(2026-07-21) — 슬롯 균등분배 예산은 계산값이라
            # 반올림·앞선 체결로 실제 잔여를 넘어설 수 있다. 넘으면 주문하지 않는다.
            _need = qty * px
            if _need > remaining_dep:
                print(f"    [skip] {code} {name} — 잔고 부족(필요 {_need:,.0f} > 잔여 {remaining_dep:,.0f})")
                continue
            print(f"    [verify] {code} {name} — {_why} / "
                  f"잔여 {remaining_dep:,.0f} ≥ 주문 {_need:,.0f} → 주문 실행" + (f" ({_lim_msg})" if _lim_msg else ""))

            if DRY:   # [§96] 본계좌 드라이런 — 주문·원장 변경 없이 '냈을 주문'만 기록(칸·예산은 실제처럼 차감해 다음 후보 계산)
                _dry_record("buy", code, name, strat, qty, px, f"{_why} · {_lim_msg}")
                spent[strat] = spent.get(strat, 0) + _need
                remaining_dep -= _need
                n_placed += 1
                placed_this_strat += 1
                placed_per_strat[strat] += 1
                already.add(code)
                continue

            try:
                _inflight("buy", code, qty, px)   # [§96] 본계좌: 전송 직전 기록(실패하면 주문하지 않음)
                r = _order_with_retry(lambda: api.order.stock_buy_order_request_kt10000(
                    dmst_stex_tp="KRX", stk_cd=code, ord_qty=str(qty),
                    trde_tp=ORDER_TYPE_LIMIT if REAL else ORDER_TYPE_BUY,
                    ord_uv=str(int(px)) if REAL else "",
                ), "매수")
                ono = _pick(r, "ord_no", "odno", default="")
                spent[strat] = spent.get(strat, 0) + _need   # [§96.6] 접수 즉시 반영(아래 기록이 실패해도 상한 계산은 맞게)
                print(f"    [매수주문] {code} {name} {qty}주 @ {int(px):,} → {ono}")
                try:
                    import notifier
                    notifier.queue_fill("buy", name, code, qty, px)
                except Exception:
                    pass
                log_order({
                    "time": datetime.now().strftime("%H:%M:%S"), "side": "buy",
                    "code": code, "name": name, "strategy": strat,
                    "qty": qty, "price": int(close),
                    "order_type": ORDER_TYPE_LIMIT if REAL else ORDER_TYPE_BUY, "ok": True, "order_no": ono,
                    "msg": _lim_msg,
                })
                remaining_dep -= _need
                n_placed += 1
                placed_this_strat += 1
                placed_per_strat[strat] += 1
                already.add(code)
                try:   # 진입가 원장 기록(참조가=신호 종가; 실체결 평균은 ensure 가 이후 보정) — [§96] 수량도(본계좌는 이 수량만 관리)
                    save_kiwoom_position(code, close, strat,
                                         str(sig.get("signal_date", "")),
                                         _to_int(sig.get("holding_days"), 20), qty=qty)
                except Exception as e:
                    print(f"    [warn] 원장 기록 실패(무시): {e}")
            except Exception as e:
                print(f"    [실패] {code} {name}: {e}")
                buy_failures.append((code, name, str(e)[:160]))
                try:
                    log_order({
                        "time": datetime.now().strftime("%H:%M:%S"), "side": "buy",
                        "code": code, "name": name, "strategy": strat,
                        "qty": qty, "price": int(close),
                        "order_type": ORDER_TYPE_LIMIT if REAL else ORDER_TYPE_BUY, "ok": False, "order_no": "",
                        "msg": str(e)[:200],
                    })
                except Exception as _le:   # 로그 파일이 잠겨도(엑셀 등) 다음 후보·실패 알림은 계속
                    print(f"    [warn] 주문로그 기록 실패: {_le}")
            time.sleep(0.5)   # 주문 간 간격 — 연속 발사 429 예방(2026-07-14)

    print(f"\n[buy] 주문 {n_placed}건 완료")
    if buy_failures:
        _alert_buy_failures(buy_failures, n_placed)


# ------------------------------------------------------------
# 익절(수익실현) 지정가 — PROFIT_TARGET 전략 보유분에 당일 지정가 매도
# ------------------------------------------------------------
def _prev_close_map():
    """최신 macro CSV(전일)의 종가 맵 — 오늘 가격제한폭(전일종가×1.3) 판정용."""
    d = latest_macro_date()
    if not d:
        return {}
    path = f"./macro_data/daily/{d}.csv"
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
        df["code"] = df["code"].astype(str).str.zfill(6)
        col = next((c for c in df.columns if c in ("close", "종가")), None)
        return dict(zip(df["code"], pd.to_numeric(df[col], errors="coerce"))) if col else {}
    except Exception:
        return {}


def _held_buy_map():
    """주문로그 전체에서 {code → 마지막 '성공 매수'의 {strategy, date, ref_px}}.

    실제 매수 시점 정보가 진실원천(최신 '신호' 기준 strategy_map 은 오배정 위험,
    2026-07-03). ref_px = 주문 참조가(신호일 종가) — 슬리피지 측정의 기준가."""
    out = {}
    try:
        for f in sorted(_glob.glob(f"{ORDERS_DIR}/orders_*.csv")):
            try:
                df = pd.read_csv(f, dtype={"code": str})
            except Exception:
                continue
            if "side" not in df.columns:
                continue
            b = df[(df["side"] == "buy")
                   & df["ok"].astype(str).str.lower().isin(("true", "1"))]
            date = os.path.basename(f)[7:15]
            for _, r in b.iterrows():   # 파일 오름차순 순회 → 최근 매수가 덮어씀
                out[str(r.get("code", "")).zfill(6)] = {
                    "strategy": str(r.get("strategy", "") or ""),
                    "date": date,
                    "ref_px": _to_int(r.get("price", 0)),
                    "qty": _to_int(r.get("qty", 0)),          # [§96] 본계좌 원장 복구용
                }
    except Exception:
        pass
    return out


def _held_strategy_map():
    """{code → 매수 당시 strategy} (하위호환 래퍼 — _held_buy_map 참조)."""
    return {c: v["strategy"] for c, v in _held_buy_map().items()}


# ── 키움 진입가 원장 (kiwoom_positions.csv) — KIS kis_positions.csv 와 동일 패턴 ──
#    스냅샷 의존이던 정합성 기준을 독립 원장으로 보강(2026-07-06). 매수 시 기록,
#    매도/익절체결 시 삭제, 브로커 보유에 있는데 원장에 없으면 자가치유 등록.
KIWOOM_POSITIONS_CSV = f"{ORDERS_DIR}/kiwoom_positions.csv"


def load_kiwoom_positions():
    """kiwoom_positions.csv → {code: {entry_px, strategy, signal_date, holding_days}}.

    [2026-07-10] 전 컬럼 dtype=str 로 읽음 — 한 행이라도 signal_date 가 비면 pandas 가
    컬럼을 float 로 승격해 전 행이 '20260624.0' 이 되고, 원장 만기판정(_expiry_due 의
    달력 매칭)이 통째로 무력화되던 위험 차단. '.0' 잔재도 방어적으로 제거."""
    if not os.path.exists(KIWOOM_POSITIONS_CSV):
        return {}
    try:
        df = pd.read_csv(KIWOOM_POSITIONS_CSV, dtype=str).fillna("")
        df["code"] = df["code"].astype(str).str.zfill(6)
        out = {}
        for _, r in df.iterrows():
            try:
                holding = int(float(str(r.get("holding_days", "") or 0)))
            except (TypeError, ValueError):
                holding = 20
            try:
                qty = int(float(str(r.get("qty", "") or 0)))
            except (TypeError, ValueError):
                qty = 0
            out[r["code"]] = {
                "entry_px":     _to_float(r.get("entry_px", 0), 0.0),
                "strategy":     str(r.get("strategy", "") or ""),
                "signal_date":  _re.sub(r"\.0$", "", str(r.get("signal_date", "") or "")),
                "holding_days": holding if holding > 0 else 20,
                "qty":          qty,     # [§96] 프로그램이 산 수량(본계좌는 이 수량만 팔고 센다). 옛 행은 0
            }
        return out
    except Exception as e:
        print(f"[warn] kiwoom_positions 로드 실패: {e}")
        return {}


def _write_kiwoom_positions(pos_dict):
    """원자적 재작성(tmp + os.replace) — 도중 크래시 시 0바이트/부분행 방지(2026-07-10)."""
    os.makedirs(ORDERS_DIR, exist_ok=True)
    fields = ["code", "entry_px", "strategy", "signal_date", "holding_days", "qty"]   # [§96] qty 추가(맨 뒤)
    tmp = KIWOOM_POSITIONS_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for code, d in pos_dict.items():
            w.writerow({"code": code, **d})
    os.replace(tmp, KIWOOM_POSITIONS_CSV)


def save_kiwoom_position(code, entry_px, strategy, signal_date, holding_days, qty=None):
    existing = load_kiwoom_positions()
    existing[str(code).zfill(6)] = {
        "entry_px": entry_px, "strategy": strategy,
        "signal_date": signal_date, "holding_days": holding_days,
        "qty": int(qty) if qty else "",
    }
    _write_kiwoom_positions(existing)


def remove_kiwoom_position(code):
    existing = load_kiwoom_positions()
    if existing.pop(str(code).zfill(6), None) is not None:
        _write_kiwoom_positions(existing)


def _own(pos):
    """[§96] 브로커 잔고 중 '이 프로그램이 산 몫'만 → {code: 잔고 dict(qty=프로그램 수량)}.

    본계좌엔 사용자가 직접 산 종목이 있을 수 있다 — 원장에 있는 종목만, 수량은 원장 수량까지(사용자가 같은 종목을
    더 샀으면 그 몫은 빼고), 그때 매입가는 섞인 브로커 평균 대신 원장 진입가. 만기·손절·익절·칸 계산은 전부 이 몫만 본다.
    모의계좌는 계좌 전체가 프로그램 몫이라 그대로 돌려준다."""
    if not REAL:
        return pos
    led = load_kiwoom_positions()
    out = {}
    for code, p in pos.items():
        r = led.get(str(code).zfill(6))
        if not r:
            continue
        lq, bq = int(r.get("qty") or 0), _to_int(p.get("qty", 0))
        q = min(lq, bq)
        if q <= 0:
            continue
        d = dict(p)
        d["qty"] = q
        if bq != lq and float(r.get("entry_px") or 0) > 0:
            d["avg_price"] = float(r["entry_px"])
        out[code] = d
    return out


def _real_sold_dates():
    """{code: 마지막 '성공 매도' 날짜} — 본계좌 원장 복구 때 '이미 판 종목'을 되살리지 않게(주문로그 기준)."""
    out = {}
    for f in sorted(_glob.glob(f"{ORDERS_DIR}/orders_*.csv")):
        try:
            df = pd.read_csv(f, dtype={"code": str})
        except Exception:
            continue
        if "side" not in df.columns:
            continue
        s = df[(df["side"] == "sell") & df["ok"].astype(str).str.lower().isin(("true", "1"))]
        for c in s["code"].astype(str).str.zfill(6):
            out[c] = os.path.basename(f)[7:15]
    return out


def _ensure_real_ledger(pos):
    """[§96] 본계좌 원장 정리 — 모의(ensure_kiwoom_ledger)와 달리 브로커에만 있는 종목을 **절대 끌어오지 않는다**
    (사용자가 직접 산 종목일 수 있음). 하는 일:
      ① 정리: 원장 종목이 잔고에 없으면 삭제(오늘 산 종목은 체결 전일 수 있어 보존, 잔고가 통째로 비었으면 오늘 판 것만)
      ② 수량: 잔고 수량 < 원장 수량이면 원장을 잔고에 맞춘다(일부 체결·사용자가 일부 매도). 많으면 사용자 몫 → 경고 1회
      ③ 매입가: 섞이지 않았을 때만 브로커 매입평균(실체결)으로 보정
      ④ 복구: 원장엔 없지만 '이 프로그램의 매수 기록'(주문로그)이 마지막 매도보다 뒤인 종목만 그 기록으로 되살림"""
    try:
        led = load_kiwoom_positions()
        sold_today = today_ordered_codes("sell")
        bought_today = today_ordered_codes("buy")
        changed, mixed = False, []
        # 잔고가 통째로 비었을 때: 일시 글리치와 '정말 다 판 것'(사용자가 직접 전부 매도)을 구분 — 1시간 넘게 떨어진 두 실행에서
        # 계속 비어 있어야 정리한다. 정리하지 않으면 '잔고 0 vs 원장 보유' 매수 중단이 영영 풀리지 않는다(모의의 좀비 행 연쇄와 같은 꼴).
        empty_ok = False
        first = ((_RA.read_json("empty_seen.json")[0]) or {}).get("first")
        if not pos and led:
            if first:
                try:
                    empty_ok = (datetime.now() - datetime.fromisoformat(first)).total_seconds() >= 3600
                except Exception:
                    empty_ok = False
            else:
                _RA.write_json("empty_seen.json", {"first": datetime.now().isoformat(timespec="seconds")})
        elif first:                                   # 잔고가 다시 보이거나 원장이 비면 처음부터
            _RA.write_json("empty_seen.json", {})
        gone = []
        for code in list(led.keys()):
            r = led[code]
            p = pos.get(code)
            if not p:
                if code in bought_today:
                    continue
                if code in sold_today or pos or empty_ok:
                    led.pop(code, None)
                    changed = True
                    gone.append(code)
                    print(f"[ledger] 본계좌 원장 정리: {code} — "
                          + ("오늘 매도 체결" if code in sold_today else "잔고에 없음(체결 안 됨·직접 매도)"))
                continue
            bq, lq = _to_int(p.get("qty", 0)), int(r.get("qty") or 0)
            if code not in bought_today and (lq <= 0 or bq < lq):
                print(f"[ledger] 본계좌 원장 수량 보정: {code} {lq} → {bq}")
                r["qty"] = bq
                lq = bq
                changed = True
            elif bq > lq > 0:
                mixed.append(code)
            avg = float(p.get("avg_price", 0) or 0)
            if bq == lq and avg > 0 and float(r.get("entry_px", 0) or 0) != avg:
                r["entry_px"] = avg
                changed = True
        buys, sells, restored, unknown = _held_buy_map(), None, [], []
        today = datetime.today().strftime("%Y%m%d")
        for code, info in buys.items():
            if code in led or code not in pos or code in sold_today:
                continue
            if sells is None:
                sells = _real_sold_dates()
            if sells.get(code, "") >= info.get("date", ""):
                continue                      # 산 뒤에 판 기록이 있음 → 이미 끝난 매매(지금 잔고는 사용자 몫)
            q = min(int(info.get("qty") or 0), _to_int(pos[code].get("qty", 0)))
            if q <= 0:
                continue
            if info.get("date") != today:
                # 오늘 산 것만 자동 복구 — 며칠 지난 기록은 그 사이 정리된 뒤 사용자가 새로 샀을 수도 있어 알리기만
                unknown.append(code)
                continue
            strat = info.get("strategy", "")
            led[code] = {"entry_px": float(pos[code].get("avg_price", 0) or 0) or float(info.get("ref_px", 0) or 0),
                         "strategy": strat,
                         "signal_date": _signal_date_lookup(code, strat, info.get("date", "")) or info.get("date", ""),
                         "holding_days": _HEAL_HOLDING.get(strat, 20), "qty": q}
            restored.append(code)
            changed = True
        if changed:
            _write_kiwoom_positions(led)
        if mixed or restored or unknown or (empty_ok and gone):
            try:
                import notifier
                if empty_ok and gone:
                    notifier.safe_send(f"ℹ️ [{LABEL}] 계좌 잔고가 1시간 넘게 비어 있어 자동매매 원장 {gone} 을(를) 정리했습니다 — "
                                       "직접 전부 매도하셨으면 정상입니다(다음 매매부터 빈 칸으로 다시 삽니다).")
                if restored:
                    notifier.safe_send(f"⚠ [{LABEL}] 원장에서 빠진 프로그램 매수 {restored} 를 주문 기록으로 복구했습니다 — "
                                       "만기·손절은 복구된 수량만 관리합니다.")
                if unknown:
                    notifier.safe_send_once(f"⚠ [{LABEL}] 프로그램이 예전에 산 기록이 있는데 원장에 없는 보유 {unknown} — "
                                            "자동매매가 관리하지 않습니다(직접 산 것일 수도 있어 함부로 가져오지 않음). 확인해 주세요.",
                                            f"real_unknown:{datetime.now():%Y%m%d}:{','.join(unknown)}")
                if mixed:
                    notifier.safe_send_once(f"ℹ️ [{LABEL}] 자동매매 종목 {mixed} 을(를) 직접 더 사셨네요 — 자동매매는 "
                                            "자기가 산 수량만 팔고, 직접 산 몫은 건드리지 않습니다.",
                                            f"real_mixed:{datetime.now():%Y%m%d}:{','.join(mixed)}")
            except Exception:
                pass
    except Exception as e:
        print(f"[warn] 본계좌 원장 정리 실패(무시): {e}")


# 자가치유 시 전략별 보유일 (안C 라이브 파라미터와 일치 — 20 고정이던 것을 수정 2026-07-10)
_HEAL_HOLDING = dict(_ACC["holding"])   # 기본 {"high_52w_filt": 20, "high_500d_filt": 20, "rsi_reversal": 5, "rsi_vol": 7}


def _signal_date_lookup(code, strategy, buy_date):
    """paper_signals.csv 에서 매수일(buy_date) 직전의 실제 신호일 역추적 — 자가치유용.

    치유가 signal_date 에 '매수일'을 넣으면 _expiry_due(진입=신호 다음날) 가정과 충돌해
    만기가 1영업일 늦어짐(2026-07-06 시드 3종에서 실측). 신호CSV 에서 찾으면 정확한
    신호일을, 못 찾으면 ''(호출측이 매수일 폴백 — 1일 지연 허용)."""
    try:
        if not os.path.exists(SIGNALS_CSV) or not buy_date:
            return ""
        s = pd.read_csv(SIGNALS_CSV, dtype=str)
        s["code"] = s["code"].astype(str).str.zfill(6)
        s["signal_date"] = s["signal_date"].astype(str).str.replace("-", "", regex=False)
        s = s[(s["code"] == str(code).zfill(6)) & (s["signal_date"] < str(buy_date))]
        if strategy and "strategy" in s.columns:
            st = s[s["strategy"] == strategy]
            if not st.empty:
                s = st
        return str(s["signal_date"].max()) if not s.empty else ""
    except Exception:
        return ""


def ensure_kiwoom_ledger(pos):
    """원장 자가치유 — 등록(브로커에만 있음) + 정리(원장에만 있음).

    [2026-07-10 재작성 — 좀비행 부활 방지(실사례 105330)]
    ① 오늘 매도된 코드는 재등록 금지: 15:21 시장가 매도는 15:30 동시호가에 체결되는데,
       직후 cmd_status 시점의 잔고엔 아직 보유로 보여 cmd_sell 이 지운 원장 행이
       되살아났음(원장에 좀비 행 축적 → 전량청산 시 '잔고0 vs 원장보유' 게이트가
       다음날 매수를 영구 차단하는 연쇄).
    ② prune: 원장에만 있고 브로커에 없는 행 제거. 보호장치 — 오늘 매수분(잔고 반영
       지연 가능)은 제외, 잔고가 통째로 빈 응답(글리치 의심)일 땐 오늘 매도분만 정리.
    ③ 치유 메타데이터 정확화: holding_days 전략별(_HEAL_HOLDING), signal_date 는
       신호CSV 역추적(_signal_date_lookup) 우선.
    entry_px = 브로커 매입평균(실체결) 우선, 없으면 주문 참조가."""
    if REAL:                          # [§96] 본계좌: 브로커에만 있는 종목을 끌어오지 않는 별도 규칙
        return _ensure_real_ledger(pos)
    try:
        led = load_kiwoom_positions()
        sold_today = today_ordered_codes("sell")
        bought_today = today_ordered_codes("buy")
        pos_codes = {str(c).zfill(6) for c in pos.keys()}
        changed = False

        # ── ② prune: 원장에만 있는 행 제거 ────────────────────────────────
        for code in list(led.keys()):
            if code in pos_codes or code in bought_today:
                continue
            if code in sold_today or pos_codes:
                led.pop(code, None)
                changed = True
                why = "오늘 매도 체결" if code in sold_today else "브로커 미보유(수동매도/체결 잔재)"
                print(f"[ledger] 원장 정리: {code} — {why}")

        # ── 실체결 보정(2026-07-11): 매수 시점 원장 entry_px 는 주문 참조가(신호종가)인데
        #    백테스트 stop/TP 기준은 '실제 진입 체결가' — 브로커 매입평균이 잡히면 보정.
        #    (기존 675행 주석 'ensure 가 이후 보정'은 heal 이 기존 행을 건너뛰어 허위였음)
        for code, p in pos.items():
            code = str(code).zfill(6)
            row = led.get(code)
            avg = float(p.get("avg_price", 0) or 0)
            if row and avg > 0 and float(row.get("entry_px", 0) or 0) != avg:
                print(f"[ledger] entry_px 실체결 보정: {code} "
                      f"{float(row.get('entry_px', 0) or 0):,.0f} → {avg:,.0f}")
                row["entry_px"] = avg
                changed = True

        # ── ①③ heal: 브로커에만 있는 행 등록 ─────────────────────────────
        buys = None
        added = 0
        blind = []   # 메타데이터 없이 치유된 코드 — 만기/손절 추적 불가라 텔레그램 경보
        for code, p in pos.items():
            code = str(code).zfill(6)
            if code in led or code in sold_today:   # 오늘 매도분은 부활 금지(①)
                continue
            if buys is None:
                buys = _held_buy_map()
            info = buys.get(code, {})
            entry = float(p.get("avg_price", 0) or 0) or float(info.get("ref_px", 0) or 0)
            strat = info.get("strategy", "")
            buy_date = info.get("date", "")
            sig_date = _signal_date_lookup(code, strat, buy_date) or buy_date
            led[code] = {"entry_px": entry,
                         "strategy": strat,
                         "signal_date": sig_date,
                         "holding_days": _HEAL_HOLDING.get(strat, 20)}
            if not strat or not sig_date:
                blind.append(code)
            added += 1
            changed = True
        if blind:   # '조용한 실패' 격상 — 빈 메타데이터 행은 만기·전략 추적이 안 됨(2026-07-11)
            try:
                import notifier
                # [2026-08-09 문구 개선] '치유'는 내부 용어라 사용자에겐 의미가 없다.
                # 실제로 사람이 해야 할 일(수동 매도)이 있는 건이라 ⚠️ 유지하되 이유를 명시.
                notifier.safe_send(
                    f"⚠️ [키움] 매수 기록이 없는 보유 종목 발견: {blind}\n"
                    f"  원장에 자동 등록했지만 진입일/전략을 몰라 **만기 자동매도가 안 됩니다**.\n"
                    f"  조치: 대시보드에서 확인 후 직접 매도하거나 그대로 두셔도 됩니다"
                    f"(다른 종목 매매엔 영향 없음).")
            except Exception:
                pass
        if changed:
            _write_kiwoom_positions(led)
            if added:
                print(f"[ledger] 원장 자가치유 등록 {added}건 → {KIWOOM_POSITIONS_CSV}")
    except Exception as e:
        print(f"[warn] 원장 자가치유 실패(무시): {e}")


# ── 실체결 슬리피지 로그 — 브로커 매입평균 vs 신호일 종가(주문 참조가) ─────────
#    백테스트/페이퍼 가정(X2=종가×1.02, 왕복 0.33%)의 실측 근거(2026-07-06).
#    cmd_status 마다 보유종목을 (code, buy_date) 멱등 append — 포지션이 돌수록 축적.
#    리포트: python slippage_report.py
SLIPPAGE_LOG = f"{ORDERS_DIR}/slippage_log.csv"


def _log_slippage(pos):
    try:
        buys = _held_buy_map()
        seen = set()
        if os.path.exists(SLIPPAGE_LOG):
            try:
                old = pd.read_csv(SLIPPAGE_LOG, dtype=str)
                seen = set(zip(old["code"], old["buy_date"]))
            except Exception:
                pass
        rows = []
        for code, p in pos.items():
            code = str(code).zfill(6)
            info = buys.get(code)
            avg = float(p.get("avg_price", 0) or 0)
            if not info or avg <= 0 or info["ref_px"] <= 0:
                continue
            if (code, info["date"]) in seen:
                continue
            rows.append({"code": code, "name": str(p.get("name", "")),
                         "strategy": info["strategy"], "buy_date": info["date"],
                         "signal_close": info["ref_px"], "fill_avg": int(avg),
                         "slip_pct": round((avg / info["ref_px"] - 1) * 100, 3)})
        if rows:
            new = not os.path.exists(SLIPPAGE_LOG)
            with open(SLIPPAGE_LOG, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["code", "name", "strategy", "buy_date",
                                                  "signal_close", "fill_avg", "slip_pct"])
                if new:
                    w.writeheader()
                w.writerows(rows)
            print(f"[slippage] 매수 슬리피지 {len(rows)}건 기록 → {SLIPPAGE_LOG}")
    except Exception as e:
        print(f"[warn] 슬리피지 기록 실패(무시): {e}")


def _order_candidates(cands, strength, tv_map, strat):
    """매수 후보 정렬 — 강도(score_ic) 내림차순, 강도 무기록은 뒤에서 거래대금 내림차순.

    [2026-07-06 변경] '거래대금 상위 우선'은 신호 경쟁이 심한 전략에서 과열 꼭짓점만
    골라 담는 역선택으로 확인됨(breaker_sweep 진단: 전체 신호 평균 +1.04% vs tv순
    체결분 −2.27%, 체결당 −3.3%p). 강도는 라이브 실측 +1.90%(≥6, 승률 59.5%)로
    유일하게 검증된 선별 기준. 강도 기록이 전혀 없으면 자연히 tv순(기존)으로 폴백.
    """
    def _key(r):
        code = str(r.get("code", "")).zfill(6)
        sc = strength.get((code, str(r.get("strategy", strat))))
        return (sc if sc is not None else float("-inf"),
                float(tv_map.get(r["code"], 0) or 0))
    return sorted(cands, key=_key, reverse=True)


def _today_tp_orders():
    """오늘 발주한 익절 지정가 {code: order_no} (side='tp_sell', ok=True)."""
    path = f"{ORDERS_DIR}/orders_{datetime.today():%Y%m%d}.csv"
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype={"code": str, "order_no": str})
        df = df[(df["side"] == "tp_sell")
                & df["ok"].astype(str).str.lower().isin(("true", "1"))]
        return {str(r["code"]).zfill(6): str(r.get("order_no", "") or "")
                for _, r in df.iterrows()}
    except Exception:
        return {}


def _reconcile_tp_fills(pos):
    """오늘 익절 지정가가 '체결된' 종목(발주됐는데 더는 미보유)을 매도 체결로 기록/알림.

    지정가 체결은 브로커 쪽에서 일어나 로그가 안 남으므로, 오후 실행 시점에
    발주기록 vs 현재 보유를 대조해 합성 sell 행을 남긴다(손익 표시·요약용)."""
    tp = _today_tp_orders()
    if not tp:
        return
    sold_logged = today_ordered_codes("sell")
    path = f"{ORDERS_DIR}/orders_{datetime.today():%Y%m%d}.csv"
    try:
        od = pd.read_csv(path, dtype={"code": str})
        od = od[od["side"] == "tp_sell"]
    except Exception:
        return
    for code, _ono in tp.items():
        if code in pos or code in sold_logged:
            continue   # 아직 보유 = 미체결 / 이미 sell 기록 있음
        row = od[od["code"].astype(str).str.zfill(6) == code].tail(1)
        if row.empty:
            continue
        r = row.iloc[0]
        qty, px = _to_int(r.get("qty", 0)), _to_int(r.get("price", 0))
        name = str(r.get("name", ""))
        print(f"  [익절체결] {code} {name} {qty}주 @ {px:,} (지정가 도달)")
        try:
            import notifier
            notifier.queue_fill("sell", name, code, qty, px)
        except Exception:
            pass
        log_order({"time": datetime.now().strftime("%H:%M:%S"), "side": "sell",
                   "code": code, "name": name, "strategy": str(r.get("strategy", "")),
                   "qty": qty, "price": px, "order_type": ORDER_TYPE_LIMIT,
                   "ok": True, "order_no": str(r.get("order_no", "")),
                   "msg": "profit_target 체결(잔고대조 추정)"})
        try:
            remove_kiwoom_position(code)   # 익절 체결 → 진입가 원장에서 제거
        except Exception:
            pass


def cmd_place_targets(api=None, pos=None):
    """PROFIT_TARGET 전략 보유종목에 '당일 유효' 익절 지정가 매도 발주.

    - 목표가 = 브로커 매입평균 × (1+목표) 를 호가단위 내림.
    - 목표가가 오늘 가격제한폭(전일종가×약1.3) 밖이면 스킵 — 오늘 도달 불가, 내일 재시도.
    - 당일 중복 발주 방지(side='tp_sell' 기록 대조). 주문은 장마감 시 자동 만료 →
      매일 아침 재발주로 자가치유(레지스트리 불필요).
    """
    guard_mock_only()
    api = api or get_api()
    pos = pos if pos is not None else get_positions(api)
    pos = _own(pos)                   # [§96] 본계좌: 프로그램 몫만(수량도 원장 수량) — 직접 산 종목엔 익절 주문 안 냄
    if not pos:
        print("[target] 보유 없음 — 종료")
        return
    strategy_map = get_signal_strategy_map()
    held_map = _held_strategy_map()   # 매수 당시 전략 우선(신호맵은 폴백)
    placed = set(_today_tp_orders().keys()) | today_ordered_codes("tp_sell")
    prev_close = _prev_close_map()
    n = 0
    for code in sorted(pos.keys()):
        p = pos[code]
        strat = held_map.get(str(code).zfill(6)) or strategy_map.get(str(code).zfill(6), "")
        tgt = PROFIT_TARGET.get(strat)
        if tgt is None or code in placed:
            continue
        entry = float(p.get("avg_price", 0) or 0)
        qty = _to_int(p.get("qty", 0))
        name = str(p.get("name", ""))
        if entry <= 0 or qty <= 0:
            print(f"  [skip] {code} {name} — 매입평균 미확인({entry}) → 발주 보류")
            continue
        tgt_px = _tick_floor(entry * (1 + tgt))
        pc = float(prev_close.get(code, 0) or 0)
        if pc > 0 and tgt_px > pc * 1.295:   # 오늘 상한가 밖 — 도달 불가
            print(f"  [skip] {code} {name} — 목표 {tgt_px:,} > 오늘 상한가(≈{int(pc * 1.3):,})")
            continue
        if DRY:                               # [§96] 본계좌 드라이런
            _dry_record("tp_sell", code, name, strat, qty, tgt_px, f"익절 지정가 +{tgt * 100:.0f}%")
            continue
        try:
            _inflight("tp_sell", code, qty, tgt_px)
            r = _order_with_retry(lambda: api.order.stock_sell_order_request_kt10001(
                dmst_stex_tp="KRX", stk_cd=code, ord_qty=str(qty),
                trde_tp=ORDER_TYPE_LIMIT, ord_uv=str(tgt_px)), "익절지정가")
            ono = _pick(r, "ord_no", "odno", default="")
            print(f"  [익절지정가] {code} {name} {qty}주 @ {tgt_px:,} (+{tgt * 100:.0f}%) → {ono}")
            log_order({"time": datetime.now().strftime("%H:%M:%S"), "side": "tp_sell",
                       "code": code, "name": name, "strategy": strat,
                       "qty": qty, "price": tgt_px, "order_type": ORDER_TYPE_LIMIT,
                       "ok": True, "order_no": ono, "msg": f"target+{tgt * 100:.0f}%"})
            n += 1
        except Exception as e:
            print(f"  [실패] {code} {name} 익절지정가: {e}")
            log_order({"time": datetime.now().strftime("%H:%M:%S"), "side": "tp_sell",
                       "code": code, "name": name, "strategy": strat,
                       "qty": qty, "price": tgt_px, "order_type": ORDER_TYPE_LIMIT,
                       "ok": False, "order_no": "", "msg": str(e)[:200]})
        time.sleep(0.5)   # 주문 간 간격 — 연속 발사 429 예방(2026-07-14)
    print(f"[target] 익절 지정가 {n}건 발주")


# ------------------------------------------------------------
# 매도 — 40영업일 도달
# ------------------------------------------------------------
def _expiry_due(ds, signal_date, holding, today):
    """만기 도달 여부 — 진입=신호 다음 영업일, 청산=진입일 포함 holding일째 종가.

    [2026-07-10 오프바이원 수정] 15:21 실행 시점의 macro 달력은 '어제'까지(당일 수집은
    15:50)라 만기일 당일엔 exit_i==len(ds) 로 항상 False → 만기매도가 익영업일에 나가
    보유가 실질 +1일이던 버그(백테스트는 만기일 종가 청산 가정 — 이 수정으로 15:21
    마감 동시호가 매도가 백테스트와 정확히 일치). 달력 마지막의 다음 영업일이 만기이고
    today 가 달력 마지막보다 뒤면 오늘=만기일로 판정(거래일에만 실행되므로 안전)."""
    if not ds or not signal_date or signal_date not in ds:
        return None   # 판정 불가
    entry_i = ds.index(signal_date) + 1
    exit_i = entry_i + int(holding) - 1
    if exit_i < len(ds):
        return ds[exit_i] <= today
    return (exit_i - (len(ds) - 1)) == 1 and today > ds[-1]


def codes_due_for_exit():
    """만기 도달 종목 반환 — 진입가 원장(kiwoom_positions.csv) 우선, 신호CSV 폴백.

    [2026-07-07 변경] 판정 기준을 '신호CSV 전 이력 스캔' → '원장 우선'으로 교체.
    신호CSV 스캔은 같은 코드에 만기 지난 '옛 신호'가 있으면 최근 신호로 산 보유분까지
    due 로 오판(2026-07-06 CJ ENM 실사례의 일반형 — 당일 매수 보류 패치는 당일만 방어,
    어제 이전 매수는 무방비). 원장은 '실제 매수 건'의 signal_date/holding_days 라
    이 충돌이 원천 차단됨. 원장에 없거나 원장 날짜로 판정 불가한 코드만 기존 방식 폴백
    (원장 유실·수동매수 종목이 만기 없이 영구 보유되는 것 방지).

    원본 모드: 진입일 = 신호 다음 영업일 (시가),
               청산일 = 진입일 포함 holding_days 영업일째 (종가).
    """
    from strategies.daily_loader import load_macro_daily
    df = load_macro_daily()
    code_dates = {c: sorted(g["date"].astype(str).tolist())
                  for c, g in df.groupby("code")}
    # ds(load_macro_daily date)·signal_date 와 동일한 대시 없는 'YYYYMMDD'.
    # (과거 '%Y-%m-%d' 대시형이라 ds[exit_i] <= today 가 위치4 '숫자 vs -'로 항상 False →
    #  키움 만기청산이 영영 미발동(보유일 초과해도 매도 안 됨). 2026-06-29 수정.)
    today = datetime.today().strftime("%Y%m%d")
    due = set()
    ledger_decided = set()   # 원장으로 판정 끝난 코드 — 신호CSV 폴백에서 제외

    undecidable = []   # 판정불가 코드 — '조용한 실패' 방지, 텔레그램 격상(2026-07-11)
    for code, info in load_kiwoom_positions().items():
        # [2026-07-10] 원장에 있으면 판정불가여도 폴백 금지 — None 경로로 '옛 신호 만기
        # 오염'(조기청산)이 재유입되던 구멍. 판정불가는 보류+경고(좀비화는 prune/치유가 방지).
        ledger_decided.add(code)
        sd = str(info.get("signal_date", "")).replace("-", "")
        holding = _to_int(info.get("holding_days"), 20)
        verdict = _expiry_due(code_dates.get(code), sd, holding, today)
        if verdict is None:
            print(f"[sell][warn] {code} 원장 signal_date({sd}) 달력 판정불가 — 만기 보류, 원장 확인 필요")
            undecidable.append(code)
        elif verdict:
            due.add(code)
    if undecidable:
        try:
            import notifier
            notifier.safe_send(f"⚠ [{TAG} 만기] 판정불가 보유 {undecidable} — 원장 signal_date 확인 필요(청산 경로 없음)")
        except Exception:
            pass

    # [§96] 본계좌는 원장(프로그램이 산 몫)만 — 신호CSV 폴백은 '원장에 없는 종목 = 사용자가 직접 산 종목'까지 팔 수 있어 금지
    if REAL or not os.path.exists(SIGNALS_CSV):
        return due
    s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    s["signal_date"] = s["signal_date"].astype(str)
    s["code"] = s["code"].astype(str).str.zfill(6)

    # v2 schema 호환
    if "holding_days" not in s.columns:
        s["holding_days"] = HOLDING_DAYS_DEFAULT
    s["holding_days"] = pd.to_numeric(s["holding_days"], errors="coerce").fillna(HOLDING_DAYS_DEFAULT).astype(int)

    for _, r in s.iterrows():
        if r["code"] in ledger_decided:
            continue   # 원장 판정 우선 — 옛 신호의 만기 오염 차단
        if _expiry_due(code_dates.get(r["code"]), r["signal_date"],
                       int(r["holding_days"]), today):
            due.add(r["code"])
    return due


def _circuit_breaker_codes(pos):
    """평가손실이 CIRCUIT_BREAKER_PCT 이하인 보유종목 → {code: loss_pct}.

    손실률 = 브로커 현재가/매입평균 기준(분할 자동보정) 우선, 둘 중 하나라도 없으면
    브로커 pnl_pct 폴백. 브레이커 비활성(None)이면 빈 dict."""
    if CIRCUIT_BREAKER_PCT is None:
        return {}
    out = {}
    for code, p in pos.items():
        code = str(code).zfill(6)
        cur = float(p.get("price", 0) or 0)
        avg = float(p.get("avg_price", 0) or 0)
        if cur > 0 and avg > 0:
            loss = (cur / avg - 1) * 100
        else:
            loss = _to_float(p.get("pnl_pct", 0), 0.0)
        if loss <= CIRCUIT_BREAKER_PCT:
            out[code] = round(loss, 2)
    return out


def _strategy_stop_codes(pos, strategy_map):
    """[2026-10-21] 전략별 손절(STRATEGY_STOP) 도달 보유종목 → {code: loss_pct}. 손익률 산식은 서킷브레이커와 동일."""
    out = {}
    for code, p in pos.items():
        code = str(code).zfill(6)
        stop = STRATEGY_STOP.get(strategy_map.get(code, ""))
        if stop is None:
            continue
        cur = float(p.get("price", 0) or 0)
        avg = float(p.get("avg_price", 0) or 0)
        loss = (cur / avg - 1) * 100 if cur > 0 and avg > 0 else _to_float(p.get("pnl_pct", 0), 0.0)
        if loss <= stop * 100:
            out[code] = round(loss, 2)
    return out


def _today_close_map():
    """당일 종가 맵 (시간외단일가 주문 가격 지정용). 당일 없으면 빈 dict."""
    path = f"./macro_data/daily/{datetime.today():%Y%m%d}.csv"
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype={"code": str})
        df["code"] = df["code"].astype(str).str.zfill(6)
        col = next((c for c in df.columns if c in ("close", "종가")), None)
        if col:
            return dict(zip(df["code"], df[col].astype(float)))
    except Exception:
        pass
    return {}


def cmd_sell():
    guard_mock_only()
    api = get_api()
    pos = get_positions(api)
    _reconcile_tp_fills(pos)   # 오늘 익절 지정가 체결분 기록/알림 (만기 없어도 매 오후 수행)
    pos = _own(pos)            # [§96] 본계좌: 아래 만기·손절·서킷브레이커·수량은 전부 프로그램 몫만(모의는 그대로)
    due = codes_due_for_exit()
    cb = _circuit_breaker_codes(pos)   # 재해성 손실(-40%) 서킷브레이커(2026-07-10)
    _smap0 = get_signal_strategy_map()
    for _c, _v in load_kiwoom_positions().items():
        if _v.get("strategy"):
            _smap0[_c] = _v["strategy"]
    sl = {c: v for c, v in _strategy_stop_codes(pos, _smap0).items() if c not in cb}   # [2026-10-21] 전략별 손절
    for c, v in sl.items():
        cb.setdefault(c, v)        # 같은 청산 경로(시장가)를 쓴다 — 사유 문구만 구분
    if not due and not cb:
        print("[sell] 청산 대상 없음 — 종료")
        try:
            import notifier
            notifier.flush_fills(f"📌 {TAG} 익절 체결")
        except Exception:
            pass
        return
    already = today_ordered_codes("sell")
    if REAL and "__unreadable__" in already:
        msg = (f"🚨 [{LABEL}] 오늘 주문 기록을 읽지 못해 매도를 보류했습니다(중복 매도 위험) — "
               f"만기·손절 대상 {sorted((due | set(cb)) & set(pos))}. 파일 확인 후 직접 처리하거나 다음 실행을 기다리세요.")
        print(msg)
        try:
            import notifier
            notifier.safe_send(msg)
        except Exception:
            pass
        return
    close_map = _today_close_map()
    tp_open = _today_tp_orders()   # 만기청산 전 취소할 미체결 익절 지정가

    strategy_map = get_signal_strategy_map()
    for _c, _v in load_kiwoom_positions().items():   # 매수 당시 전략 우선(로그 정확성)
        if _v.get("strategy"):
            strategy_map[_c] = _v["strategy"]
    bought_today = today_ordered_codes("buy")
    targets = (due | set(cb)) & set(pos.keys()) - already - bought_today
    if (due | set(cb)) & bought_today:
        # 옛 신호의 만기와 '오늘 새 신호 매수'가 같은 코드에 겹치면, 코드 전량 매도가
        # 오늘 산 물량까지 당일 청산해버림(2026-07-06 CJ ENM 실사례) → 당일 매수 코드는
        # 만기매도에서 제외(내일 이후 만기 로직이 다시 처리).
        print(f"[sell] 당일 매수 코드 만기매도 보류: {sorted((due | set(cb)) & bought_today)}")
    # [2026-08-20 문구 수정] 종전 "만기 680종목"은 '전 신호이력 중 만기가 지난 코드 수'
    # 였는데 보유가 0인 날에도 680 이 찍혀 "680종목을 청산해야 하는데 못 했다"로 읽혔다.
    # 실제 의미(청산 후보 목록 vs 실보유 교집합)를 문구에 드러낸다.
    print(f"[sell] 만기 경과 코드 {len(due)}개(신호이력 누계) + 서킷브레이커 {len(cb)}개 "
          f"→ 그중 실제 보유 = 청산 대상 {len(targets)}종목")
    if cb:
        try:
            import notifier
            notifier.safe_send(f"🚨 [{TAG} 서킷브레이커/손절] "
                               + ", ".join(f"{c} {pos[c]['name']}({v:+.1f}%{', 전략손절' if c in sl else ''})"
                                           for c, v in cb.items() if c in pos)
                               + f" ≤ {CIRCUIT_BREAKER_PCT:.0f}% — 시장가 청산" + (" (드라이런 — 실제 주문 안 함)" if DRY else ""))
        except Exception:
            pass
    n_placed = 0
    failures = []      # [(code, name, msg)] — 청산 실패 집계(아래에서 긴급 통지)
    for code in sorted(targets):
        qty = pos[code]["qty"]
        name = pos[code]["name"]
        strat = strategy_map.get(str(code).zfill(6), "")
        _cp = close_map.get(code, 0)
        ref_px = int(_cp) if (_cp and _cp == _cp) else 0   # NaN(자기비교 False) 크래시 방어
        reason = (f"stop({cb[code]:+.1f}% ≤ {STRATEGY_STOP.get(strat, 0) * 100:.0f}%)" if code in sl
                  else f"circuit_breaker({cb[code]:+.1f}%)" if code in cb else "")
        if DRY:                    # [§96] 본계좌 드라이런 — 취소·매도·원장 변경 없음
            _dry_record("sell", code, name, strat, qty, ref_px or pos[code].get("price", 0), reason or "만기")
            continue
        # 미체결 익절 지정가가 물량을 잠그고 있으면 먼저 취소(잔량 전부) 후 시장가 청산
        if code in tp_open:
            try:
                _assert_order_ok(api.order.stock_cancel_order_request_kt10003(
                    dmst_stex_tp="KRX", orig_ord_no=str(tp_open[code]),
                    stk_cd=code, cncl_qty="0"), "지정가취소")
                print(f"  [취소] {code} {name} 미체결 익절 지정가({tp_open[code]}) 취소")
                time.sleep(0.4)
            except Exception as e:
                print(f"  [warn] {code} 익절 지정가 취소 실패({e}) — 시장가 매도 시도 계속")
        try:
            _inflight("sell", code, qty, ref_px)   # [§96] 본계좌: 전송 직전 기록
            r = _order_with_retry(lambda: api.order.stock_sell_order_request_kt10001(
                dmst_stex_tp="KRX", stk_cd=code, ord_qty=str(qty),
                trde_tp=ORDER_TYPE_SELL,
                ord_uv="",   # 시장가
            ), "매도")
            ono = _pick(r, "ord_no", "odno", default="")
            print(f"  [매도주문] {code} {name} {qty}주 ref:{ref_px:,}"
                  + (f"  사유:{reason}" if reason else "") + f" → {ono}")
            try:
                remove_kiwoom_position(code)   # 진입가 원장에서 제거
            except Exception:
                pass
            try:
                import notifier
                notifier.queue_fill("sell", name, code, qty, ref_px)
            except Exception:
                pass
            log_order({"time": datetime.now().strftime("%H:%M:%S"), "side": "sell",
                       "code": code, "name": name, "strategy": strat,
                       "qty": qty, "price": ref_px,
                       "order_type": ORDER_TYPE_SELL, "ok": True, "order_no": ono,
                       "msg": reason})
            n_placed += 1
        except Exception as e:
            print(f"  [실패] {code} {name}: {e}")
            failures.append((code, name, str(e)[:120]))
            try:
                log_order({"time": datetime.now().strftime("%H:%M:%S"), "side": "sell",
                           "code": code, "name": name, "strategy": strat,
                           "qty": qty, "price": ref_px,
                           "order_type": ORDER_TYPE_SELL, "ok": False, "order_no": "",
                           "msg": (reason + " | " if reason else "") + str(e)[:180]})
            except Exception as _le:   # 로그 파일이 잠겨도 나머지 청산·실패 알림은 계속
                print(f"  [warn] 주문로그 기록 실패: {_le}")
        time.sleep(0.7)   # 주문 간 간격 — 연속 발사 시 429 예방(2026-07-14, 실사례 07-13)
    print(f"[sell] 주문 {n_placed}건 완료")
    # [2026-08-20] 청산 실패 = 계획 밖 보유 연장(리스크 사건). 종전엔 [실패] 한 줄만
    # 찍고 exit 0 이라 조용히 반복됐다(KIS 안D 에서 08-11~08-19 실사고). 반드시 알린다.
    if failures:
        _lines = [f"  {c} {n} — {m}" for c, n, m in failures[:5]]
        if len(failures) > 5:
            _lines.append(f"  … 외 {len(failures) - 5}건")
        try:
            import notifier
            notifier.safe_send(
                f"🚨 [{LABEL}] 청산 실패 {len(failures)}건 — 만기/서킷브레이커 매도가 "
                f"집행되지 않았습니다\n" + "\n".join(_lines) +
                "\n  해당 종목은 계획보다 오래 보유 중입니다. 원인 확인 필요.")
        except Exception:
            pass


def _real_loss_check():
    """[§96] 본계좌 일일 손실 한도 — 프로그램 몫의 오늘 손익(전일 종가 대비, 오늘 산 건 매입가 대비)이 -한도 이하이면
    신규 매수 정지(real_account.halt). 매도(만기·손절·익절)는 막지 않는다. 결과는 loss_check.json(대시보드 표시).
    AM(09:00 — 시가 갭 손실)·PM(15:21 매도 직후·보조 매수 직전 — 하루 거의 전부) 두 번 본다. 조회 실패는 판정 생략."""
    lim = _ACC.get("daily_loss_limit")
    try:
        pos = get_positions(get_api())
    except Exception as e:
        print(f"[손실한도] 잔고 조회 실패 — 이번엔 판정 생략: {str(e)[:120]}")
        return
    own = _own(pos)
    pnl, n = _RA.day_pnl(own, today_ordered_codes("buy"), _prev_close_map())
    hit = _RA.loss_limit_hit(pnl, lim)
    try:
        _RA.write_json("loss_check.json", {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pnl": pnl,
                                           "n": n, "limit": lim, "hit": hit, "mode": MODE})
    except Exception:
        pass
    print(f"[손실한도] 프로그램 몫 오늘 손익 {pnl:+,}원 (종목 {n}개) / 한도 "
          + (f"-{int(lim):,}원" if lim else "없음") + (" → 한도 도달" if hit else ""))
    if hit:
        day = _ACC.get("loss_action") == "day"
        _RA.halt_engage("loss_limit", f"오늘 손익 {pnl:+,}원 ≤ 한도 -{int(lim):,}원 (종목 {n}개)",
                        until=datetime.now().strftime("%Y%m%d") if day else None)


def _went_live_today_after_open(now=None):
    """[§96] 실주문 모드로 바뀐 때가 오늘 09:00 이후인가(모드 기록 changed_at 'YYYY-MM-DD HH:MM:SS')."""
    try:
        ch = str(_RA.mode_info().get("changed_at") or "")
        now = now or datetime.now()
        return ch[:10] == now.strftime("%Y-%m-%d") and ch[11:19] >= "09:00:00"
    except Exception:
        return False


def cmd_daily(mode=None):
    """sell → buy 순서로 일괄 실행 (스케줄러용).

    mode: 'am' | 'pm' | None(시계 분기 — 하위호환).
    [2026-07-12] 트리거별 명시 모드 도입 — StartWhenAvailable 지연복구가 정오를 넘기면
    시계 분기가 '매수 트리거'를 매도 경로로 실행(당일 매수 무경보 소실 + 만기·CB 매도가
    마감 동시호가가 아닌 한낮 시장가 조기 발동)하던 문제. 지연복구로 모드와 실제 시각이
    어긋나면 위험한 쪽(주문)을 생략하고 텔레그램으로 알린다.
    """
    hour = datetime.now().hour
    if mode is None:
        mode = "am" if hour < 12 else "pm"

    if mode == "am":
        if hour >= 12:   # 09:03 트리거의 오후 지연복구 — 한낮 시장가 매수는 위험, 생략
            msg = f"⚠ [{TAG}] 09:03 매수 트리거가 정오 이후 지연복구됨 — 당일 매수 생략(가격 이동 위험). 익절 지정가만 점검"
            print(msg)
            try:
                import notifier
                notifier.safe_send(msg)
            except Exception:
                pass
            cmd_place_targets()
        else:
            print("[daily] 오전 모드 — 매수 후 익절 지정가")
            if REAL:
                _real_loss_check()  # [§96] 시가 갭 손실이 한도를 넘었으면 오늘 매수 전에 정지
            cmd_buy()
            if not DRY:
                time.sleep(10)      # 시장가 매수 체결이 잔고에 반영될 시간(드라이런은 주문이 없어 기다릴 필요 없음)
            cmd_place_targets()     # 보유종목 당일 익절 지정가 발주(어제 발주분은 마감 시 만료됨)
    else:
        print("[daily] 오후 모드 — 매도 후 매수")
        cmd_sell()
        if hour < 12:   # 15:21 트리거의 오전 지연복구(주말 넘김 등) — 매수는 09:03 트리거 몫
            msg = f"⚠ [{TAG}] 15:21 매도 트리거가 오전에 지연복구됨 — 매도만 수행, 매수는 정규 09:03 경로에 위임"
            print(msg)
            try:
                import notifier
                notifier.safe_send(msg)
            except Exception:
                pass
        else:
            # ※ 15:21 매도는 15:30 동시호가 체결이라 아래 매수 시점엔 슬롯·예수금이 아직
            #   회복되지 않음 — 오후 매수는 '이미 빈 슬롯'이 있을 때만 동작하는 보조 경로이고,
            #   만기청산으로 비워진 슬롯의 신규 진입은 다음날 09:03 매수가 담당(2026-07-10 명확화).
            if REAL:
                _real_loss_check()  # [§96] 하루 손익(15:20 기준)이 한도를 넘었으면 보조 매수·다음 매수 정지
            if REAL and not DRY and _went_live_today_after_open():
                # [§96] 장중에 실주문을 막 켠 날 — 빈 칸이 전부라 오후 보조 매수가 마감 동시호가에 칸을 한꺼번에 채운다.
                # 백테스트(다음날 시가 진입)와 다르고 예상 밖이라, 첫 매수는 다음 거래일 09:00 정규 경로로 미룬다.
                print("[daily] 오늘 장중에 실주문을 켬 — 첫 매수는 다음 거래일 09:00(시가) 부터, 오후 보조 매수 생략")
            else:
                cmd_buy()
            # 오후 매수분 익절 지정가는 다음날 아침 cmd_place_targets 가 재발주(당일주문 곧 만료라 생략)
    # [2026-07-21] 매매(매도·매수·익절)는 위에서 이미 완료 — 마지막 잔고 스냅샷은 대시보드
    # 표시용이라 여기서 실패해도 매매 결과엔 무영향. 크래시로 exit 1(무서운 '매매 크래시'
    # 텔레그램)을 내지 않도록 비치명 처리하되, 실패 사실은 조용히 알린다(원장 자가치유는
    # 다음 status/daily 실행이 재수행). 단, 매매 도중 실패는 위에서 정상 전파돼 여전히 경보.
    # [2026-08-09] 1회 재시도 + 원인 문구 포함.
    # 종전엔 예외 '타입'만 알려(APIError) 원인을 알 수 없어 8월 간헐 실패(08-06 2회,
    # 08-07 1회)를 사후 진단할 수 없었다. 스냅샷은 표시용이라 잠깐 쉬고 한 번 더
    # 시도해도 매매에 영향이 없으므로, 일시적 레이트리밋/글리치는 재시도로 흡수한다.
    try:
        cmd_status()
    except Exception as _e1:
        print(f"    [warn] 잔고 스냅샷 1차 실패({type(_e1).__name__}: {_e1}) — 3초 후 재시도")
        time.sleep(3.0)
        try:
            cmd_status()
            print("    [ok] 잔고 스냅샷 재시도 성공")
        except Exception as _e2:
            # [2026-08-09 문구 개선] 매매는 끝난 뒤의 '표시용 조회' 실패다. 종전 문구가
            # ⚠+"실패"로 시작해 매매 사고처럼 읽혔다 → 조치 불필요임을 앞에 명시.
            msg = (f"ℹ️ [{TAG}] 매매는 정상 완료됐고, 잔고 화면 갱신만 실패했습니다"
                   "(조치 불필요 — 다음 실행 때 자동으로 맞춰집니다).\n"
                   f"  참고 원인: {type(_e2).__name__}: {str(_e2)[:100]}")
            print(msg)
            try:
                import notifier
                notifier.safe_send(msg)
            except Exception:
                pass


# ── 진입점 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    # 크래시 텔레그램(2026-07-17) — KIS 쪽 '무경보 사망' 실사례(모의서버 장애로 PM
    # ReadTimeout 즉사, 스케줄러 이력에만 흔적)와 동일 클래스 방지. 미처리 예외를
    # 텔레그램으로 격상하고 traceback/종료코드는 원래대로 유지.
    def _crash_hook(_tp, _val, _tb):
        try:
            import notifier
            notifier.safe_send(f"🚨 [{TAG}] {cmd} 크래시: {_tp.__name__}: {_val}"
                               " — 이 시도는 중단. "
                               f"{_retry_note()}")
        except Exception:
            pass
        sys.__excepthook__(_tp, _val, _tb)
    sys.excepthook = _crash_hook

    if REAL:   # [§96] 본계좌 실행 머리말 — 로그만 봐도 어떤 모드로 돌았는지 보이게
        print(f"[본계좌] 모드 {MODE} — "
              + ("실제 주문" if not DRY else "드라이런(주문 안 냄 — 냈을 주문만 dryrun 기록)" if MODE == "dryrun" else "꺼짐"))
        if MODE == "off":
            print("[본계좌] 모드 '끔' — 아무것도 하지 않고 종료")
            sys.exit(0)

    # status 도 락 안에서 — cmd_status 의 ensure(prune/heal)가 원장 '쓰기' 작업이라
    # 락 없이 daily 와 겹치면 load-modify-write 레이스(행 유실/부활) 가능(2026-07-10).
    with kiwoom_lock(timeout=60):
        if cmd == "status":
            cmd_status()
        elif cmd == "buy":
            cmd_buy()
        elif cmd == "sell":
            cmd_sell()
        elif cmd == "targets":
            cmd_place_targets()
        elif cmd == "reconcile":
            cmd_reconcile()
        elif cmd == "daily":
            cmd_daily()
        elif cmd == "daily-am":     # 09:03 트리거 전용(지연복구 가드 포함, 2026-07-12)
            if datetime.now().hour >= 15:
                # [2026-07-17] 장외 재점화 가드 — 캐치업/수동 재실행이 15시 이후 AM 을
                # 점화하면 손절매도까지 장외 주문이 됨. 15시 이후는 PM/익일 소관.
                _m = f"⚠ [{TAG}] daily-am 이 15시 이후 점화됨 — 전체 생략(재점화 가드)"
                print(_m)
                try:
                    import notifier
                    notifier.safe_send(_m)
                except Exception:
                    pass
            else:
                cmd_daily("am")
        elif cmd == "daily-pm":     # 15:21 트리거 전용
            cmd_daily("pm")
            # watchdog 완료 마커(2026-07-17) — 시작 마커는 실행 중 크래시를 못 잡는다
            # (KIS 15:21 ReadTimeout 사망을 [OK] 로 본 실사례). 끝까지 와야 찍힌다.
            print("오후 모드 완료")
        else:
            print(f"[main] 알 수 없는 명령: {cmd}")
            print("사용법: python kiwoom_trader.py [status|buy|sell|targets|reconcile|daily|daily-am|daily-pm]")
            sys.exit(1)

    # 체결 묶음 알림 — 이번 실행에서 쌓인 매수/매도를 1통으로 (없으면 전송 안 함)
    try:
        import notifier
        notifier.flush_fills(f"🧪 [{LABEL} · 드라이런 — 실제 주문 안 함] 실주문이었다면:" if DRY
                             else f"💰 [{LABEL} · 실계좌 주문]" if REAL else f"[{LABEL}]")
    except Exception:
        pass
