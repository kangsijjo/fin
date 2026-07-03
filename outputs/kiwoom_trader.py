"""
키움증권 모의투자 자동 집행기 — v3: 안C 포트폴리오 (4:4:2)

의존: pip install kiwoom-rest-api
인증: .env 의 KIWOOM_MOCK_APP_KEY / KIWOOM_MOCK_APP_SECRET (KIWOOM_ENV=mock)

명령:
  python kiwoom_trader.py status   # 예수금·잔고·미체결 출력
  python kiwoom_trader.py buy      # 전략별 우선순위로 신호 종목 매수
  python kiwoom_trader.py sell     # per-signal holding_days 기준 만기 종목 매도
  python kiwoom_trader.py daily    # sell → buy → status 일괄 (스케줄러용)

전략별 슬롯 구조 (총 10) — 안C 포트폴리오:
  슬롯 1-4  →  high_52w_filt   (52주 신고가+거래량+시장강세, 20일 보유)  max=4
  슬롯 5-8  →  rsi_reversal    (RSI<30 과매도 반전,                5일 보유)  max=4
  슬롯 9-10 →  rsi_vol         (RSI<30 + 거래량 2배 급증,          7일 보유)  max=2

  ※ 변경 이력: 2026-06-17 for_high20_mkt(CAGR 기여 낮음) 제거,
               rsi_reversal +2슬롯, rsi_vol 신규 2슬롯
     백테스트 CAGR: 현재 포트 -2.6% → 안C +11.6% (2019-2025, 총자본 기준)

운용 룰:
  - 매수: 신호 다음 영업일 09:01 시장가. 전략별 빈 슬롯 확인 후 우선순위 배정.
  - 매도: 진입일 포함 holding_days 영업일째 15:21 시장가. 전략마다 보유일 다름.
  - 멱등: 같은 날 같은 종목 중복 주문 방지 (db/kiwoom/orders_*.csv 로그 기준).
  - `daily` 명령: 12시 이전 → 매수, 이후 → 매도.

⚠️ 안전장치: KIWOOM_ENV=prod 면 주문 명령을 거부한다 (조회만 허용).
   실전 전환은 모의 검증 수개월 후 별도 논의 — 그때도 사용자가 직접 결정.

주문유형(trde_tp): 0 지정가 / 3 시장가
"""

import os
import sys
import csv
import time
import json
import glob as _glob
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

# ── 키움 API 파일락 ──────────────────────────────────────────────────────────
# 우리 시스템(kiwoom_trader)과 Stock_AI_Project(kiwoom_extra) 가
# 동시에 키움 API에 접근하는 것을 방지하는 파일 기반 잠금.
# lock 파일: C:/fin/Stock_AI_Project/data/.kiwoom.lock
_LOCK_CANDIDATES = [
    os.getenv("KIWOOM_LOCK", ""),
    "../Stock_AI_Project/data/.kiwoom.lock",
    "C:/fin/Stock_AI_Project/data/.kiwoom.lock",
]
_LOCK_PATH = next((p for p in _LOCK_CANDIDATES if p and os.path.dirname(p)
                   and os.path.isdir(os.path.dirname(p))), None)


@contextmanager
def kiwoom_lock(timeout: int = 30):
    """키움 API 파일락 컨텍스트 매니저.

    다른 프로세스가 이미 잠금을 보유하면 timeout 초 대기 후 경고와 함께 계속.
    (완전 차단 대신 경고 — 토큰이 다른 파일에 저장되므로 충돌 위험이 낮음)
    """
    if _LOCK_PATH is None:
        yield   # lock 경로를 못 찾으면 잠금 없이 진행
        return

    deadline = time.time() + timeout
    while os.path.exists(_LOCK_PATH):
        try:
            with open(_LOCK_PATH, "r") as f:
                info = json.load(f)
            age = time.time() - info.get("ts", 0)
            if age > 300:   # 5분 이상 된 stale lock → 강제 해제
                os.remove(_LOCK_PATH)
                break
            print(f"[kiwoom_lock] 잠금 대기 중 (보유: {info.get('who','?')}, {age:.0f}초 경과)...")
        except Exception:
            break   # lock 파일 손상 → 무시하고 진행

        if time.time() > deadline:
            print("[kiwoom_lock] 대기 시간 초과 — 잠금 무시하고 진행")
            break
        time.sleep(5)

    # 잠금 획득
    try:
        with open(_LOCK_PATH, "w") as f:
            json.dump({"who": f"kiwoom_trader(pid={os.getpid()})",
                       "ts": time.time(), "started": datetime.now().isoformat()}, f)
    except Exception:
        pass   # lock 파일 쓰기 실패해도 동작은 계속

    try:
        yield
    finally:
        try:
            if os.path.exists(_LOCK_PATH):
                os.remove(_LOCK_PATH)
        except Exception:
            pass

import config

SIGNALS_CSV = "./paper_signals.csv"
ORDERS_DIR = "./db/kiwoom"
HOLDING_DAYS_DEFAULT = 40   # 구버전 CSV 호환 fallback
MAX_CONCURRENT = 10

# 전략별 최대 슬롯 수 (합계 = MAX_CONCURRENT) — 안C 포트폴리오 (2026-06-17)
STRATEGY_MAX_SLOTS = {
    "high_52w_filt": 4,
    "rsi_reversal":  4,
    "rsi_vol":       2,
}
# 매수 우선순위 (앞 전략이 빈 슬롯 먼저 채움)
STRATEGY_PRIORITY = ["high_52w_filt", "rsi_reversal", "rsi_vol"]
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
PROFIT_TARGET = {"rsi_vol": 0.20, "high_52w_filt": 0.50}
MIN_ORDER_AMOUNT = 100_000   # 슬롯당 이보다 작으면 주문 생략

# 강도 필터(사용자 결정 2026-07-02): score_ic(0~10, 높을수록 강함)가 이 값 미만인 신호는
# 매수 스킵. 기록 719건 기준 ≥6.0 = 상위 24.5%(신호 과다 완화). 신호 '기록'은 전량 유지
# (strength_logger 는 계속 모든 신호 채점 — 사후검증 데이터 보존), '집행'만 거른다.
# 강도 기록이 없는 신호는 통과(fail-open — 로거 장애가 매수 전면 중단으로 번지지 않게).
MIN_STRENGTH_SCORE = 6.0


# ------------------------------------------------------------
# 공용
# ------------------------------------------------------------
class KiwoomBundle:
    """order/account 클라이언트 묶음."""
    def __init__(self, order, acct):
        self.order = order
        self.acct = acct


def get_api():
    if not config.KIWOOM_APP_KEY or not config.KIWOOM_APP_SECRET:
        print("[ERROR] .env 에 KIWOOM_MOCK_APP_KEY / KIWOOM_MOCK_APP_SECRET 없음")
        sys.exit(1)
    # 라이브러리가 import 시점에 환경변수를 읽으므로 import 전에 주입
    os.environ["KIWOOM_API_KEY"] = config.KIWOOM_APP_KEY
    os.environ["KIWOOM_API_SECRET"] = config.KIWOOM_APP_SECRET
    os.environ["KIWOOM_USE_SANDBOX"] = "false" if config.KIWOOM_ENV == "prod" else "true"
    try:
        from kiwoom_rest_api.config import get_base_url
        from kiwoom_rest_api.auth.token import TokenManager
        from kiwoom_rest_api.koreanstock.order import Order
        from kiwoom_rest_api.koreanstock.account import Account
    except ImportError:
        print("[ERROR] kiwoom-rest-api 미설치 → pip install kiwoom-rest-api")
        sys.exit(1)
    base = get_base_url()
    tm = TokenManager()
    tm.get_token()   # 토큰 발급 검증 (실패 시 예외)
    print(f"[kiwoom] 토큰 발급 OK (env={config.KIWOOM_ENV}, {base})")
    return KiwoomBundle(Order(base_url=base, token_manager=tm),
                        Account(base_url=base, token_manager=tm))


def guard_mock_only():
    if config.KIWOOM_ENV == "prod":
        print("[ABORT] KIWOOM_ENV=prod — 실전 계좌 주문은 이 스크립트에서 차단됨.")
        print("        모의 검증 후 실전 전환은 별도로 진행하세요 (KIWOOM_ENV=mock 으로 변경).")
        sys.exit(1)


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
    path = f"{ORDERS_DIR}/orders_{datetime.today():%Y%m%d}.csv"
    if not os.path.exists(path):
        return set()
    df = pd.read_csv(path, dtype={"code": str})
    if side:
        df = df[df["side"] == side]
    ok = df["ok"].astype(str).str.lower().isin(("true", "1"))  # CSV 재로드 시 문자열 대응
    return set(df[ok]["code"].astype(str).str.zfill(6))


# ------------------------------------------------------------
# 계좌 조회
# ------------------------------------------------------------
def get_deposit(api):
    """주문가능 예수금 (원). 스키마 모르면 raw 출력. (kt00001, qry_tp 3=추정조회)"""
    try:
        r = api.acct.deposit_detail_status_request_kt00001(qry_tp="3")
    except Exception:
        r = api.acct.deposit_detail_status_request_kt00001(qry_tp="2")
    for key in ("ord_alow_amt", "ord_alowa", "100stk_ord_alow_amt",
                "entr", "prsm_dpst_aset_amt", "pymn_alow_amt"):
        v = _pick(r, key)
        if v is not None:
            return _to_int(v)
    print(f"[warn] 예수금 필드 식별 실패 — raw keys: {list(r.keys())[:25]}")
    return 0


def get_positions(api):
    """보유 종목 dict: code -> {qty, name}. (kt00018) 스키마 방어적 파싱."""
    r = api.acct.account_evaluation_balance_detail_request_kt00018(
        query_type="1", domestic_exchange_type="KRX")
    items = None
    for key in ("acnt_evlt_remn_indv_tot", "stk_acnt_evlt_prst", "output", "list"):
        if isinstance(r, dict) and isinstance(r.get(key), list):
            items = r[key]
            break
    if items is None:
        print(f"[warn] 잔고 리스트 식별 실패 — raw keys: {list(r.keys())[:20]}")
        return {}
    pos = {}
    for it in items:
        code = str(_pick(it, "stk_cd", "stock_code", default="")).replace("A", "").zfill(6)
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
            }
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
    """잔고 정합성 점검 — 브로커 보유 vs 직전 스냅샷. 불일치 시 텔레그램 경고."""
    api = get_api()
    pos = get_positions(api)
    broker = set(str(c).zfill(6) for c in pos.keys())
    snap = _snapshot_codes()
    drift_out = sorted(broker - snap)   # 브로커엔 있는데 스냅샷에 없음
    drift_in = sorted(snap - broker)    # 스냅샷엔 있는데 브로커 없음
    print(f"[reconcile] 브로커 보유 {len(pos)}건 / 스냅샷대비 추가{drift_out} 누락{drift_in}")
    if not pos and snap:
        msg = "⛔ [키움 reconcile] 브로커 잔고 0 인데 직전 스냅샷엔 보유 있음 — API 누락 의심"
        print(msg)
        try:
            import notifier; notifier.safe_send(msg)
        except Exception:
            pass
        return False
    if drift_out or drift_in:
        try:
            import notifier
            notifier.safe_send(f"⚠ [키움 잔고] 스냅샷 대비 변동 추가{drift_out}/누락{drift_in} — 확인 권장")
        except Exception:
            pass
    return True


def cmd_status():
    api = get_api()
    dep = get_deposit(api)
    pos = get_positions(api)
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
                "env": config.KIWOOM_ENV,
                "deposit": dep,
                "total_eval": total_eval, "securities": securities, "eval_pnl": eval_pnl,
                "positions": [{"code": c, "name": p["name"], "qty": p["qty"],
                               "price": p.get("price", 0), "avg_price": p.get("avg_price", 0),
                               "pnl": p.get("pnl", 0), "pnl_pct": p.get("pnl_pct", 0)}
                              for c, p in pos.items()]}
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
        if "account" in df.columns:   # 키움 계좌 기록만 (KIS 기록과 혼동 방지)
            df = df[df["account"].astype(str).str.lower().str.startswith("kiwoom")]
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["score_ic"] = pd.to_numeric(df["score_ic"], errors="coerce")
        out = {}
        for _, r in df.iterrows():
            if pd.notna(r["score_ic"]):
                out[(str(r["code"]), str(r.get("strategy", "")))] = float(r["score_ic"])
        return out
    except Exception as e:
        print(f"[warn] 강도 로그 로드 실패(필터 미적용): {e}")
        return {}


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
    return s[s["signal_date"] == target].to_dict("records")


def cmd_buy():
    guard_mock_only()
    sigs = todays_signals()
    if not sigs:
        print("[buy] 오늘 신호 없음 — 종료")
        return
    api = get_api()
    pos = get_positions(api)

    # ── 정합성 게이트(오주문 방지) ──────────────────────────────────────────
    # get_positions 는 스키마 인식 실패/일시 글리치 시 조용히 {} 를 반환할 수 있다.
    # 브로커 0건인데 직전 스냅샷엔 보유가 있었으면 = 응답 누락 의심 → 슬롯 오판으로
    # 이미 보유한 종목에 과다 매수가 날 수 있으므로 강제 중단.
    if not pos and _snapshot_codes():
        print("[buy] 잔고 응답 의심(브로커 0 / 직전 스냅샷 보유 있음) — 오주문 방지 위해 매수 중단")
        try:
            import notifier
            notifier.safe_send("⛔ [키움 buy 중단] 잔고 응답이 비어 있음(스냅샷은 보유). API 누락 의심 → 매수 스킵.")
        except Exception:
            pass
        return

    already = today_ordered_codes("buy")
    dep = get_deposit(api)
    strength = _strength_map(sigs)   # {(code,strategy): score_ic} — 강도 필터용
    if strength:
        n_weak = sum(1 for s in sigs
                     if strength.get((str(s.get("code", "")).zfill(6),
                                      str(s.get("strategy", ""))), 99) < MIN_STRENGTH_SCORE)
        print(f"[buy] 강도 필터: score_ic < {MIN_STRENGTH_SCORE} 스킵 예정 {n_weak}건 / 기록 {len(strength)}건")
    else:
        print("[buy] 강도 기록 없음 — 강도 필터 미적용(fail-open)")

    # ── 전략별 슬롯 현황 ──────────────────────────────────────────────────────
    strategy_map = get_signal_strategy_map()
    slot_used = count_slots_by_strategy(pos.keys(), strategy_map)
    legacy_used = slot_used.get("_legacy", 0)   # 레거시 전략(for_high20_mkt 등) 보유 수
    slot_avail = {k: max(0, STRATEGY_MAX_SLOTS[k] - slot_used[k])
                  for k in STRATEGY_MAX_SLOTS}
    # 전체 가용 슬롯 = 총 슬롯 상한 - 현재 사용(현 전략 + 레거시)
    total_avail = max(0, sum(slot_avail.values()) - legacy_used)

    print(f"[buy] 오늘 신호 {len(sigs)}건  예수금 {dep:,}원")
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

    # ── 전략별로 신호 분류 + 거래대금 내림차순 정렬 ───────────────────────────
    sigs_by_strat = {k: [] for k in STRATEGY_PRIORITY}
    for sig in sigs:
        strat = str(sig.get("strategy", "high_52w_filt"))
        if strat not in sigs_by_strat:
            strat = "high_52w_filt"
        sigs_by_strat[strat].append(sig)
    for strat in STRATEGY_PRIORITY:
        sigs_by_strat[strat].sort(
            key=lambda r: float(tv_map.get(r["code"], 0) or 0), reverse=True)

    # ── 우선순위대로 슬롯 채우기 ─────────────────────────────────────────────
    n_placed = 0
    remaining_dep = dep
    placed_per_strat = {k: 0 for k in STRATEGY_PRIORITY}  # 전략별 실제 배정 수 추적

    for strat in STRATEGY_PRIORITY:
        if n_placed >= total_avail:   # 전역 동시보유 상한(레거시 반영) 도달 — 과다매수 방지(2026-06-29)
            break
        avail = slot_avail[strat]
        if avail <= 0:
            continue
        strat_sigs = sigs_by_strat.get(strat, [])
        print(f"\n  [{strat}] 빈슬롯 {avail}개  후보 {len(strat_sigs)}건")

        placed_this_strat = 0
        for sig in strat_sigs:
            # 전략별 빈슬롯 OR 전역 가용슬롯(레거시 반영) 중 먼저 소진되면 중단
            if placed_this_strat >= avail or n_placed >= total_avail:
                break
            code = str(sig["code"]).zfill(6)
            name = str(sig.get("name", ""))
            close = float(sig.get("entry_price_close", 0) or 0)

            if code in pos or code in already:
                print(f"    [skip] {code} {name} — 이미 보유/주문됨")
                continue
            if close <= 0:
                continue

            # 강도 필터 — score_ic < 6.0 이면 스킵(기록 없으면 통과, 2026-07-02)
            _sc = strength.get((code, strat))
            if _sc is not None and _sc < MIN_STRENGTH_SCORE:
                print(f"    [skip] {code} {name} — 강도 {_sc:.2f} < {MIN_STRENGTH_SCORE:.0f}")
                continue

            # 잔여 빈 슬롯 수로 예산 균등 분배 (각 전략의 실제 배정 수 반영)
            # placed_per_strat[k] 가 이미 현재 전략 포함 모든 배정 수를 추적하므로
            # placed_this_strat 이중 차감 불필요 (버그 수정 2026-06-18)
            total_slots_remaining = sum(
                max(0, slot_avail[k] - placed_per_strat[k])
                for k in STRATEGY_PRIORITY
            )
            if total_slots_remaining <= 0:
                break
            budget = remaining_dep / total_slots_remaining
            qty = int((budget * 0.97) // close)   # 갭상승 대비 3% 여유
            if qty < 1 or qty * close < MIN_ORDER_AMOUNT:
                print(f"    [skip] {code} {name} — 예산 부족 ({budget:,.0f}원)")
                continue

            try:
                r = api.order.stock_buy_order_request_kt10000(
                    dmst_stex_tp="KRX", stk_cd=code, ord_qty=str(qty),
                    trde_tp=ORDER_TYPE_BUY,
                    ord_uv="",
                )
                ono = _pick(r, "ord_no", "odno", default="")
                print(f"    [매수주문] {code} {name} {qty}주 @ {int(close):,} → {ono}")
                try:
                    import notifier
                    notifier.queue_fill("buy", name, code, qty, close)
                except Exception:
                    pass
                log_order({
                    "time": datetime.now().strftime("%H:%M:%S"), "side": "buy",
                    "code": code, "name": name, "strategy": strat,
                    "qty": qty, "price": int(close),
                    "order_type": ORDER_TYPE_BUY, "ok": True, "order_no": ono, "msg": "",
                })
                remaining_dep -= qty * close
                n_placed += 1
                placed_this_strat += 1
                placed_per_strat[strat] += 1
                already.add(code)
            except Exception as e:
                print(f"    [실패] {code} {name}: {e}")
                log_order({
                    "time": datetime.now().strftime("%H:%M:%S"), "side": "buy",
                    "code": code, "name": name, "strategy": strat,
                    "qty": qty, "price": int(close),
                    "order_type": ORDER_TYPE_BUY, "ok": False, "order_no": "",
                    "msg": str(e)[:200],
                })

    print(f"\n[buy] 주문 {n_placed}건 완료")


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


def _held_strategy_map():
    """주문로그 전체에서 {code → 마지막 '성공 매수'의 strategy}.

    get_signal_strategy_map 은 '최신 신호' 기준이라 보유 중 다른 전략 신호가 또 나면
    익절 목표%가 오배정될 수 있음(예: rsi_vol +20% 보유인데 high_52w 신호 → +50% 발주).
    실제 매수 시점의 전략이 진실원천(2026-07-03)."""
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
            for _, r in b.iterrows():   # 파일 오름차순 순회 → 최근 매수가 덮어씀
                out[str(r.get("code", "")).zfill(6)] = str(r.get("strategy", "") or "")
    except Exception:
        pass
    return out


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
        try:
            r = api.order.stock_sell_order_request_kt10001(
                dmst_stex_tp="KRX", stk_cd=code, ord_qty=str(qty),
                trde_tp=ORDER_TYPE_LIMIT, ord_uv=str(tgt_px))
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
    print(f"[target] 익절 지정가 {n}건 발주")


# ------------------------------------------------------------
# 매도 — 40영업일 도달
# ------------------------------------------------------------
def codes_due_for_exit():
    """만기 도달 신호 종목 반환 — 전략별 holding_days 각각 적용.

    원본 모드: 진입일 = 신호 다음 영업일 (시가),
               청산일 = 진입일 포함 holding_days 영업일째 (종가).
    """
    from strategies.daily_loader import load_macro_daily
    if not os.path.exists(SIGNALS_CSV):
        return set()
    s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    s["signal_date"] = s["signal_date"].astype(str)
    s["code"] = s["code"].astype(str).str.zfill(6)

    # v2 schema 호환
    if "holding_days" not in s.columns:
        s["holding_days"] = HOLDING_DAYS_DEFAULT
    s["holding_days"] = pd.to_numeric(s["holding_days"], errors="coerce").fillna(HOLDING_DAYS_DEFAULT).astype(int)

    df = load_macro_daily()
    code_dates = {c: sorted(g["date"].astype(str).tolist())
                  for c, g in df.groupby("code")}
    # ds(load_macro_daily date)·signal_date 와 동일한 대시 없는 'YYYYMMDD'.
    # (과거 '%Y-%m-%d' 대시형이라 ds[exit_i] <= today 가 위치4 '숫자 vs -'로 항상 False →
    #  키움 만기청산이 영영 미발동(보유일 초과해도 매도 안 됨). 2026-06-29 수정.
    #  형제 함수 todays_signals() 는 2026-06-23 에 이미 같은 클래스 수정됨.)
    today = datetime.today().strftime("%Y%m%d")
    due = set()
    for _, r in s.iterrows():
        ds = code_dates.get(r["code"])
        if not ds or r["signal_date"] not in ds:
            continue
        holding = int(r["holding_days"])
        entry_i = ds.index(r["signal_date"]) + 1   # 진입 = 신호 다음 영업일
        exit_i  = entry_i + holding - 1             # 진입일 포함 holding일째
        if exit_i < len(ds) and ds[exit_i] <= today:
            due.add(r["code"])
    return due


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
    due = codes_due_for_exit()
    if not due:
        print("[sell] 청산 대상 없음 — 종료")
        try:
            import notifier
            notifier.flush_fills("📌 키움 익절 체결")
        except Exception:
            pass
        return
    already = today_ordered_codes("sell")
    close_map = _today_close_map()
    tp_open = _today_tp_orders()   # 만기청산 전 취소할 미체결 익절 지정가

    strategy_map = get_signal_strategy_map()
    targets = due & set(pos.keys()) - already
    print(f"[sell] 전략별 보유일 만기: {len(due)}종목 | 실제 보유 대상: {len(targets)}종목")
    n_placed = 0
    for code in sorted(targets):
        qty = pos[code]["qty"]
        name = pos[code]["name"]
        strat = strategy_map.get(str(code).zfill(6), "")
        ref_px = int(close_map.get(code, 0))
        # 미체결 익절 지정가가 물량을 잠그고 있으면 먼저 취소(잔량 전부) 후 시장가 청산
        if code in tp_open:
            try:
                api.order.stock_cancel_order_request_kt10003(
                    dmst_stex_tp="KRX", orig_ord_no=str(tp_open[code]),
                    stk_cd=code, cncl_qty="0")
                print(f"  [취소] {code} {name} 미체결 익절 지정가({tp_open[code]}) 취소")
                time.sleep(0.4)
            except Exception as e:
                print(f"  [warn] {code} 익절 지정가 취소 실패({e}) — 시장가 매도 시도 계속")
        try:
            r = api.order.stock_sell_order_request_kt10001(
                dmst_stex_tp="KRX", stk_cd=code, ord_qty=str(qty),
                trde_tp=ORDER_TYPE_SELL,
                ord_uv="",   # 시장가
            )
            ono = _pick(r, "ord_no", "odno", default="")
            print(f"  [매도주문] {code} {name} {qty}주 ref:{ref_px:,} → {ono}")
            try:
                import notifier
                notifier.queue_fill("sell", name, code, qty, ref_px)
            except Exception:
                pass
            log_order({"time": datetime.now().strftime("%H:%M:%S"), "side": "sell",
                       "code": code, "name": name, "strategy": strat,
                       "qty": qty, "price": ref_px,
                       "order_type": ORDER_TYPE_SELL, "ok": True, "order_no": ono, "msg": ""})
            n_placed += 1
        except Exception as e:
            print(f"  [실패] {code} {name}: {e}")
            log_order({"time": datetime.now().strftime("%H:%M:%S"), "side": "sell",
                       "code": code, "name": name, "strategy": strat,
                       "qty": qty, "price": ref_px,
                       "order_type": ORDER_TYPE_SELL, "ok": False, "order_no": "",
                       "msg": str(e)[:200]})
    print(f"[sell] 주문 {n_placed}건 완료")


def cmd_daily():
    """sell → buy 순서로 일괄 실행 (스케줄러용).

    12시 이전: 매수만 (전일 신호 종목 진입).
    12시 이후: 매도 후 매수 (만기 청산 후 신규 진입).
    """
    if datetime.now().hour < 12:
        print("[daily] 오전 모드 — 매수 후 익절 지정가")
        cmd_buy()
        time.sleep(10)          # 시장가 매수 체결이 잔고에 반영될 시간
        cmd_place_targets()     # 보유종목 당일 익절 지정가 발주(어제 발주분은 마감 시 만료됨)
    else:
        print("[daily] 오후 모드 — 매도 후 매수")
        cmd_sell()
        cmd_buy()
        # 오후 매수분 익절 지정가는 다음날 아침 cmd_place_targets 가 재발주(당일주문 곧 만료라 생략)
    cmd_status()


# ── 진입점 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        cmd_status()
        sys.exit(0)

    with kiwoom_lock(timeout=60):
        if cmd == "buy":
            cmd_buy()
        elif cmd == "sell":
            cmd_sell()
        elif cmd == "targets":
            cmd_place_targets()
        elif cmd == "reconcile":
            cmd_reconcile()
        elif cmd == "daily":
            cmd_daily()
        else:
            print(f"[main] 알 수 없는 명령: {cmd}")
            print("사용법: python kiwoom_trader.py [status|buy|sell|targets|reconcile|daily]")
            sys.exit(1)

    # 체결 묶음 알림 — 이번 실행에서 쌓인 매수/매도를 1통으로 (없으면 전송 안 함)
    try:
        import notifier
        notifier.flush_fills("[키움 안C]")
    except Exception:
        pass
