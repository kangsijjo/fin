"""
한국투자증권 모의투자 자동 집행기 — v2: 안D 포트폴리오 + 전략별 stop-loss

인증: .env 의 KIS_MOCK_APP_KEY / KIS_MOCK_APP_SECRET / KIS_MOCK_ACCOUNT

명령:
  python kis_trader.py status   # 예수금·잔고·슬롯 현황 출력
  python kis_trader.py buy      # 전략별 우선순위로 신호 종목 매수
  python kis_trader.py sell     # 만기 or stop 발동 종목 매도
  python kis_trader.py daily    # sell → buy → status 일괄 (스케줄러용)

전략별 슬롯 구조 (총 10) — 안D 포트폴리오 (2026-06-17):
  슬롯 1-4  →  h52w_for3d_mkt  (52주신고가+외국인3일+시장강세, 20일)  stop -15%
  슬롯 5-8  →  for_high20_mkt  (20일신고가+외국인3일+시장강세,  20일)  stop -10%
  슬롯 9-10 →  gc_for3d        (골든크로스20/60+외국인3일,       15일)  stop -26%

  신호 파일: kis_paper_signals.csv (kis_live_signal.py 생성)
  키움(안C)과 신호 파일 분리 → 두 시스템 독립 운용

운용 룰:
  - 매수: 신호 다음 영업일 09:01 시장가. 전략별 빈 슬롯 확인 후 배정.
  - 매도 조건 (둘 중 먼저 발동):
      1. 만기: 진입일 포함 holding_days 영업일째 15:21 시장가
      2. stop: 전일 종가 ≤ 진입가 × (1 + stop_pct) 이면 당일 09:01 시장가
  - 진입가 추적: db/kis/kis_positions.csv (매수 체결 시 기록)
  - 멱등: db/kiwoom/orders_*.csv 로 중복 주문 방지

⚠️ 안전장치: 이 스크립트는 항상 모의투자(openapivts) URL만 사용한다.
   실전 계좌 주문은 절대 실행되지 않는다.

KIS 모의 TR ID:
  VTTC0802U  매수주문
  VTTC0801U  매도주문
  VTTC8434R  잔고조회
"""

import os
import sys
import csv
import time
import json
import glob as _glob
from contextlib import contextmanager
from datetime import datetime

import requests
import pandas as pd

# ── 상수 ──────────────────────────────────────────────────────────────────────
MOCK_URL         = "https://openapivts.koreainvestment.com:29443"
TOKEN_CACHE      = ".kis_mock_token.json"
SIGNALS_CSV      = "./kis_paper_signals.csv"   # kis_live_signal.py 출력 파일
ORDERS_DIR       = "./db/kiwoom"               # 키움과 공유 (일자별 주문 로그)
KIS_POSITIONS_CSV = "./db/kis/kis_positions.csv"   # KIS 전용 진입가 추적
HOLDING_DAYS_DEFAULT = 20
MAX_CONCURRENT   = 10
MIN_ORDER_AMOUNT = 100_000

STRATEGY_MAX_SLOTS = {
    "h52w_for3d_mkt": 4,
    "for_high20_mkt": 4,
    "gc_for3d":       2,
}
STRATEGY_PRIORITY = ["h52w_for3d_mkt", "for_high20_mkt", "gc_for3d"]

# 전략별 stop-loss (백테스트 최적, delta 양수)
# h52w_for3d_mkt: stop-15  delta +0.14%
# for_high20_mkt: stop-10  delta +0.22%
# gc_for3d:       stop-26  delta +0.07%
STRATEGY_STOP = {
    "h52w_for3d_mkt": -0.15,
    "for_high20_mkt": -0.10,
    "gc_for3d":       -0.26,
}


# ── 환경변수 로드 ──────────────────────────────────────────────────────────────
def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.split("#")[0].strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_dotenv()

_APP_KEY    = os.getenv("KIS_MOCK_APP_KEY", "")
_APP_SECRET = os.getenv("KIS_MOCK_APP_SECRET", "")
_ACCOUNT    = os.getenv("KIS_MOCK_ACCOUNT", "")

if not _APP_KEY or not _APP_SECRET or not _ACCOUNT:
    print("[ERROR] .env 에 KIS_MOCK_APP_KEY / KIS_MOCK_APP_SECRET / KIS_MOCK_ACCOUNT 없음")
    sys.exit(1)

_CANO         = _ACCOUNT[:8]
_ACNT_PRDT_CD = _ACCOUNT[8:] if len(_ACCOUNT) > 8 else "01"


# ── 파일락 (kiwoom_trader와 동일 락 파일 공유) ──────────────────────────────────
_LOCK_CANDIDATES = [
    os.getenv("KIWOOM_LOCK", ""),
    "../Stock_AI_Project/data/.kiwoom.lock",
    "C:/fin/Stock_AI_Project/data/.kiwoom.lock",
]
_LOCK_PATH = next(
    (p for p in _LOCK_CANDIDATES
     if p and os.path.dirname(p) and os.path.isdir(os.path.dirname(p))),
    None
)


@contextmanager
def kis_lock(timeout: int = 60):
    if _LOCK_PATH is None:
        yield
        return
    deadline = time.time() + timeout
    while os.path.exists(_LOCK_PATH):
        try:
            with open(_LOCK_PATH) as f:
                info = json.load(f)
            age = time.time() - info.get("ts", 0)
            if age > 300:
                os.remove(_LOCK_PATH)
                break
            print(f"[kis_lock] 대기 중 ({info.get('who','?')}, {age:.0f}s)...")
        except Exception:
            break
        if time.time() > deadline:
            print("[kis_lock] 타임아웃 — 잠금 무시하고 진행")
            break
        time.sleep(5)
    try:
        with open(_LOCK_PATH, "w") as f:
            json.dump({"who": f"kis_trader(pid={os.getpid()})",
                       "ts": time.time(), "started": datetime.now().isoformat()}, f)
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            if os.path.exists(_LOCK_PATH):
                os.remove(_LOCK_PATH)
        except Exception:
            pass


# ── KIS 모의투자 클라이언트 ────────────────────────────────────────────────────
class KISMockClient:
    """항상 openapivts(모의) URL만 사용. 실전 계좌는 절대 접근 불가."""

    def __init__(self):
        self._token = None

    def _load_cached_token(self):
        if not os.path.exists(TOKEN_CACHE):
            return None
        try:
            with open(TOKEN_CACHE) as f:
                d = json.load(f)
            if d.get("expire", 0) - 300 > time.time():
                return d["token"]
        except Exception:
            pass
        return None

    def _request_token(self):
        r = requests.post(
            f"{MOCK_URL}/oauth2/tokenP",
            headers={"Content-Type": "application/json"},
            json={"grant_type": "client_credentials",
                  "appkey": _APP_KEY, "appsecret": _APP_SECRET},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        token = d["access_token"]
        expire = time.time() + int(d.get("expires_in", 86400))
        with open(TOKEN_CACHE, "w") as f:
            json.dump({"token": token, "expire": expire}, f)
        return token

    def get_token(self):
        cached = self._load_cached_token()
        if cached:
            return cached
        token = self._request_token()
        print(f"[KIS 모의] 토큰 발급 OK ({MOCK_URL})")
        return token

    def _hdrs(self, tr_id, hashkey=None):
        h = {
            "Content-Type":  "application/json; charset=utf-8",
            "authorization": f"Bearer {self.get_token()}",
            "appkey":        _APP_KEY,
            "appsecret":     _APP_SECRET,
            "tr_id":         tr_id,
            "custtype":      "P",
        }
        if hashkey:
            h["hashkey"] = hashkey
        return h

    def _hashkey(self, body: dict) -> str:
        try:
            r = requests.post(
                f"{MOCK_URL}/oauth2/Hashkey",
                headers={"Content-Type": "application/json",
                         "appkey": _APP_KEY, "appsecret": _APP_SECRET},
                json=body, timeout=10,
            )
            r.raise_for_status()
            return r.json().get("HASH", "")
        except Exception as e:
            print(f"[warn] hashkey 실패: {e}")
            return ""

    def get_balance(self):
        """잔고 조회 → (deposit, positions).

        deposit   : int — 예수금(주문가능)
        positions : dict[code → {qty, name}]
        """
        params = {
            "CANO": _CANO, "ACNT_PRDT_CD": _ACNT_PRDT_CD,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
            "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        r = requests.get(
            f"{MOCK_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=self._hdrs("VTTC8434R"),
            params=params, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"잔고조회 오류: {data.get('msg1','')}")

        o2 = data.get("output2", [{}])
        o2d = o2[0] if o2 else {}
        dep_raw = o2d.get("dnca_tot_amt", "0")
        deposit = _to_int(dep_raw)

        # 총평가금액·평가손익 등 요약(앱 헤드라인과 동일 항목). 반환 시그니처는 유지하고
        # 마지막 조회 요약을 속성에 보관 → cmd_status 가 스냅샷에 함께 기록.
        self.last_summary = {
            "deposit":      deposit,                                       # 예수금총금액(정산완료분)
            "orderable":    _to_int(o2d.get("prvs_rcdl_excc_amt", 0)),    # 가수도정산금액(D+2 정산대기 포함 주문가능)
            "total_eval":   _to_int(o2d.get("tot_evlu_amt", 0)),          # 총평가금액(앱 헤드라인)
            "securities":   _to_int(o2d.get("scts_evlu_amt", 0)),         # 유가증권 평가금액
            "eval_pnl":     _to_int(o2d.get("evlu_pfls_smtl_amt", 0)),    # 평가손익합계
            "purchase_amt": _to_int(o2d.get("pchs_amt_smtl_amt", 0)),     # 매입금액합계
        }

        positions = {}
        for it in data.get("output1", []):
            code = str(it.get("pdno", "")).zfill(6)
            qty  = _to_int(it.get("hldg_qty", "0"))
            name = it.get("prdt_name", "")
            if code and qty > 0:
                positions[code] = {
                    "qty": qty, "name": name,
                    "price":     _to_int(it.get("prpr", 0)),          # 현재가(장중 라이브)
                    "avg_price": _to_int(it.get("pchs_avg_pric", 0)),  # 매입평균
                    "pnl_pct":   _to_float(it.get("evlu_pfls_rt", 0)), # 평가손익률(%)
                }
        return deposit, positions

    def order_buy(self, code: str, qty: int) -> str:
        """시장가 매수. 주문번호(odno) 반환."""
        body = {
            "CANO": _CANO, "ACNT_PRDT_CD": _ACNT_PRDT_CD,
            "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0",
        }
        hk = self._hashkey(body)
        r = requests.post(
            f"{MOCK_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._hdrs("VTTC0802U", hk),
            json=body, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"매수 오류: {data.get('msg1','')}")
        return data.get("output", {}).get("odno", "")

    def order_sell(self, code: str, qty: int) -> str:
        """시장가 매도. 주문번호(odno) 반환."""
        body = {
            "CANO": _CANO, "ACNT_PRDT_CD": _ACNT_PRDT_CD,
            "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0",
        }
        hk = self._hashkey(body)
        r = requests.post(
            f"{MOCK_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._hdrs("VTTC0801U", hk),
            json=body, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"매도 오류: {data.get('msg1','')}")
        return data.get("output", {}).get("odno", "")


def _to_int(v, default=0):
    try:
        return int(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return default


def _to_float(v, default=0.0):
    # get_balance 의 evlu_pfls_rt(평가손익률 %) 등 실수 필드용 — 2026-06-29 추가.
    # (과거 line 275 가 미정의 _to_float 를 호출해 get_balance NameError → 잔고/매수/매도 전부 크래시)
    try:
        return float(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return default


# ── 진입가 추적 (stop-loss 계산용) ────────────────────────────────────────────
def load_kis_positions():
    """kis_positions.csv → {code: {entry_px, strategy, signal_date, holding_days}}.

    매수 체결 시 기록, 매도 체결 시 삭제.
    """
    path = KIS_POSITIONS_CSV
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype={"code": str})
        df["code"] = df["code"].astype(str).str.zfill(6)
        result = {}
        for _, r in df.iterrows():
            result[r["code"]] = {
                "entry_px":    float(r.get("entry_px", 0)),
                "strategy":    str(r.get("strategy", "")),
                "signal_date": str(r.get("signal_date", "")),
                "holding_days": _to_int(r.get("holding_days"), HOLDING_DAYS_DEFAULT),
            }
        return result
    except Exception as e:
        print(f"[warn] kis_positions 로드 실패: {e}")
        return {}


def save_kis_position(code, entry_px, strategy, signal_date, holding_days):
    """매수 후 진입가 기록."""
    os.makedirs(os.path.dirname(KIS_POSITIONS_CSV), exist_ok=True)
    existing = load_kis_positions()
    existing[str(code).zfill(6)] = {
        "entry_px": entry_px,
        "strategy": strategy,
        "signal_date": signal_date,
        "holding_days": holding_days,
    }
    _write_positions(existing)


def remove_kis_position(code):
    """매도 후 진입가 레코드 삭제."""
    existing = load_kis_positions()
    existing.pop(str(code).zfill(6), None)
    _write_positions(existing)


def _write_positions(pos_dict):
    os.makedirs(os.path.dirname(KIS_POSITIONS_CSV), exist_ok=True)
    fields = ["code", "entry_px", "strategy", "signal_date", "holding_days"]
    with open(KIS_POSITIONS_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for code, d in pos_dict.items():
            w.writerow({"code": code, **d})


# ── 공용 헬퍼 ─────────────────────────────────────────────────────────────────
def log_order(row):
    os.makedirs(ORDERS_DIR, exist_ok=True)
    path = f"{ORDERS_DIR}/kis_orders_{datetime.today():%Y%m%d}.csv"
    exists = os.path.exists(path)
    fields = ["time", "side", "code", "name", "strategy", "qty", "price",
              "order_type", "reason", "ok", "order_no", "msg"]
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def get_signal_strategy_map():
    """kis_paper_signals.csv → {code: strategy} (최신 신호 기준)."""
    if not os.path.exists(SIGNALS_CSV):
        return {}
    try:
        s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
        s["code"] = s["code"].astype(str).str.zfill(6)
        if "strategy" not in s.columns:
            return {c: "h52w_for3d_mkt" for c in s["code"].unique()}
        s = s.sort_values("signal_date", ascending=False).drop_duplicates("code", keep="first")
        return dict(zip(s["code"], s["strategy"]))
    except Exception:
        return {}


def count_slots_by_strategy(pos_codes, strategy_map):
    """보유 종목 → 전략별 슬롯 사용 수. 안D에 없는 전략은 _legacy."""
    counts = {k: 0 for k in STRATEGY_MAX_SLOTS}
    counts["_legacy"] = 0
    for code in pos_codes:
        strat = strategy_map.get(str(code).zfill(6), "h52w_for3d_mkt")
        if strat in STRATEGY_MAX_SLOTS:
            counts[strat] += 1
        else:
            counts["_legacy"] += 1
    return counts


def today_ordered_codes(side=None):
    path = f"{ORDERS_DIR}/kis_orders_{datetime.today():%Y%m%d}.csv"
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, dtype={"code": str})
        if side:
            df = df[df["side"] == side]
        ok = df["ok"].astype(str).str.lower().isin(("true", "1"))
        return set(df[ok]["code"].astype(str).str.zfill(6))
    except Exception:
        return set()


def latest_macro_date():
    files = sorted(_glob.glob("./macro_data/daily/*.csv"))
    return os.path.basename(files[-1])[:-4] if files else None


def _today_close_map():
    """최신 macro CSV에서 종가 맵 {code: close}.

    오늘 macro가 15:21 시점엔 아직 미갱신 → latest_macro_date()로 가장 최근 파일 사용.
    stop 체크는 '전일 종가 <= 진입가 x (1+stop_pct)' 기준이므로 정상 동작.
    """
    latest = latest_macro_date()
    if not latest:
        return {}
    path = f"./macro_data/daily/{latest}.csv"
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


# ── 청산 판단 ─────────────────────────────────────────────────────────────────
def codes_due_for_exit(close_map):
    """만기 도달 or stop 발동 종목 → {code: reason}.

    reason: 'expire' | 'stop'
    stop 판단: 전일 종가(close_map) ≤ 진입가 × (1 + stop_pct)
    만기 판단: 진입일 포함 holding_days 영업일째
    """
    from strategies.daily_loader import load_macro_daily
    # 대시 없는 'YYYYMMDD' — ds(load_macro_daily date)·signal_date·target_exit_date 와 동일 형식.
    # (과거 '%Y-%m-%d' 대시형이라 ds[xi] <= today 가 위치4 '숫자 vs -' 로 항상 False →
    #  KIS 만기청산이 영영 미발동하던 버그. 2026-06-29 수정. 과거 매수 0건 버그와 동일 클래스.)
    today = datetime.today().strftime("%Y%m%d")
    due = {}

    # ── kis_positions.csv의 target_exit_date 기반 즉시 만기 ───────────────────
    # (kis_paper_signals.csv 없거나 수동 등록 종목 대비 fallback)
    try:
        if os.path.exists(KIS_POSITIONS_CSV):
            _pdf = pd.read_csv(KIS_POSITIONS_CSV, dtype={"code": str})
            _pdf["code"] = _pdf["code"].astype(str).str.zfill(6)
            if "target_exit_date" in _pdf.columns:
                for _, _pr in _pdf.iterrows():
                    _c = str(_pr["code"])
                    _tgt = str(_pr.get("target_exit_date", ""))
                    if _tgt and _tgt <= today:
                        due[_c] = "expire"
    except Exception:
        pass

    if not os.path.exists(SIGNALS_CSV):
        # stop 체크도 kis_positions 기반으로 수행
        kis_pos = load_kis_positions()
        for _c, pos_info in kis_pos.items():
            if _c in due:
                continue
            strat = str(pos_info.get("strategy", ""))
            stop_pct = STRATEGY_STOP.get(strat)
            if stop_pct is None:
                continue
            entry_px = float(pos_info.get("entry_px", 0))
            if entry_px <= 0:
                continue
            cur_close = close_map.get(_c, 0)
            if cur_close <= 0:
                continue
            stop_px = entry_px * (1 + stop_pct)
            if cur_close <= stop_px:
                due[_c] = "stop"
        return due

    s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    s["signal_date"] = s["signal_date"].astype(str)
    s["code"] = s["code"].astype(str).str.zfill(6)
    if "holding_days" not in s.columns:
        s["holding_days"] = HOLDING_DAYS_DEFAULT
    s["holding_days"] = pd.to_numeric(s["holding_days"], errors="coerce").fillna(HOLDING_DAYS_DEFAULT).astype(int)
    if "strategy" not in s.columns:
        s["strategy"] = "h52w_for3d_mkt"

    df = load_macro_daily()
    code_dates = {c: sorted(g["date"].astype(str).tolist())
                  for c, g in df.groupby("code")}
    kis_pos = load_kis_positions()

    for _, r in s.iterrows():
        code = r["code"]
        ds = code_dates.get(code)
        if not ds or r["signal_date"] not in ds:
            continue

        # 만기 체크
        holding = int(r["holding_days"])
        ei = ds.index(r["signal_date"]) + 1
        xi = ei + holding - 1
        if xi < len(ds) and ds[xi] <= today:
            due[code] = "expire"
            continue

        # stop 체크 (진입가 추적 있을 때만)
        strat = str(r.get("strategy", "h52w_for3d_mkt"))
        stop_pct = STRATEGY_STOP.get(strat)
        if stop_pct is None:
            continue
        pos_info = kis_pos.get(code)
        if pos_info is None:
            continue
        entry_px = float(pos_info.get("entry_px", 0))
        if entry_px <= 0:
            continue
        cur_close = close_map.get(code, 0)
        if cur_close <= 0:
            continue
        stop_px = entry_px * (1 + stop_pct)
        if cur_close <= stop_px:
            due[code] = "stop"

    return due


# ── 명령 구현 ─────────────────────────────────────────────────────────────────
def cmd_status():
    client = KISMockClient()
    deposit, positions = client.get_balance()
    strategy_map = get_signal_strategy_map()
    slot_used = count_slots_by_strategy(positions.keys(), strategy_map)
    kis_pos = load_kis_positions()

    _sm = getattr(client, "last_summary", {}) or {}
    print(f"\n[KIS 모의 — 안D] 총평가금액: {_sm.get('total_eval', 0):,} 원")
    print(f"  예수금(정산완료) {deposit:,}  /  주문가능(D+2정산포함) {_sm.get('orderable', 0):,}"
          f"  /  보유평가 {_sm.get('securities', 0):,}")
    print(f"[평가손익] {_sm.get('eval_pnl', 0):,} 원  (매입합계 {_sm.get('purchase_amt', 0):,})")
    print(f"[보유 종목] {len(positions)} / {MAX_CONCURRENT} 슬롯")
    for strat in STRATEGY_PRIORITY:
        stop_pct = STRATEGY_STOP[strat]
        print(f"  {strat}: {slot_used[strat]}/{STRATEGY_MAX_SLOTS[strat]}"
              f"  (stop {stop_pct*100:.0f}%)")
    for code, p in positions.items():
        pos_info = kis_pos.get(code, {})
        entry_px = pos_info.get("entry_px", 0)
        strat = strategy_map.get(code, "")
        print(f"  {code} {p['name']}: {p['qty']:,}주"
              f"  진입가:{entry_px:,.0f}  전략:{strat}")

    # 스냅샷 저장
    try:
        os.makedirs(ORDERS_DIR, exist_ok=True)
        snap = {
            "date":      datetime.today().strftime("%Y%m%d"),
            "time":      datetime.now().strftime("%H:%M"),
            "broker":    "KIS_mock_andD",
            "deposit":   deposit,
            "orderable":    _sm.get("orderable"),      # 주문가능(D+2 정산대기 포함)
            "total_eval":   _sm.get("total_eval"),     # 총평가금액(앱 헤드라인)
            "securities":   _sm.get("securities"),     # 보유 평가금액
            "eval_pnl":     _sm.get("eval_pnl"),       # 평가손익합계
            "purchase_amt": _sm.get("purchase_amt"),   # 매입금액합계
            "positions": [{"code": c, "name": p["name"], "qty": p["qty"],
                           "price":     p.get("price", 0),       # 현재가
                           "avg_price": p.get("avg_price", 0),   # 매입평균
                           # 평가손익(원) = 수량 × (현재가 − 매입평균). API 가 금액을
                           # 안 주므로 계산(키움 스냅샷 pnl 과 동일 의미).
                           "pnl":       p.get("qty", 0) * (p.get("price", 0) - p.get("avg_price", 0)),
                           "pnl_pct":   p.get("pnl_pct", 0)}
                          for c, p in positions.items()],
        }
        with open(f"{ORDERS_DIR}/kis_snapshot.json", "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        hist = f"{ORDERS_DIR}/kis_equity_history.csv"
        new_file = not os.path.exists(hist)
        with open(hist, "a", newline="", encoding="utf-8-sig") as f:
            if new_file:
                f.write("date,time,deposit,n_positions\n")
            f.write(f"{snap['date']},{snap['time']},{deposit},{len(positions)}\n")
    except Exception as e:
        print(f"[warn] 스냅샷 저장 실패: {e}")


def todays_signals():
    """직전 영업일 신호 목록 (kis_paper_signals.csv 기준)."""
    if not os.path.exists(SIGNALS_CSV):
        print(f"[buy][WARN] {SIGNALS_CSV} 없음 — kis_live_signal.py 먼저 실행 필요")
        return []
    s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    s["signal_date"] = s["signal_date"].astype(str)
    s["code"] = s["code"].astype(str).str.zfill(6)
    if "strategy" not in s.columns:
        s["strategy"] = "h52w_for3d_mkt"
    if "holding_days" not in s.columns:
        s["holding_days"] = HOLDING_DAYS_DEFAULT
    s["holding_days"] = pd.to_numeric(s["holding_days"], errors="coerce").fillna(HOLDING_DAYS_DEFAULT).astype(int)

    target_raw = latest_macro_date()
    if not target_raw:
        return []
    # [fix 2026-06-23] CSV signal_date 는 'YYYYMMDD'(대시 없음). 과거엔 target 을 대시형식으로
    #   바꿔 비교 → 항상 0건 → 매수 안 됨. 양쪽 대시 제거 후 비교.
    s["signal_date"] = s["signal_date"].str.replace("-", "", regex=False)
    target = target_raw.replace("-", "")
    today_raw = datetime.today().strftime("%Y%m%d")
    if target_raw == today_raw:
        return []
    print(f"[buy] 신호 기준일: {target}")
    return s[s["signal_date"] == target].to_dict("records")


def _reconcile_diff(positions):
    """브로커 잔고 vs 로컬 추적(kis_positions.csv) 불일치 계산.
    반환: (orphan, stale) — orphan=브로커만 보유(추적 누락), stale=로컬만 보유(브로커 없음).
    """
    broker = set(str(c).zfill(6) for c in positions.keys())
    local = set(str(c).zfill(6) for c in load_kis_positions().keys())
    return sorted(broker - local), sorted(local - broker)


def cmd_reconcile():
    """장 시작 직전/직후 잔고 정합성 점검. 불일치 시 텔레그램 경고."""
    client = KISMockClient()
    _, positions = client.get_balance()
    orphan, stale = _reconcile_diff(positions)
    print(f"[reconcile] 브로커 보유 {len(positions)}건 / orphan(브로커만) {orphan} / stale(로컬만) {stale}")
    if orphan or stale:
        msg = (f"⚠ 잔고 정합성 불일치\n브로커만(추적누락): {orphan}\n로컬만(브로커없음): {stale}\n"
               f"→ 손절·매수 관리 사각지대. 확인 필요.")
        try:
            import notifier
            notifier.safe_send(msg)
        except Exception:
            pass
        return False
    print("[reconcile] 일치 — 정상")
    return True


def cmd_buy():
    sigs = todays_signals()
    if not sigs:
        print("[buy] 오늘 신호 없음 — 종료")
        return

    client = KISMockClient()
    deposit, positions = client.get_balance()

    # ── 정합성 게이트(오주문 방지) ──────────────────────────────────────────
    # 브로커 잔고가 비었는데 로컬 추적엔 보유가 있으면 = API 응답 누락 의심.
    # 이 상태로 슬롯을 세면 '다 비었다'고 오판해 과다 매수 → 강제 중단.
    orphan, stale = _reconcile_diff(positions)
    if not positions and load_kis_positions():
        print("[buy] 잔고 응답 의심(브로커 0 / 로컬 보유 있음) — 오주문 방지 위해 매수 중단")
        try:
            import notifier
            notifier.safe_send("⛔ [KIS buy 중단] 잔고 응답이 비어 있음(로컬은 보유). API 누락 의심 → 매수 스킵.")
        except Exception:
            pass
        return
    if orphan or stale:
        print(f"[buy] 정합성 경고 — orphan(브로커만){orphan} / stale(로컬만){stale} (경고만, 매수는 진행)")
        try:
            import notifier
            notifier.safe_send(f"⚠ [KIS buy] 잔고 불일치 orphan{orphan}/stale{stale} — 진행하되 확인 권장")
        except Exception:
            pass

    already = today_ordered_codes("buy")
    strategy_map = get_signal_strategy_map()
    slot_used = count_slots_by_strategy(positions.keys(), strategy_map)
    legacy_used = slot_used.get("_legacy", 0)
    slot_avail = {k: max(0, STRATEGY_MAX_SLOTS[k] - slot_used[k])
                  for k in STRATEGY_MAX_SLOTS}
    total_avail = max(0, sum(slot_avail.values()) - legacy_used)

    print(f"[buy] 오늘 신호 {len(sigs)}건  예수금 {deposit:,}원")
    print("  전략별 슬롯: "
          + " / ".join(f"{k}={slot_used[k]}/{STRATEGY_MAX_SLOTS[k]}"
                       for k in STRATEGY_PRIORITY))
    if total_avail <= 0:
        print("[buy] 모든 슬롯 가득 — 주문 없음")
        return

    # 거래대금 맵 (우선순위 정렬용)
    tv_map = {}
    macro_csv = f"./macro_data/daily/{latest_macro_date()}.csv"
    if os.path.exists(macro_csv):
        md = pd.read_csv(macro_csv, encoding="utf-8-sig", dtype={"code": str})
        md["code"] = md["code"].astype(str).str.zfill(6)
        for col_cand in ("거래대금", "trading_value"):
            if col_cand in md.columns:
                tv_map = dict(zip(md["code"], md[col_cand]))
                break

    # 전략별 신호 분류 + 거래대금 내림차순
    sigs_by_strat = {k: [] for k in STRATEGY_PRIORITY}
    for sig in sigs:
        strat = str(sig.get("strategy", "h52w_for3d_mkt"))
        if strat not in sigs_by_strat:
            strat = "h52w_for3d_mkt"
        sigs_by_strat[strat].append(sig)
    for strat in STRATEGY_PRIORITY:
        sigs_by_strat[strat].sort(
            key=lambda r: float(tv_map.get(r["code"], 0) or 0), reverse=True)

    n_placed = 0
    remaining_dep = deposit
    placed_per_strat = {k: 0 for k in STRATEGY_PRIORITY}

    for strat in STRATEGY_PRIORITY:
        if n_placed >= total_avail:   # 전역 동시보유 상한(레거시 반영) 도달 — 과다매수 방지(2026-06-29)
            break
        avail = slot_avail[strat]
        if avail <= 0:
            continue
        strat_sigs = sigs_by_strat.get(strat, [])
        print(f"\n  [{strat}] 빈슬롯 {avail}개  후보 {len(strat_sigs)}건  stop {STRATEGY_STOP[strat]*100:.0f}%")

        placed_this = 0
        for sig in strat_sigs:
            # 전략별 빈슬롯 OR 전역 가용슬롯(레거시 반영) 중 먼저 소진되면 중단
            if placed_this >= avail or n_placed >= total_avail:
                break
            code = str(sig["code"]).zfill(6)
            name = str(sig.get("name", ""))
            close = float(sig.get("entry_price_close", 0) or 0)

            if code in positions or code in already:
                print(f"    [skip] {code} {name} — 이미 보유/주문됨")
                continue
            if close <= 0:
                continue

            # placed_per_strat[k] 가 이미 현재 전략 포함 모든 배정 수를 추적하므로
            # placed_this 이중 차감 불필요 (버그 수정 2026-06-18)
            total_rem = sum(
                max(0, slot_avail[k] - placed_per_strat[k])
                for k in STRATEGY_PRIORITY
            )
            if total_rem <= 0:
                break
            budget = remaining_dep / total_rem
            qty = int((budget * 0.97) // close)
            if qty < 1 or qty * close < MIN_ORDER_AMOUNT:
                print(f"    [skip] {code} {name} — 예산 부족 ({budget:,.0f}원)")
                continue

            try:
                ono = client.order_buy(code, qty)
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
                    "order_type": "시장가(VTTC0802U)", "reason": "signal",
                    "ok": True, "order_no": ono, "msg": "",
                })
                # 진입가 기록
                save_kis_position(
                    code=code,
                    entry_px=close,
                    strategy=strat,
                    signal_date=str(sig.get("signal_date", "")),
                    holding_days=_to_int(sig.get("holding_days"), HOLDING_DAYS_DEFAULT),
                )
                remaining_dep -= qty * close
                n_placed += 1
                placed_this += 1
                placed_per_strat[strat] += 1
                already.add(code)
            except Exception as e:
                print(f"    [실패] {code} {name}: {e}")
                log_order({
                    "time": datetime.now().strftime("%H:%M:%S"), "side": "buy",
                    "code": code, "name": name, "strategy": strat,
                    "qty": qty, "price": int(close),
                    "order_type": "시장가(VTTC0802U)", "reason": "signal",
                    "ok": False, "order_no": "", "msg": str(e)[:200],
                })

    print(f"\n[buy] 주문 {n_placed}건 완료")


def cmd_sell():
    """만기 or stop 발동 종목 매도.

    stop 판단: 전일 종가 <= 진입가 x (1 + stop_pct)
    """
    close_map = _today_close_map()
    due = codes_due_for_exit(close_map)
    if not due:
        print("[sell] 청산 대상 없음 — 종료")
        return

    client = KISMockClient()
    _, positions = client.get_balance()
    already = today_ordered_codes("sell")
    strategy_map = get_signal_strategy_map()

    targets = (set(due.keys()) & set(positions.keys())) - already
    expire_n = sum(1 for c in targets if due.get(c) == "expire")
    stop_n   = sum(1 for c in targets if due.get(c) == "stop")
    print(f"[sell] 청산 대상: 만기 {expire_n}건  stop발동 {stop_n}건  실제보유 {len(targets)}건")

    n_placed = 0
    for code in sorted(targets):
        qty    = positions[code]["qty"]
        name   = positions[code]["name"]
        strat  = strategy_map.get(str(code).zfill(6), "")
        reason = due.get(code, "expire")
        ref_px = int(close_map.get(code, 0))
        try:
            ono = client.order_sell(code, qty)
            print(f"  [매도주문] {code} {name} {qty}주 ref:{ref_px:,}  사유:{reason} → {ono}")
            try:
                import notifier
                notifier.queue_fill("sell", name, code, qty, ref_px)
            except Exception:
                pass
            log_order({
                "time": datetime.now().strftime("%H:%M:%S"), "side": "sell",
                "code": code, "name": name, "strategy": strat,
                "qty": qty, "price": ref_px,
                "order_type": "시장가(VTTC0801U)", "reason": reason,
                "ok": True, "order_no": ono, "msg": "",
            })
            remove_kis_position(code)
            n_placed += 1
        except Exception as e:
            print(f"  [실패] {code} {name}: {e}")
            log_order({
                "time": datetime.now().strftime("%H:%M:%S"), "side": "sell",
                "code": code, "name": name, "strategy": strat,
                "qty": qty, "price": ref_px,
                "order_type": "시장가(VTTC0801U)", "reason": reason,
                "ok": False, "order_no": "", "msg": str(e)[:200],
            })

    print(f"[sell] 주문 {n_placed}건 완료")


def cmd_stop_check():
    """장중 손절 '모니터 전용'(매도 안 함). 15분마다 호출.

    ※ 2026-06-21: 장중-저가 기준 손절 재검증 결과, 안D 전략 2/3 에서 장중 손절이
       오히려 수익을 깎음(h52w_for3d_mkt Δ-0.48 / for_high20_mkt Δ-0.33).
       손절 이득은 '종가(EOD) 기준'일 때만 유효 → 실제 청산은 15:21 cmd_sell(종가기준)이
       담당하고, 이 함수는 자동매도하지 않는다. 대시보드 조기경보 표시용으로만 현황 기록.
    """
    client = KISMockClient()
    _, positions = client.get_balance()
    monitor = []
    if positions:
        kis_pos = load_kis_positions()
        strategy_map = get_signal_strategy_map()
        for code, p in positions.items():
            strat = strategy_map.get(str(code).zfill(6), "")
            stop_pct = STRATEGY_STOP.get(strat)
            if stop_pct is None:
                continue
            cur = float(p.get("price", 0) or 0)
            entry_px = float(kis_pos.get(code, {}).get("entry_px", 0) or 0)
            if entry_px <= 0:
                entry_px = float(p.get("avg_price", 0) or 0)
            if cur <= 0 or entry_px <= 0:
                continue
            pnl = cur / entry_px - 1
            room = (pnl - stop_pct) * 100   # 손절선까지 여유(%p). 0 이하면 EOD 청산 예정.
            monitor.append({
                "code": code, "name": p["name"], "pnl_pct": round(pnl * 100, 2),
                "stop_pct": round(stop_pct * 100, 1), "room_pp": round(room, 2),
                "imminent": room <= 3.0,
            })

    try:
        os.makedirs(ORDERS_DIR, exist_ok=True)
        with open(f"{ORDERS_DIR}/kis_stop_monitor.json", "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "items": sorted(monitor, key=lambda x: x["room_pp"])}, f, ensure_ascii=False)
    except Exception:
        pass
    reached = sum(1 for m in monitor if m["room_pp"] <= 0)
    print(f"[stop-monitor] 보유 {len(monitor)}건 점검 / 손절선 도달 {reached}건(청산은 EOD cmd_sell)")


def _wait_balance_settle(max_wait=20, interval=4):
    """매수/매도 직후 KIS 모의 잔고조회 API 가 체결을 즉시 반영하지 못해
    스냅샷이 pre-buy 로 박제되는 문제 방지. 우리 기록(kis_positions.csv)의
    보유종목이 API 잔고에 모두 잡힐 때까지 폴링(최대 max_wait 초).
    타임아웃이어도 그냥 진행(다음 status 가 보정). 아무 보유 기록 없으면 즉시 반환.
    """
    try:
        expected = set(load_kis_positions().keys())
    except Exception:
        expected = set()
    if not expected:
        return
    try:
        client = KISMockClient()
    except Exception:
        return
    waited = 0
    while waited < max_wait:
        try:
            _, positions = client.get_balance()
            if set(positions.keys()) >= expected:
                print(f"[daily] 잔고 체결 반영 확인({waited}s) — 보유 {len(positions)}건")
                return
        except Exception:
            pass
        time.sleep(interval)
        waited += interval
    print(f"[daily] 잔고 반영 대기 타임아웃({max_wait}s) — 다음 status 가 보정")


# ── 진입점 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        cmd_status()
        sys.exit(0)

    with kis_lock(timeout=60):
        if cmd == "buy":
            cmd_buy()
        elif cmd == "sell":
            cmd_sell()
        elif cmd == "stopcheck":
            cmd_stop_check()
        elif cmd == "reconcile":
            cmd_reconcile()
        elif cmd == "daily":
            # stop-loss는 전일 종가 기준 → 오전/오후 구분 없이 항상 sell 먼저 실행
            # (만기 청산은 오후에만 발동. 오전 실행 시 stop 발동 건만 매도됨)
            print("[daily] 매도(만기/stop) 후 매수")
            cmd_sell()
            cmd_buy()
            # 매수 직후 잔고API 체결반영 지연 대비 — 우리 기록이 잔고에 잡힐 때까지
            # 잠깐 대기 후 스냅샷 기록(대시보드가 당일 보유를 바로 반영).
            _wait_balance_settle()
            cmd_status()
        else:
            print(f"[main] 알 수 없는 명령: {cmd}")
            print("사용법: python kis_trader.py [status|buy|sell|stopcheck|reconcile|daily]")
            sys.exit(1)

    # 체결 묶음 알림 — 이번 실행에서 쌓인 매수/매도를 1통으로 (없으면 전송 안 함)
    try:
        import notifier
        notifier.flush_fills("[KIS 안D]")
    except Exception:
        pass
