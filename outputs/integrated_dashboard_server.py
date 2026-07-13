"""
integrated_dashboard_server.py -- v3
6 tabs: overview / cheonok / mock-account / backtest+compare / ai-model / data+logs

Run : python integrated_dashboard_server.py [port]
Open: http://localhost:5050
"""

import sys, os, re, sqlite3, json
from pathlib import Path
from datetime import datetime, timedelta

try:
    from flask import Flask, jsonify, request
except ImportError:
    print("pip install flask"); sys.exit(1)
try:
    import pandas as pd
    import numpy as np
except ImportError:
    print("pip install pandas numpy"); sys.exit(1)

app = Flask(__name__)

# ── numpy / pandas 타입 JSON 직렬화 지원 (Flask 3.x) ──────────────────────────
try:
    from flask.json.provider import DefaultJSONProvider
    class _NpProvider(DefaultJSONProvider):
        def default(self, o):
            if isinstance(o, np.integer):  return int(o)
            if isinstance(o, np.floating): return float(o)
            if isinstance(o, np.ndarray):  return o.tolist()
            if isinstance(o, np.bool_):    return bool(o)
            return super().default(o)
    app.json_provider_class = _NpProvider
    app.json = _NpProvider(app)
except ImportError:
    # Flask < 2.2 fallback
    import json as _json
    class _NpEncoder(_json.JSONEncoder):
        def default(self, o):
            if isinstance(o, np.integer):  return int(o)
            if isinstance(o, np.floating): return float(o)
            if isinstance(o, np.ndarray):  return o.tolist()
            return super().default(o)
    app.json_encoder = _NpEncoder  # type: ignore

# ── paths ──────────────────────────────────────────────────────────────────────
PORT        = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
BASE        = Path(__file__).parent
AI_DIR      = BASE.parent / "Stock_AI_Project"
DB_PATH     = AI_DIR / "data" / "stock.db"
LOG_DIR     = BASE / "logs"           # 실제 로그 위치: C:\fin\outputs\logs\
RESULTS_DIR = BASE / "results"
KIWOOM_DIR  = BASE / "db" / "kiwoom"
KIS_DIR     = BASE / "db" / "kis"
MACRO_DAILY = BASE / "macro_data" / "daily"

PAPER_CSV       = BASE / "paper_signals.csv"
KIS_SIGNALS_CSV = BASE / "kis_paper_signals.csv"
STRENGTH_LOG_CSV = BASE / "db" / "signal_strength_log.csv"
INTRADAY_CACHE  = BASE / "db" / "kiwoom" / "intraday_cache.json"
HISTORY_CSV = BASE / "trades_history_v3.csv"
MODEL_JSON  = BASE / "ai_data" / "meta_model_v4.json"
FEAT_CSV    = BASE / "ai_data" / "meta_model_v4.features.csv"

import glob as _glob

def _latest_log(pattern: str) -> Path:
    """logs/ 디렉토리에서 날짜별 로그 중 가장 최신 파일 반환."""
    files = sorted(_glob.glob(str(LOG_DIR / pattern)))
    return Path(files[-1]) if files else LOG_DIR / f"_missing_{pattern.replace('*','X')}"
PER_SLOT    = 3_000_000

_META_COLS = {"date","code","strategy","entry_date","exit_date","net_pct","gross_pct"}


# ── helpers ────────────────────────────────────────────────────────────────────
def safe_db(query, params=()):
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(DB_PATH.as_uri()+"?mode=ro", uri=True, timeout=3)
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def tail_log(path, n=60):
    """로그 파일 마지막 n줄을 문자열로 반환 (JS textContent 호환)."""
    if not Path(path).exists():
        return ""
    try:
        raw = Path(path).read_bytes()[-20000:]
        # Windows 환경에서 CP949(EUC-KR) 로그 파일 대응 — UTF-8 먼저 시도
        for enc in ("utf-8", "cp949"):
            try:
                return "\n".join(raw.decode(enc).strip().splitlines()[-n:])
            except UnicodeDecodeError:
                continue
        return "\n".join(raw.decode("utf-8", errors="replace").strip().splitlines()[-n:])
    except Exception:
        return ""


def calc_mdd(values):
    """Max drawdown (%) from equity value list. Returns negative float."""
    if len(values) < 2:
        return 0.0
    peak = values[0]; mdd = 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)
    return round(mdd, 2)


def calc_sharpe(net_pcts):
    """Trade-level Sharpe (mean/std). Returns 0 if insufficient data."""
    if len(net_pcts) < 5:
        return 0.0
    arr = np.array(net_pcts, dtype=float)
    std = arr.std()
    return round(float(arr.mean() / std), 3) if std > 0 else 0.0


def _norm_sent(v):
    if v is None: return "neutral"
    try:
        s = float(v)
        return "positive" if s > 0.2 else ("negative" if s < -0.2 else "neutral")
    except (ValueError, TypeError):
        s = str(v).lower()
        return "positive" if "pos" in s else ("negative" if "neg" in s else "neutral")


def _latest_price(code):
    """Look up today's close from macro_data/daily CSV."""
    today = datetime.today()
    for delta in range(5):
        d = (today - timedelta(days=delta)).strftime("%Y%m%d")
        p = MACRO_DAILY / f"{d}.csv"
        if not p.exists():
            continue
        try:
            df = pd.read_csv(p, dtype={"code": str})
            df["code"] = df["code"].str.zfill(6)
            row = df[df["code"] == str(code).zfill(6)]
            if len(row):
                return float(row["close"].iloc[0]), d
        except Exception:
            pass
    return None, None


# ── 1. data freshness ──────────────────────────────────────────────────────────
def get_freshness():
    issues = []
    # (테이블, 쿼리, 최대허용일) — 주기에 맞춘 임계. 일일수집=5~6, 뉴스=7, 신용잔고=주1회라 10.
    checks = [
        ("korea_stocks",     "SELECT MAX(date) FROM korea_stocks", 5),
        ("supply_demand",    "SELECT MAX(date) FROM supply_demand", 5),
        ("usa_stocks",       "SELECT MAX(date) FROM usa_stocks", 6),
        ("macro_indicators", "SELECT MAX(date) FROM macro_indicators", 6),
        ("credit_balance",   "SELECT MAX(date) FROM credit_balance", 10),   # 주1회(토) kiwoom_weekly — 한 주 누락 시만 경고
        ("news",             "SELECT MAX(pubDate) FROM news", 6),
    ]
    for name, q, max_days in checks:
        rows = safe_db(q)
        if rows and rows[0][0]:
            latest = str(rows[0][0])[:10]
            try:
                fmt = "%Y-%m-%d" if "-" in latest else "%Y%m%d"
                dt = datetime.strptime(latest, fmt)
                age = (datetime.now() - dt).days
                if age > max_days:
                    issues.append({"table": name, "latest": latest, "age_days": age})
            except Exception:
                pass
        else:
            issues.append({"table": name, "latest": "없음", "age_days": 9999})
    return issues


def get_health():
    """조용한 실패 종합 점검 — 상단 배너용. 테이블 정체(freshness) + 신호 정체를 한데 모은다.
    오늘 세션에서 고친 버그들(2개월 0신호, 테이블 정체)이 전부 '에러 없이 조용히' 굴러갔기에,
    이상 징후를 대시보드 맨 위에서 바로 보이게 한다."""
    issues = []
    for f in get_freshness():
        issues.append({"sev": "warn",
                       "msg": f"{f['table']} {f['age_days']}일 정체(최신 {f['latest']})"})
    # 신호 정체 — paper_signals 최신 signal_date 가 오래됐으면 신호생성/데이터 이상 신호.
    try:
        if PAPER_CSV.exists():
            sd = pd.read_csv(PAPER_CSV, dtype={"code": str})
            if "signal_date" in sd.columns and len(sd):
                last = str(sd["signal_date"].astype(str).max())[:8]
                age = (datetime.now() - datetime.strptime(last, "%Y%m%d")).days
                if age >= 7:
                    issues.append({"sev": "err",
                                   "msg": f"신호 {age}일째 없음(최신 {last}) — live_signal/로더 점검"})
    except Exception:
        pass
    return issues


# ── 슬롯 구성 (kiwoom_trader.py 와 동기화) ─────────────────────────────────────
_STRATEGY_MAX_SLOTS = {"high_52w_filt": 4, "rsi_reversal": 4, "rsi_vol": 2}
_STRATEGY_PRIORITY  = ["high_52w_filt", "rsi_reversal", "rsi_vol"]


def _ledger_slot_counts(ledger_csv, slot_keys):
    """진입가 원장(실보유) 기준 전략별 사용 슬롯 수(2026-07-12).

    [수정 배경] 종전엔 신호 CSV 의 '만기 미도래 신호 수'를 슬롯 사용으로 셌는데,
    신호 CSV 는 매수 여부와 무관한 모든 신호의 누적이라 rsi_reversal 211/4 같은
    무의미한 카운트가 됐음(실측). 원장은 매수 시 기록·매도 시 삭제·prune 정리라
    실보유와 일치한다."""
    used = {k: 0 for k in slot_keys}
    try:
        p = Path(ledger_csv)
        if p.exists():
            df = pd.read_csv(p, dtype=str).fillna("")
            for s in df.get("strategy", pd.Series(dtype=str)):
                if s in used:
                    used[s] += 1
    except Exception:
        pass
    return used


def _slot_status(paper_df=None):
    """전략별 슬롯 사용 현황 — 키움 진입가 원장(실보유) 기준(2026-07-12 전환).

    paper_df 인자는 하위호환용으로 무시(과거 신호CSV 기준 카운트는 오표시였음)."""
    used = _ledger_slot_counts(KIWOOM_DIR / "kiwoom_positions.csv", _STRATEGY_MAX_SLOTS)
    slots = []
    for strat in _STRATEGY_PRIORITY:
        mx = _STRATEGY_MAX_SLOTS[strat]
        cu = used.get(strat, 0)
        slots.append({
            "strategy": strat,
            "max": mx,
            "used": cu,
            "free": max(0, mx - cu),
        })
    return slots


# ── 2. cheonok (trading system) ────────────────────────────────────────────────
def get_cheonok():
    out = {
        "active_count": 0, "total_signals": 0, "signals": [],
        "equity": {"labels": [], "values": []},
        "win_rate": 0, "avg_return": 0, "trade_count": 0,
        "final_capital": PER_SLOT, "mdd": 0, "sharpe": 0,
        "strategy_stats": [],
        "slot_status": [],      # 전략별 슬롯 현황 (v2 추가)
        "strategy_perf": [],    # 전략별 실제 매매 손익 (paper_signals 기준)
    }
    if PAPER_CSV.exists():
        try:
            df = pd.read_csv(PAPER_CSV, dtype={"code": str})
            df["code"] = df["code"].str.zfill(6)
            today_int = int(datetime.today().strftime("%Y%m%d"))  # 20260619 형식 — int 비교
            active = df[df["target_exit_date"] >= today_int] if "target_exit_date" in df.columns else df
            out["active_count"] = int(len(active))
            out["total_signals"] = int(len(df))

            # 전략별 슬롯 현황
            out["slot_status"] = _slot_status(df)

            # Active signals with estimated P&L
            sigs = []
            for _, r in active.head(30).iterrows():
                s = {c: (str(r[c]) if not isinstance(r[c], float) or not np.isnan(r[c]) else "") for c in active.columns}
                ep = float(r.get("entry_price_close", 0) or 0)
                if ep > 0:
                    curr, curr_date = _latest_price(str(r["code"]))
                    if curr:
                        s["curr_price"] = curr
                        s["curr_date"] = curr_date
                        s["est_pct"] = round((curr / ep - 1) * 100 - 0.33, 2)
                    else:
                        s["curr_price"] = ""; s["curr_date"] = ""; s["est_pct"] = ""
                else:
                    s["curr_price"] = ""; s["curr_date"] = ""; s["est_pct"] = ""
                sigs.append(s)
            out["signals"] = sigs
        except Exception as e:
            out["signals_error"] = str(e)[:80]

    if HISTORY_CSV.exists():
        try:
            hist = pd.read_csv(HISTORY_CSV)
            if "net_pct" in hist.columns:
                done = hist.dropna(subset=["net_pct"])
                date_col = next((c for c in ["exit_date","date"] if c in hist.columns), hist.columns[0])
                done = done.sort_values(date_col)
                cap = PER_SLOT; eq_labels = []; eq_vals = [PER_SLOT]
                for _, r in done.iterrows():
                    cap += PER_SLOT * float(r["net_pct"]) / 100
                    eq_labels.append(str(r[date_col])[:8])
                    eq_vals.append(round(cap))
                out["equity"] = {"labels": eq_labels[-120:], "values": eq_vals[-120:]}
                out["win_rate"]      = round(float((done["net_pct"] > 0).mean()) * 100, 1)
                out["avg_return"]    = round(float(done["net_pct"].mean()), 2)
                out["trade_count"]   = int(len(done))
                out["final_capital"] = int(eq_vals[-1]) if eq_vals else PER_SLOT
                out["mdd"]           = calc_mdd(eq_vals)
                out["sharpe"]        = calc_sharpe(done["net_pct"].tolist())
                if "strategy" in done.columns:
                    strat = (done.groupby("strategy")["net_pct"]
                             .agg(count="count", win=lambda x: float((x>0).mean()), avg="mean")
                             .reset_index().sort_values("count", ascending=False))
                    out["strategy_stats"] = [
                        {"strategy": str(r["strategy"]), "count": int(r["count"]),
                         "win": round(float(r["win"]), 3), "avg": round(float(r["avg"]), 3)}
                        for _, r in strat.head(10).iterrows()
                    ]
        except Exception as e:
            out["equity_error"] = str(e)[:80]
    return out


# ── 3. backtest trades (results/trades_AB_*.csv) ───────────────────────────────
def get_backtest_trades(date_from="", date_to=""):
    out = {"source": "", "total": 0, "trades": [], "stats": {}}
    if not RESULTS_DIR.exists():
        out["error"] = "results/ not found"; return out
    # exit_compare 우선 (profit_target_test 결과), 없으면 기존 trades_* 파일
    files = sorted(RESULTS_DIR.glob("exit_compare_*.csv"))
    if not files:
        files = sorted(RESULTS_DIR.glob("trades_AB_*.csv"))
    if not files:
        files = sorted(RESULTS_DIR.glob("trades_*.csv"))
    if not files:
        out["error"] = "No exit_compare_*.csv or trades_*.csv in results/"; return out
    latest = files[-1]
    out["source"] = latest.name
    try:
        df = pd.read_csv(latest, dtype={"stock_code": str})
        if "entry_time" in df.columns:
            df["entry_date"] = df["entry_time"].astype(str).str[:10]
        elif "entry_date" not in df.columns:
            df["entry_date"] = ""
        if date_from:
            df = df[df["entry_date"] >= date_from]
        if date_to:
            df = df[df["entry_date"] <= date_to]
        out["total"] = int(len(df))
        if "net_pct" in df.columns:
            done = df.dropna(subset=["net_pct"])
            cap = PER_SLOT; eq_vals = [PER_SLOT]
            for _, r in done.sort_values("entry_date").iterrows():
                cap += PER_SLOT * float(r["net_pct"]) / 100
                eq_vals.append(round(cap))
            out["stats"] = {
                "total": int(len(done)),
                "win_rate": round(float((done["net_pct"] > 0).mean()) * 100, 1),
                "avg_return": round(float(done["net_pct"].mean()), 2),
                "mdd": calc_mdd(eq_vals),
                "sharpe": calc_sharpe(done["net_pct"].tolist()),
            }
        # return up to 500 trades
        recs = []
        for _, r in df.head(500).iterrows():
            rec = {}
            for c in df.columns:
                v = r[c]
                rec[c] = "" if (isinstance(v, float) and np.isnan(v)) else (round(float(v), 4) if isinstance(v, float) else str(v) if not isinstance(v, (int, str)) else v)
            recs.append(rec)
        out["trades"] = recs
    except Exception as e:
        out["error"] = str(e)[:120]
    return out




# ── 3b. strategy_engine compare table (strategy_compare_*.csv) ─────────────────
def get_strategy_compare():
    """results/strategy_compare_*.csv 중 가장 최신 파일을 로드."""
    out = {"source": "", "rows": [], "updated": ""}
    if not RESULTS_DIR.exists():
        return out
    # strategy_lab 사례처럼 대용량 형제 파일(_trades_) 오선택을 예방.
    files = sorted(f for f in RESULTS_DIR.glob("strategy_compare_*.csv")
                   if "_trades_" not in f.name)
    if not files:
        return out
    latest = files[-1]
    out["source"] = latest.name
    out["updated"] = latest.name.replace("strategy_compare_", "").replace(".csv", "")
    try:
        df = pd.read_csv(latest)
        df = df.where(pd.notnull(df), None)
        out["rows"] = df.to_dict("records")
    except Exception as e:
        out["error"] = str(e)[:80]
    return out


def get_strategy_lab():
    """전략 랩 — strategy_lab.py 가 만든 forward(OOS) 전략별 성과. results/strategy_lab_*.csv 최신본."""
    out = {"rows": [], "updated": ""}
    # ★ glob "strategy_lab_*.csv" 는 요약본(strategy_lab_YYYYMMDD.csv, 48행)뿐 아니라
    #   거래내역(strategy_lab_trades_YYYYMMDD.csv, 25만행/12MB)까지 잡고,
    #   정렬상 trades 파일이 뒤로 가 files[-1] 로 선택돼 25만행이 통째로 브라우저에 전송→
    #   DOM 폭발(렌더러 수 GB) 버그가 있었다. trades 파일을 제외한다.
    files = sorted(f for f in RESULTS_DIR.glob("strategy_lab_*.csv")
                   if "_trades_" not in f.name)
    if not files:
        return out
    latest = files[-1]
    out["updated"] = latest.name.replace("strategy_lab_", "").replace(".csv", "")
    try:
        df = pd.read_csv(latest)
        df = df.where(pd.notnull(df), None)
        out["rows"] = df.to_dict("records")
    except Exception as e:
        out["error"] = str(e)[:80]
    return out

# ── 4. kiwoom mock account ─────────────────────────────────────────────────────
_DAILY_CLOSE_CACHE = {}


def _px_map_loader(date_str, cols, cache):
    """macro_data/daily/{date}.csv → {code: 가격}. 성공 파싱만 캐시(2026-07-12).

    [수정] 기존엔 '파일 없음'도 빈 dict 로 영구 캐시 — 상시 구동 서버가 15:21~15:50
    (당일 CSV 생성 전) 사이 한 번이라도 조회하면, 파일이 생긴 뒤에도 그날 매도의
    실현손익 종가보정이 재시작 전까지 영영 안 되던 버그."""
    date_str = str(date_str)
    if not date_str:
        return {}
    if date_str in cache:
        return cache[date_str]
    p = BASE / "macro_data" / "daily" / f"{date_str}.csv"
    if not p.exists():
        return {}   # 아직 미생성 — 캐시하지 않음(생기면 다음 호출에 반영)
    try:
        d = pd.read_csv(p, dtype={"code": str}, encoding="utf-8-sig")
        d["code"] = d["code"].astype(str).str.zfill(6)
        col = next((c for c in cols if c in d.columns), None)
        if col is None:
            return {}
        m = {k: v for k, v in zip(d["code"], pd.to_numeric(d[col], errors="coerce"))}
        cache[date_str] = m   # 성공 파싱만 캐시
        return m
    except Exception:
        return {}   # 부분 쓰기 중 파싱 실패 가능 — 캐시하지 않음


_DAILY_OPEN_CACHE = {}


def _daily_close_map(date_str):
    return _px_map_loader(date_str, ("close", "종가"), _DAILY_CLOSE_CACHE)


def _daily_open_map(date_str):
    return _px_map_loader(date_str, ("open", "시가"), _DAILY_OPEN_CACHE)


def _attach_realized_pnl(combined, sell_px_mode="close"):
    """매도 행에 실현손익(pnl 금액·pnl_pct %)을 FIFO 매칭으로 계산해 붙인다.

    주문로그엔 매입가가 없어 같은 code 의 직전 매수 가격과 시간순 FIFO 로 매칭한다.
    sell_px_mode:
      "close"(키움): 매도 price 가 0 이면 매도일 종가로 보정(15:21 마감 동시호가 ≈ 종가).
      "open"(KIS, 2026-07-12): KIS 매도행의 price 는 '전일 종가'(참조가)라 그대로 쓰면
        전일종가→당일시가 갭이 통째로 빠짐(특히 stop 매도는 갭하락 지속이 많아 손실이
        체계적으로 과소 표시) — 실체결(09:01 시장가)에 가까운 '매도일 시가'를 우선 사용.
    실패(ok=False) 행은 체결 안 됐으므로 제외. 매수행·미매칭 매도는 pnl 공란.
    (대시보드 표시 전용 — 매매 로직과 무관)
    """
    try:
        df = combined.copy()
        df["pnl"] = ""
        df["pnl_pct"] = ""
        s = df.assign(_d=df["date"].astype(str), _t=df.get("time", "").astype(str)) \
              .sort_values(["_d", "_t"])
        lots = {}        # code -> [[price, qty_remaining], ...]
        upd = {}         # original_index -> (pnl_amt, pnl_pct, eff_sell_price)
        for idx, r in s.iterrows():
            ok = str(r.get("ok", "")).strip().lower() not in ("false", "0", "")
            side = str(r.get("side", "")).strip().lower()
            code = str(r.get("code", "")).zfill(6)
            try:
                qty = int(float(r.get("qty", 0) or 0))
                price = float(r.get("price", 0) or 0)
            except Exception:
                continue
            if not ok or qty <= 0:
                continue
            if side == "buy":
                if price > 0:
                    lots.setdefault(code, []).append([price, qty])
            elif side == "sell":
                if sell_px_mode == "open":
                    d8 = r.get("date", "")
                    _o = float(_daily_open_map(d8).get(code, 0) or 0)
                    eff = (_o if _o == _o and _o > 0 else 0) \
                        or float(_daily_close_map(d8).get(code, 0) or 0) or price
                else:
                    eff = price if price > 0 else float(_daily_close_map(r.get("date", "")).get(code, 0) or 0)
                rem, cost, matched = qty, 0.0, 0
                q = lots.get(code, [])
                while rem > 0 and q:
                    lot = q[0]
                    take = min(rem, lot[1])
                    cost += take * lot[0]
                    matched += take
                    lot[1] -= take
                    rem -= take
                    if lot[1] <= 0:
                        q.pop(0)
                if matched > 0 and eff > 0:
                    avg = cost / matched
                    upd[idx] = (int(round((eff - avg) * matched)),
                                round((eff / avg - 1) * 100, 2) if avg > 0 else 0,
                                int(round(eff)))
        for idx, (a, p, ep) in upd.items():
            df.at[idx, "pnl"] = a
            df.at[idx, "pnl_pct"] = p
            df.at[idx, "price"] = ep   # 0 으로 기록됐던 매도 표시가도 실제 종가로 보정
        return df
    except Exception:
        return combined


def _signal_strength_index(account_prefix):
    """signal_strength_log.csv → {(code, strategy): [(signal_date, score_ic)] 정렬}.

    account_prefix: 'kiwoom' | 'kis' (강도로그 account 열이 'kiwoom_안C'/'KIS_안D')."""
    idx = {}
    path = BASE / "db" / "signal_strength_log.csv"
    if not path.exists():
        return idx
    try:
        df = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
        if "account" in df.columns:
            df = df[df["account"].astype(str).str.lower().str.startswith(account_prefix)]
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["signal_date"] = df["signal_date"].astype(str).str.replace("-", "", regex=False)
        df["score_ic"] = pd.to_numeric(df["score_ic"], errors="coerce")
        for _, r in df.iterrows():
            if pd.isna(r["score_ic"]):
                continue
            idx.setdefault((r["code"], str(r.get("strategy", ""))), []) \
               .append((r["signal_date"], round(float(r["score_ic"]), 2)))
        for k in idx:
            idx[k].sort()
    except Exception:
        pass
    return idx


def _pick_strength(idx, code, strategy, ref_date):
    """(code, strategy)의 신호 중 ref_date(진입/주문일) 이전 최신 강도. 없으면 코드 전체 폴백."""
    code = str(code or "").zfill(6)
    lst = idx.get((code, str(strategy or "")))
    if not lst:   # 전략 불일치 시 같은 코드의 아무 전략이나(재신호 등)
        cand = [v for (c, _s), vs in idx.items() if c == code for v in vs]
        lst = sorted(cand) if cand else None
    if not lst:
        return None
    ref = str(ref_date or "").replace("-", "")[:8]
    picked = None
    for sd, sc in lst:
        if not ref or sd <= ref:
            picked = sc
        else:
            break
    return picked if picked is not None else lst[-1][1]


def _attach_strength_mock(out, account_prefix):
    """보유 포지션·매수 주문행에 score_ic(신호강도) 부착(2026-07-13)."""
    idx = _signal_strength_index(account_prefix)
    if not idx:
        return
    for p in out.get("positions", []):
        p["score_ic"] = _pick_strength(idx, p.get("code"), p.get("strategy"), p.get("entry_date"))
    for o in out.get("orders", []):
        if str(o.get("side", "")).lower() == "buy":
            o["score_ic"] = _pick_strength(idx, o.get("code"), o.get("strategy"), o.get("date"))


def _strategy_perf(combined, positions):
    """전략별 성과(2026-07-13) — 실현(청산 매도행 pnl) + 미실현(현 보유 pnl) 분리 집계.

    combined: _attach_realized_pnl 적용된 주문 DataFrame(strategy·side·pnl·pnl_pct 보유)
    positions: 보유 포지션 리스트(strategy·pnl·pnl_pct 병합됨)
    반환: [{strategy, closed_n, win_pct, avg_pct, realized_pnl, open_n, unreal_pnl}] pnl 내림차순.
    ※ 실현손익은 FIFO 추정치(브로커 정산 정확값 아님) — 표시용.
    """
    perf = {}

    def _row(s):
        return perf.setdefault(str(s or "기타"), {
            "strategy": str(s or "기타"), "closed_n": 0, "wins": 0,
            "sum_pct": 0.0, "realized_pnl": 0, "open_n": 0, "unreal_pnl": 0})

    try:
        if combined is not None and len(combined):
            sells = combined[combined["side"].astype(str).str.lower().isin(("sell", "tp_sell"))]
            for _, r in sells.iterrows():
                pnl = r.get("pnl", "")
                if pnl == "" or pnl is None:      # 미체결/미매칭 매도는 실현손익 없음
                    continue
                try:
                    pnl = int(float(pnl)); pct = float(r.get("pnl_pct", 0) or 0)
                except (TypeError, ValueError):
                    continue
                d = _row(r.get("strategy", ""))
                d["closed_n"] += 1
                d["wins"] += 1 if pnl > 0 else 0
                d["sum_pct"] += pct
                d["realized_pnl"] += pnl
    except Exception:
        pass

    try:
        for p in (positions or []):
            d = _row(p.get("strategy", ""))
            d["open_n"] += 1
            try:
                d["unreal_pnl"] += int(float(p.get("pnl", 0) or 0))
            except (TypeError, ValueError):
                pass
    except Exception:
        pass

    out = []
    for d in perf.values():
        n = d["closed_n"]
        out.append({
            "strategy": d["strategy"],
            "closed_n": n,
            "win_pct": round(d["wins"] / n * 100, 1) if n else None,
            "avg_pct": round(d["sum_pct"] / n, 2) if n else None,
            "realized_pnl": d["realized_pnl"],
            "open_n": d["open_n"],
            "unreal_pnl": d["unreal_pnl"],
        })
    out.sort(key=lambda r: -(r["realized_pnl"] + r["unreal_pnl"]))
    return out


def get_kiwoom_mock(date_from="", date_to=""):
    out = {"snapshot": {}, "equity": [], "orders": [], "order_total": 0, "positions": []}
    snap_path = KIWOOM_DIR / "snapshot.json"
    if snap_path.exists():
        try:
            out["snapshot"] = json.loads(snap_path.read_text(encoding="utf-8"))
            # 보유 포지션 표용 — 스냅샷 positions 를 그대로 노출(fillMock 이 mock.positions 로 읽음).
            # 평가금액(evlt_amt)=수량×현재가, 손익(pnl, 원)도 함께 노출.
            out["positions"] = [
                {"code": p.get("code", ""), "name": p.get("name", ""),
                 "qty": p.get("qty", 0), "avg_price": p.get("avg_price", 0),
                 "cur_price": p.get("price", 0),
                 "evlt_amt": p.get("evlt_amt", (p.get("qty", 0) or 0) * (p.get("price", 0) or 0)),
                 "pnl": p.get("pnl", 0), "pnl_pct": p.get("pnl_pct", 0)}
                for p in out["snapshot"].get("positions", [])
            ]
        except Exception:
            pass
    eq_path = KIWOOM_DIR / "equity_history.csv"
    if eq_path.exists():
        try:
            out["equity"] = pd.read_csv(eq_path).tail(120).fillna("").to_dict("records")
        except Exception:
            pass
    if KIWOOM_DIR.exists():
        frames = []
        for f in sorted(KIWOOM_DIR.glob("orders_*.csv")):
            date_str = f.stem.replace("orders_", "")
            if date_from and date_str < date_from.replace("-",""):
                continue
            if date_to and date_str > date_to.replace("-",""):
                continue
            try:
                df = pd.read_csv(f, dtype={"code": str}, encoding="utf-8-sig")
                df["date"] = date_str
                frames.append(df)
            except Exception:
                pass
        if frames:
            combined = pd.concat(frames, ignore_index=True).fillna("")
            combined = _attach_realized_pnl(combined)   # 매도행 실현손익(금액·%) 부여
            out["order_total"] = int(len(combined))
            out["orders"] = combined.tail(500).to_dict("records")
            # 보유 포지션에 전략·진입일 병합 — 스냅샷(kt00018)엔 없음 → 주문로그의
            # 해당 종목 '마지막 성공 매수'(ok=True)에서 가져온다(2026-07-02, 빈칸 수정).
            try:
                b = combined[combined["side"].astype(str).str.lower() == "buy"]
                b = b[b["ok"].astype(str).str.lower().isin(("true", "1"))]
                buy_map = {}
                for _, r in b.iterrows():   # 시간순 순회 → 뒤(최신)가 덮어씀
                    buy_map[str(r.get("code", "")).zfill(6)] = (
                        str(r.get("strategy", "") or ""), str(r.get("date", "") or ""))
                for p in out["positions"]:
                    st, dt = buy_map.get(str(p.get("code", "")).zfill(6), ("", ""))
                    if not p.get("strategy"):
                        p["strategy"] = st
                    if not p.get("entry_date"):
                        p["entry_date"] = dt
            except Exception:
                pass
            out["strategy_perf"] = _strategy_perf(combined, out["positions"])   # 전략별 실현+미실현
    _attach_strength_mock(out, "kiwoom")   # 보유·매수행에 신호강도(score_ic) 부착
    out["slot_status"] = _kiwoom_slot_status()   # 키움 모의투자 슬롯(진입/청산 조건 포함)
    return out


# ── 4b. KIS mock account (안D 포트폴리오) ─────────────────────────────────────
# ── 모의투자 슬롯 정의(전략별 슬롯수 + 진입/청산 조건) — 슬롯 클릭 드롭다운용 ──
# KIS 안D (손절 있음)
_KIS_STRATEGY_SLOTS = {"h52w_for3d_mkt": 4, "for_high20_mkt": 4, "gc_for3d": 2}
_KIS_STRATEGY_STOP  = {"h52w_for3d_mkt": -15, "for_high20_mkt": -10, "gc_for3d": -26}
_KIS_STRATEGY_LABEL = {
    "h52w_for3d_mkt": "52주신고가+외국인",
    "for_high20_mkt": "20일신고가+외국인",
    "gc_for3d":       "골든크로스+외국인",
}
_KIS_STRATEGY_HOLD = {"h52w_for3d_mkt": 20, "for_high20_mkt": 20, "gc_for3d": 15}
_KIS_ENTRY = {
    "h52w_for3d_mkt": "52주 신고가 돌파 + 외국인 3일 연속 순매수 + 시장강세 게이트(전종목 평균등락 60일MA>0) + 거래대금 ≥ 30억",
    "for_high20_mkt": "20일 신고가(종가 ≥ 직전20일 최고) + 외국인 3일 연속 순매수 + 시장강세 게이트 + 거래대금 ≥ 30억",
    "gc_for3d":       "골든크로스(MA20이 MA60 상향돌파) + 외국인 3일 연속 순매수 + 거래대금 ≥ 30억 (시장게이트 없음)",
}
_KIS_EXIT = {
    "h52w_for3d_mkt": "진입 20영업일째 종가 또는 -15% 손절(전일 종가 기준)",
    "for_high20_mkt": "진입 20영업일째 종가 또는 -10% 손절",
    "gc_for3d":       "진입 15영업일째 종가 또는 -26% 손절",
}
# 키움 안C (손절 없음 = 시간청산)
_KW_STRATEGY_SLOTS = {"high_52w_filt": 4, "rsi_reversal": 4, "rsi_vol": 2}
_KW_STRATEGY_LABEL = {
    "high_52w_filt": "52주 신고가+거래량",
    "rsi_reversal":  "RSI 과매도 반전",
    "rsi_vol":       "RSI 과매도+거래량급증",
}
_KW_STRATEGY_HOLD = {"high_52w_filt": 20, "rsi_reversal": 5, "rsi_vol": 7}
_KW_ENTRY = {
    "high_52w_filt": "종가 > 직전 252영업일 신고가(52주 돌파) + 시장강세 게이트(전종목 평균등락 60일MA>0) + 당일 거래대금 ≥ 30일평균 ×1.5 + 거래대금 ≥ 30억 + 강도(score_ic) ≥ 6",
    "rsi_reversal":  "RSI(14) < 30 (과매도) + 거래대금 ≥ 10억 + 강도(score_ic) ≥ 6",
    "rsi_vol":       "RSI(14) < 30 + 당일 거래대금 > 20일평균 ×2.0(거래량 급증) + 거래대금 ≥ 10억 + 강도(score_ic) ≥ 6",
}
_KW_EXIT = {
    "high_52w_filt": "익절 +50% 장중 지정가(매일 아침 발주) 또는 진입 20영업일째 종가 (손절 없음)",
    "rsi_reversal":  "진입 5영업일째 종가 (손절 없음)",
    "rsi_vol":       "익절 +20% 장중 지정가(매일 아침 발주) 또는 진입 7영업일째 종가 (손절 없음)",
}


def _slot_status_detail(ledger_csv, slots, labels, holds, entries, exits, stops):
    """전략별 슬롯 현황(used/max) + 진입·청산 조건을 묶어 반환(슬롯 클릭 드롭다운용).

    [2026-07-12 이중 수정]
    ① 함수명 변경: 종전 이름이 _slot_status(라인 214)를 섀도잉해 get_cheonok 의
      1인자 호출이 항상 TypeError→빈 signals 로 삼켜졌음(활성신호 표 전면 소실).
    ② used 를 신호CSV('만기 미도래 신호 수' — 매수 여부 무관 누적이라 211/4 같은
      무의미 카운트) → 진입가 원장(실보유) 기준으로 교체."""
    used = _ledger_slot_counts(ledger_csv, slots)
    return [{
        "strategy": strat, "label": labels.get(strat, strat),
        "used": used.get(strat, 0), "max": slots[strat],
        "holding": holds.get(strat, ""), "stop_pct": stops.get(strat, ""),
        "entry": entries.get(strat, ""), "exit": exits.get(strat, ""),
    } for strat in slots]


def _kis_slot_status():
    return _slot_status_detail(KIS_DIR / "kis_positions.csv",
                               _KIS_STRATEGY_SLOTS, _KIS_STRATEGY_LABEL,
                               _KIS_STRATEGY_HOLD, _KIS_ENTRY, _KIS_EXIT, _KIS_STRATEGY_STOP)


def _kiwoom_slot_status():
    return _slot_status_detail(KIWOOM_DIR / "kiwoom_positions.csv",
                               _KW_STRATEGY_SLOTS, _KW_STRATEGY_LABEL,
                               _KW_STRATEGY_HOLD, _KW_ENTRY, _KW_EXIT, {})

def get_kis_mock(date_from="", date_to=""):
    out = {
        "snapshot": {},
        "positions": [],
        "orders": [],
        "order_total": 0,
        "slot_status": [],
        "signals_total": 0,
    }
    # 스냅샷 — kis_trader.py 가 db/kiwoom/kis_snapshot.json 에 기록(orders 와 같은 폴더). db/kis 아님.
    snap_path = KIWOOM_DIR / "kis_snapshot.json"
    if snap_path.exists():
        try:
            out["snapshot"] = json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 장중 손절 모니터(kis_trader stopcheck 가 15분마다 기록 — 읽기 전용)
    mon_path = KIWOOM_DIR / "kis_stop_monitor.json"
    if mon_path.exists():
        try:
            out["stop_monitor"] = json.loads(mon_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 보유 포지션 — 키움과 동일하게 '스냅샷'을 단일 진실원천으로 사용(이름+금액 포함).
    # 전략/진입일은 kis_positions.csv(진입가 추적)에서 code 로 머지(보조정보).
    track = {}
    pos_path = KIS_DIR / "kis_positions.csv"
    if pos_path.exists():
        try:
            pos_df = pd.read_csv(pos_path, dtype={"code": str})
            pos_df["code"] = pos_df["code"].str.zfill(6)
            for _, r in pos_df.iterrows():
                track[str(r["code"]).zfill(6)] = {
                    "strategy": r.get("strategy", ""),
                    "entry_date": str(r.get("signal_date", "")),
                }
        except Exception:
            pass
    snap_pos = (out.get("snapshot") or {}).get("positions") or []
    out["positions"] = [
        {"code": str(p.get("code", "")).zfill(6), "name": p.get("name", ""),
         "qty": p.get("qty", 0), "avg_price": p.get("avg_price", 0),
         "cur_price": p.get("price", 0),
         "evlt_amt": p.get("evlt_amt", (p.get("qty", 0) or 0) * (p.get("price", 0) or 0)),
         "pnl": p.get("pnl", 0), "pnl_pct": p.get("pnl_pct", 0),
         "strategy": track.get(str(p.get("code", "")).zfill(6), {}).get("strategy", ""),
         "entry_date": track.get(str(p.get("code", "")).zfill(6), {}).get("entry_date", "")}
        for p in snap_pos
    ]
    # 주문 로그 (kis_orders_*.csv)
    if KIWOOM_DIR.exists():
        frames = []
        for f in sorted(KIWOOM_DIR.glob("kis_orders_*.csv")):
            date_str = f.stem.replace("kis_orders_", "")
            if date_from and date_str < date_from.replace("-", ""):
                continue
            if date_to and date_str > date_to.replace("-", ""):
                continue
            try:
                df = pd.read_csv(f, dtype={"code": str}, encoding="utf-8-sig")
                df["date"] = date_str
                frames.append(df)
            except Exception:
                pass
        if frames:
            combined = pd.concat(frames, ignore_index=True).fillna("")
            # KIS 매도행 price 는 '전일 종가' 참조가 — 매도일 시가 기준으로 갭 반영(2026-07-12)
            combined = _attach_realized_pnl(combined, sell_px_mode="open")
            out["order_total"] = int(len(combined))
            out["orders"] = combined.tail(500).to_dict("records")
            out["strategy_perf"] = _strategy_perf(combined, out.get("positions", []))   # 전략별 실현+미실현
    _attach_strength_mock(out, "kis")   # 보유·매수행에 신호강도(score_ic) 부착
    # 슬롯 현황 + 신호 수
    out["slot_status"] = _kis_slot_status()
    if KIS_SIGNALS_CSV.exists():
        try:
            out["signals_total"] = int(len(pd.read_csv(KIS_SIGNALS_CSV)))
        except Exception:
            pass
    return out


# ── 4c. 신호 강도 백데이터 (signal_strength_log.csv) ──────────────────────────
def get_ai_paper():
    """AI/강도 가상매매 실행 결과(ai_paper_trader.py 산출 JSON) — 표시 전용."""
    p = BASE / "db" / "ai_paper_result.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)[:200]}


def get_strength(limit=300):
    """
    strength_logger.py 가 누적한 신호 강도 원천값을 요약+최근기록으로 반환.
    실거래와 무관(슬롯 균등분배). '이 강도면 이렇게' 사후검증용 데이터.
    """
    out = {"rows": [], "total": 0, "summary": {}, "by_strategy": []}
    if not STRENGTH_LOG_CSV.exists():
        return out
    try:
        df = pd.read_csv(STRENGTH_LOG_CSV, dtype={"code": str}, encoding="utf-8-sig")
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    if df.empty:
        return out
    df["code"] = df["code"].astype(str).str.zfill(6)
    for c in ("score_ic", "score_tv", "slot_rank", "entry_close", "n_factors", "ai_prob"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    n = int(len(df))
    scored = int(df["score_ic"].notna().sum()) if "score_ic" in df.columns else 0
    out["total"] = n
    out["summary"] = {
        "total": n,
        "accounts": int(df["account"].nunique()) if "account" in df.columns else 0,
        "strategies": int(df["strategy"].nunique()) if "strategy" in df.columns else 0,
        "avg_score_ic": (round(float(df["score_ic"].mean()), 2)
                         if scored else None),
        "scored_pct": (int(round(100.0 * scored / n)) if n else 0),
        "last_date": (str(df["signal_date"].astype(str).max())
                      if "signal_date" in df.columns else ""),
    }
    # 전략별 집계
    if "strategy" in df.columns:
        try:
            g = df.groupby("strategy").agg(
                n=("code", "size"),
                avg_ic=("score_ic", "mean"),
                avg_tv=("score_tv", "mean"),
            ).reset_index().sort_values("n", ascending=False)
            out["by_strategy"] = [
                {"strategy": r["strategy"], "n": int(r["n"]),
                 "avg_ic": (round(float(r["avg_ic"]), 2) if pd.notna(r["avg_ic"]) else None),
                 "avg_tv": (round(float(r["avg_tv"]), 1) if pd.notna(r["avg_tv"]) else None)}
                for _, r in g.iterrows()
            ]
        except Exception:
            pass
    # ── 강도 가상매매 검증 — 실현수익(paper_audit_result.csv, done만)과 (signal_date,code)
    #    조인해 '이 강도 구간이면 실제 이렇게 됐다'를 집계. 실거래 아님(사후검증, 2026-07-02).
    try:
        pa_path = BASE / "paper_audit_result.csv"
        if pa_path.exists() and "score_ic" in df.columns:
            pa = pd.read_csv(pa_path, dtype={"code": str}, encoding="utf-8-sig")
            pa = pa[pa["status"].astype(str) == "done"]
            pa["code"] = pa["code"].astype(str).str.zfill(6)
            pa["signal_date"] = pa["signal_date"].astype(str).str.replace("-", "")
            pa["net_pct"] = pd.to_numeric(pa["net_pct"], errors="coerce")
            sl = df[df["score_ic"].notna()].copy()
            sl["signal_date"] = sl["signal_date"].astype(str).str.replace("-", "")
            j = sl.merge(pa[["signal_date", "code", "net_pct"]],
                         on=["signal_date", "code"], how="inner").dropna(subset=["net_pct"])
            if len(j):
                buckets = []
                for label, lo, hi in [("6 이상", 6.0, None), ("5~6", 5.0, 6.0), ("5 미만", None, 5.0)]:
                    m = j["score_ic"] >= lo if lo is not None else j["score_ic"].notna()
                    if hi is not None:
                        m &= j["score_ic"] < hi
                    g = j[m]
                    if len(g):
                        buckets.append({
                            "label": label, "n": int(len(g)),
                            "avg_net": round(float(g["net_pct"].mean()), 2),
                            "win": round(float((g["net_pct"] > 0).mean() * 100), 1),
                        })
                out["virtual"] = {"done_total": int(len(j)), "buckets": buckets}

            # AI 가상매매 검증 — ai_prob(모델 big-win 확률) 구간별 실현 성과.
            # ai_prob 는 차원버그 수정(6/29) 후 6/30부터 기록 → 첫 완료 표본은 ~7/7 이후 누적.
            if "ai_prob" in df.columns:
                sa = df[pd.to_numeric(df["ai_prob"], errors="coerce").notna()].copy()
                sa["ai_prob"] = pd.to_numeric(sa["ai_prob"], errors="coerce")
                sa["signal_date"] = sa["signal_date"].astype(str).str.replace("-", "")
                ja = sa.merge(pa[["signal_date", "code", "net_pct"]],
                              on=["signal_date", "code"], how="inner").dropna(subset=["net_pct"])
                out["virtual_ai"] = {"done_total": int(len(ja)),
                                     "logged_total": int(len(sa)), "buckets": []}
                if len(ja) >= 9:   # 3분위 최소 표본
                    try:
                        ja["q"] = pd.qcut(ja["ai_prob"], 3, labels=False, duplicates="drop")
                        bks = []
                        for qi, lab in [(2, "상위⅓"), (1, "중위⅓"), (0, "하위⅓")]:
                            g = ja[ja["q"] == qi]
                            if len(g):
                                bks.append({
                                    "label": f"{lab} ({g['ai_prob'].min():.2f}~{g['ai_prob'].max():.2f})",
                                    "n": int(len(g)),
                                    "avg_net": round(float(g["net_pct"].mean()), 2),
                                    "win": round(float((g["net_pct"] > 0).mean() * 100), 1),
                                })
                        out["virtual_ai"]["buckets"] = bks
                    except Exception:
                        pass
    except Exception:
        pass

    # 최근 기록 (신호일 내림차순 → 같은 날은 계좌/전략/강도순위 오름차순)
    sort_cols = [c for c in ("signal_date", "account", "strategy", "slot_rank")
                 if c in df.columns]
    asc = [False] + [True] * (len(sort_cols) - 1)
    try:
        df2 = df.sort_values(sort_cols, ascending=asc).head(limit)
    except Exception:
        df2 = df.tail(limit)
    out["rows"] = df2.drop(columns=["factors_json"], errors="ignore").fillna("").to_dict("records")
    return out


# ── 5. paper vs backtest comparison ───────────────────────────────────────────
def get_paper_vs_bt():
    out = {"paper": {}, "bt": {}, "drift": {}}
    audit_csv = BASE / "paper_audit_result.csv"
    if audit_csv.exists():
        try:
            df = pd.read_csv(audit_csv)
            done = df[df["status"] == "done"].dropna(subset=["net_pct"])
            if len(done):
                out["paper"] = {
                    "count": int(len(done)),
                    "win_rate": round(float((done["net_pct"] > 0).mean()) * 100, 1),
                    "avg_return": round(float(done["net_pct"].mean()), 2),
                    "sharpe": calc_sharpe(done["net_pct"].tolist()),
                }
        except Exception:
            pass
    if HISTORY_CSV.exists():
        try:
            hist = pd.read_csv(HISTORY_CSV)
            done = hist.dropna(subset=["net_pct"])
            out["bt"] = {
                "count": int(len(done)),
                "win_rate": round(float((done["net_pct"] > 0).mean()) * 100, 1),
                "avg_return": round(float(done["net_pct"].mean()), 2),
                "sharpe": calc_sharpe(done["net_pct"].tolist()),
            }
        except Exception:
            pass
    p, b = out["paper"], out["bt"]
    if p and b:
        out["drift"] = {
            "win_rate": round(p.get("win_rate",0) - b.get("win_rate",0), 1),
            "avg_return": round(p.get("avg_return",0) - b.get("avg_return",0), 2),
            "sharpe": round(p.get("sharpe",0) - b.get("sharpe",0), 3),
        }
    return out


# ── 무거운 재계산 방지 캐시 ──────────────────────────────────────────────────────
# 5분 자동새로고침마다 trades_history_v3.csv(6MB+)를 2회 읽고 IC 를 전부 재계산하던
# 것을 mtime 기준 1회로 줄인다. CSV 가 바뀔 때(주간 학습/데이터셋 재생성)만 갱신.
_TRADES_CACHE = {"mtime": None, "df": None}
_SCORER_CACHE = {"mtime": None, "obj": None}


def _trades_mtime():
    try:
        return HISTORY_CSV.stat().st_mtime
    except Exception:
        return None


def _load_trades_cached():
    mt = _trades_mtime()
    if _TRADES_CACHE["df"] is None or _TRADES_CACHE["mtime"] != mt:
        _TRADES_CACHE["df"] = pd.read_csv(HISTORY_CSV)
        _TRADES_CACHE["mtime"] = mt
    return _TRADES_CACHE["df"]


def _get_scorer_cached():
    mt = _trades_mtime()
    if _SCORER_CACHE["obj"] is None or _SCORER_CACHE["mtime"] != mt:
        if str(BASE) not in sys.path:
            sys.path.insert(0, str(BASE))
        from factor_scorer import FactorScorer
        _SCORER_CACHE["obj"] = FactorScorer()
        _SCORER_CACHE["mtime"] = mt
    return _SCORER_CACHE["obj"]


# ── 6. AI project status ───────────────────────────────────────────────────────
def get_ai_registry(limit=24):
    """ai_model_registry.csv (ai_trainer_v4 가 학습마다 1행 기록) → 모델 진화 표(최신순).
    버전별 선택 지표그룹·test AUC·사이징 스프레드·CV best 를 보여줘 '스스로 발전'을 가시화."""
    out = {"rows": [], "total": 0}
    path = BASE / "ai_data" / "ai_model_registry.csv"
    if not path.exists():
        return out
    try:
        df = pd.read_csv(path)
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    if df.empty:
        return out
    out["total"] = int(len(df))
    out["rows"] = df.tail(limit).iloc[::-1].fillna("").to_dict("records")   # 최신순
    return out


def get_ai_status():
    out = {}
    db_info = {}
    if DB_PATH.exists():
        db_info["size_mb"] = round(DB_PATH.stat().st_size / 1_000_000, 1)
        for tbl, q in {
            "korea_stocks":    "SELECT COUNT(*), MAX(date) FROM korea_stocks",
            "usa_stocks":      "SELECT COUNT(*), MAX(date) FROM usa_stocks",
            "supply_demand":   "SELECT COUNT(*), MAX(date) FROM supply_demand",
            "macro_indicators":"SELECT COUNT(*), MAX(date) FROM macro_indicators",
            "credit_balance":  "SELECT COUNT(*), MAX(date) FROM credit_balance",
            "news":            "SELECT COUNT(*), MAX(pubDate) FROM news",
        }.items():
            rows = safe_db(q)
            db_info[tbl] = {"count": int(rows[0][0] or 0), "latest": str(rows[0][1] or "")} if rows and rows[0][0] else {"count": 0, "latest": ""}
        dart = safe_db("SELECT COUNT(*) FROM news WHERE source='DART'")
        db_info["news_dart"] = int(dart[0][0]) if dart else 0
    else:
        db_info["error"] = "stock.db not found"
    out["db"] = db_info

    model_info = {}
    if MODEL_JSON.exists():
        mtime = datetime.fromtimestamp(MODEL_JSON.stat().st_mtime)
        model_info["last_trained"] = mtime.strftime("%Y-%m-%d %H:%M")
        model_info["age_days"] = (datetime.now() - mtime).days
    if FEAT_CSV.exists():
        try: model_info["feature_count"] = int(len(pd.read_csv(FEAT_CSV)))
        except Exception: pass
    _sch = _latest_log("collect_*.log")
    if _sch.exists():
        try:
            log_txt = _sch.read_bytes()[-30000:].decode("utf-8", errors="replace")
            auc_m = re.findall(r"AUC[:\s=]+([0-9]+\.[0-9]+)", log_txt)
            if auc_m: model_info["auc"] = float(auc_m[-1])
        except Exception: pass
    out["model"] = model_info

    ic_info = {}
    try:
        if str(BASE) not in sys.path: sys.path.insert(0, str(BASE))
        from factor_scorer import FEATURE_META
        scorer = _get_scorer_cached()
        if scorer.ic_weights:
            sorted_w = sorted(scorer.ic_weights.items(), key=lambda x: abs(x[1]), reverse=True)
            ic_info["weights"] = [{"feat": f, "label": FEATURE_META.get(f,(f,))[0], "ic": round(v,4)} for f,v in sorted_w]
            ic_info["count"] = int(len(scorer.ic_weights))
        else:
            ic_info["error"] = "No IC weights"
    except Exception as e:
        ic_info["error"] = str(e)[:120]
    out["ic"] = ic_info

    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    sent_rows = safe_db("SELECT sentiment, COUNT(*) FROM news WHERE pubDate >= ? GROUP BY sentiment", (week_ago,))
    if sent_rows:
        sentiment = {"positive": 0, "negative": 0, "neutral": 0}
        for raw_key, cnt in sent_rows:
            sentiment[_norm_sent(raw_key)] += cnt
        out["news"] = {"week_total": sum(sentiment.values()), "sentiment": sentiment}
    else:
        out["news"] = {"week_total": 0, "sentiment": {}}
    recent = safe_db("SELECT ticker, title, sentiment, pubDate, source FROM news ORDER BY pubDate DESC LIMIT 15")
    out["news"]["recent"] = [{"ticker": r[0] or "", "title": str(r[1] or "")[:65], "sentiment": _norm_sent(r[2]), "date": str(r[3] or "")[:10], "source": r[4] or ""} for r in recent]
    return out


def get_training_stats():
    out = {"available": False}
    if not HISTORY_CSV.exists():
        out["error"] = "trades_history_v3.csv not found"; return out
    try:
        df = _load_trades_cached()
        out["available"] = True
        out["total_rows"] = int(len(df))
        date_col = next((c for c in ["date","entry_date"] if c in df.columns), None)
        if date_col:
            out["date_min"] = str(df[date_col].min())
            out["date_max"] = str(df[date_col].max())
        feat_cols = [c for c in df.columns if c not in _META_COLS]
        out["feature_count"] = int(len(feat_cols))
        nan_rates = []
        for c in feat_cols:
            na_sum = int(df[c].isna().sum())
            nan_rates.append({"feat": c, "nan_pct": round(na_sum/len(df)*100,1), "filled": int(len(df)-na_sum)})
        nan_rates.sort(key=lambda x: x["nan_pct"], reverse=True)
        out["features"] = nan_rates
        if "strategy" in df.columns:
            sc = df["strategy"].value_counts().head(10)
            out["strategies"] = [{"name": str(k), "count": int(v)} for k,v in sc.items()]
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


# ── AI scoring constants ────────────────────────────────────────────────────
# 추론 폴백 순서 — feature_spec 단일 출처에서 가져온다(2026-06-29 단일화).
#   import 실패 시에만 쓰는 하드코딩 폴백을 except 에 둔다(대시보드 기동 복원력 유지).
try:
    from feature_spec import FALLBACK_MODEL_ORDER as _AI_FEAT_ORDER
except Exception:
    _AI_FEAT_ORDER = [
        "rsi14","atr_pct","vol_ratio","tv_ratio","for_5d","ins_5d","mcap_class",
        "score_tv","news_sent_7d","news_cnt_7d","vix","vix_chg_5d","sox_ret_5d",
        "usdkrw_chg_5d","kospi_ret_20d","for_net5_db","ins_net5_db","rsi_db",
        "macd_hist_db","bb_pct_db","prm_net_5d_ratio",
        "strat_h252_40","strat_h500_20","strat_h500_40_MKT",
    ]
_AI_FEAT_LABELS = {
    "vix":           ("서프라이즈안정", +1),  # VIX 낮음=좋음
    "vix_chg_5d":    ("공포지수상승",     -1),
    "kospi_ret_20d": ("시장상승충",           +1),
    "sox_ret_5d":    ("반도체강세",           +1),
    "usdkrw_chg_5d": ("강달러압박",           -1),
    "for_net5_db":   ("외국인순매수",     +1),
    "ins_net5_db":   ("기관순매수",           +1),
    "for_5d":        ("외국인매수",           +1),
    "ins_5d":        ("기관매수",                 +1),
    "rsi14":         ("RSI과매수",                    -1),
    "atr_pct":       ("고변동성",                 -1),
    "vol_ratio":     ("거래량급증",           +1),
    "tv_ratio":      ("거래대금급증",     +1),
    "news_sent_7d":  ("뉴스긍정",                +1),
    "macd_hist_db":  ("MACD상승",                        +1),
    "bb_pct_db":     ("볼린저상단",          -1),
}
_ai_model_cache = [None, ""]   # [model, date_str]


def _load_ai_feat_order():
    """학습된 모델의 실제 피처 순서 — feature_spec 단일 출처에서 로드(없으면 폴백).
    추론 피처셋을 학습과 일치시켜 XGBoost feature_names·차원 불일치(→ 조용히 빈
    AI점수)를 막는다. feature_spec import 실패 시 _AI_FEAT_ORDER 로 폴백."""
    try:
        from feature_spec import load_model_feature_order
        return load_model_feature_order()
    except Exception:
        return list(_AI_FEAT_ORDER)


def _ai_score_signals(codes):
    """신호 종목 AI 대박확률 계산. 실패 시 빈 dict (graceful)."""
    if not codes:
        return {}
    try:
        import xgboost as xgb
        _use_xgb = True
    except ImportError:
        try:
            import joblib as _jl
            _use_xgb = False
        except ImportError:
            return {}

    today = datetime.today().strftime("%Y%m%d")
    model = _ai_model_cache[0]

    if _ai_model_cache[1] != today or model is None:
        if _use_xgb and MODEL_JSON.exists():
            try:
                m = xgb.Booster(); m.load_model(str(MODEL_JSON))
                _ai_model_cache[0], _ai_model_cache[1] = m, today
                model = m
            except Exception:
                return {}
        else:
            pkl_p = MODEL_JSON.with_suffix(".pkl")
            if not pkl_p.exists():
                return {}
            try:
                model = _jl.load(str(pkl_p))
                _ai_model_cache[0], _ai_model_cache[1] = model, today
            except Exception:
                return {}

    if model is None:
        return {}

    feat_order = _load_ai_feat_order()   # 학습된 실제 피처 순서(그룹탐색으로 가변)
    codes_q = ",".join(f"'{c}'" for c in codes)

    # ── OHLCV + 기술지표 계산 ─────────────────────────────────────────────
    rows = safe_db(
        f"SELECT date,code,close,high,low,volume,trading_value,market_cap "
        f"FROM korea_stocks WHERE code IN ({codes_q}) ORDER BY date"
    )
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["date","code","close","high","low",
                                     "volume","trading_value","market_cap"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    for col in ["close","high","low","volume","trading_value","market_cap"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values(["code","date"])

    delta = df.groupby("code")["close"].diff()
    gain, loss = delta.clip(lower=0), (-delta).clip(lower=0)
    ag = gain.groupby(df["code"]).transform(lambda x: x.ewm(alpha=1/14, adjust=False).mean())
    al = loss.groupby(df["code"]).transform(lambda x: x.ewm(alpha=1/14, adjust=False).mean())
    df["rsi14"] = 100 - 100 / (1 + ag / al.replace(0, np.nan))

    df["prev_c"] = df.groupby("code")["close"].shift(1)
    df["tr"] = np.maximum(df["high"]-df["low"],
                np.maximum((df["high"]-df["prev_c"]).abs(),(df["low"]-df["prev_c"]).abs()))
    atr = df.groupby("code")["tr"].transform(lambda x: x.ewm(alpha=1/14, adjust=False).mean())
    df["atr_pct"] = atr / df["close"].replace(0, np.nan) * 100

    vm20 = df.groupby("code")["volume"].transform(lambda x: x.shift(1).rolling(20,min_periods=10).mean())
    tm20 = df.groupby("code")["trading_value"].transform(lambda x: x.shift(1).rolling(20,min_periods=10).mean())
    df["vol_ratio"] = df["volume"] / vm20.replace(0, np.nan)
    df["tv_ratio"]  = df["trading_value"] / tm20.replace(0, np.nan)

    df["mcap_class"] = 3
    df.loc[df["market_cap"] >= 1e12, "mcap_class"] = 2
    df.loc[df["market_cap"] >= 1e13, "mcap_class"] = 1

    last_date = df["date"].max()
    ld = df[df["date"] == last_date].copy()
    ld["score_tv"] = ld["trading_value"].rank(ascending=False, method="min")
    score_map = ld.set_index("code")["score_tv"].to_dict()
    latest = df[df["date"] == last_date].set_index("code")

    # ── 수급 ─────────────────────────────────────────────────────────────
    ld_str = last_date[:10] if "-" in str(last_date) else f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"
    sd_rows = safe_db(
        f"SELECT code, SUM(foreign_net), SUM(inst_net) FROM supply_demand "
        f"WHERE code IN ({codes_q}) AND date >= date('{ld_str}','-7 days') GROUP BY code"
    )
    sd_map = {str(r[0]).zfill(6): (float(r[1] or 0), float(r[2] or 0)) for r in sd_rows}

    # ── 뉴스 ─────────────────────────────────────────────────────────────
    pw = (datetime.today()-timedelta(days=7)).strftime("%Y%m%d")
    nw_rows = safe_db(
        f"SELECT ticker, AVG(CAST(sentiment AS FLOAT)), COUNT(*) FROM news "
        f"WHERE ticker IN ({codes_q}) AND pubDate >= '{pw}' GROUP BY ticker"
    )
    nw_map = {str(r[0]).zfill(6): (float(r[1] or 0), int(r[2] or 0)) for r in nw_rows}

    # ── 매크로 ────────────────────────────────────────────────────────────
    mac_rows = safe_db(
        "SELECT indicator,close FROM macro_indicators "
        "WHERE date=(SELECT MAX(date) FROM macro_indicators)"
    )
    mac = {str(r[0]): float(r[1] or 0) for r in mac_rows}
    vix_val = mac.get("VIX", np.nan)

    def _mac_ret(ind, n):
        rs = safe_db(f"SELECT close FROM macro_indicators WHERE indicator='{ind}' "
                     f"ORDER BY date DESC LIMIT {n+1}")
        if len(rs) >= n+1:
            return (float(rs[0][0] or 0) / float(rs[n][0] or 1) - 1) * 100
        return 0.0

    vix_chg   = _mac_ret("VIX", 5)
    kospi_r20 = _mac_ret("KOSPI", 20)
    sox_r5    = _mac_ret("SOX", 5)
    usd_r5    = _mac_ret("USDKRW", 5)

    # ── 종목별 벡터 → 예측 ───────────────────────────────────────────────
    feat_vecs, code_list = [], []
    for code in [str(c).zfill(6) for c in codes]:
        if code not in latest.index:
            continue
        r = latest.loc[code]
        sd = sd_map.get(code, (np.nan, np.nan))
        nw = nw_map.get(code, (np.nan, 0))
        fv = {
            "rsi14":           float(r.get("rsi14", np.nan)),
            "atr_pct":         float(r.get("atr_pct", np.nan)),
            "vol_ratio":       float(r.get("vol_ratio", np.nan)),
            "tv_ratio":        float(r.get("tv_ratio", np.nan)),
            "for_5d":          np.nan,
            "ins_5d":          np.nan,
            "mcap_class":      float(r.get("mcap_class", 3)),
            "score_tv":        float(score_map.get(code, np.nan)),
            "news_sent_7d":    float(nw[0]),
            "news_cnt_7d":     float(nw[1]),
            "vix":             float(vix_val),
            "vix_chg_5d":      float(vix_chg),
            "sox_ret_5d":      float(sox_r5),
            "usdkrw_chg_5d":   float(usd_r5),
            "kospi_ret_20d":   float(kospi_r20),
            "for_net5_db":     float(sd[0]),
            "ins_net5_db":     float(sd[1]),
            "rsi_db":          float(r.get("rsi14", np.nan)),
            "macd_hist_db":    np.nan,
            "bb_pct_db":       np.nan,
            "prm_net_5d_ratio": np.nan,
            "strat_h252_40":   0,
            "strat_h500_20":   0,
            "strat_h500_40_MKT": 1,
        }
        feat_vecs.append([fv.get(f, np.nan) for f in feat_order])
        code_list.append((code, fv))

    if not feat_vecs:
        return {}

    try:
        arr = np.array(feat_vecs, dtype=float)
        if _use_xgb:
            probs = model.predict(xgb.DMatrix(arr, feature_names=feat_order))
        else:
            probs = model.predict_proba(arr)[:, 1]
    except Exception:
        return {}

    results = {}
    for i, (code, fv) in enumerate(code_list):
        p = float(probs[i])
        pos, neg = [], []
        for feat, (label, direction) in _AI_FEAT_LABELS.items():
            val = fv.get(feat)
            if val is None or (isinstance(val, float) and (np.isnan(val) or val == 0)):
                continue
            if direction > 0 and val > 0:
                pos.append(label)
            elif direction < 0 and val > 0:
                neg.append(label)
        results[code] = {
            "bigwin_pct": round(p * 100, 1),
            "top_pos": pos[:2],
            "top_neg": neg[:2],
        }
    return results


def get_candidates():
    """
    매매 후보 종목:
      1. 오늘 신규 신호 (paper_signals.csv 에서 signal_date == today) → 내일 매수 예정
      2. 최근 랭킹 CSV 상위 종목 (오늘 없으면 최신 파일)
      3. 랭킹 종목 중 paper_signals.csv 에 이미 올라 있는 종목 표시
    """
    out = {"new_signals": [], "rankings": [], "rank_date": "", "new_signal_count": 0}
    today = datetime.today().strftime("%Y%m%d")

    # ── 기존 시그널 집합 (이미 신호 난 code 목록) ──
    existing_codes = set()
    if PAPER_CSV.exists():
        try:
            df = pd.read_csv(PAPER_CSV, dtype={"code": str})
            df["code"] = df["code"].str.zfill(6)
            existing_codes = set(df["code"].tolist())
            # 오늘 신규 신호
            today_sigs = df[df["signal_date"].astype(str) == today]
            out["new_signal_count"] = int(len(today_sigs))
            out["new_signals"] = [
                {"code": str(r["code"]).zfill(6),
                 "name": str(r.get("name", "")),
                 "entry_price": float(r.get("entry_price_close", 0) or 0),
                 "target_exit": str(r.get("target_exit_date", "")),
                 "signal_date": str(r.get("signal_date", ""))}
                for _, r in today_sigs.iterrows()
            ]
            # AI 대박확률 보강 (모델 없으면 graceful skip)
            try:
                sig_codes = [s["code"] for s in out["new_signals"]]
                ai_scores = _ai_score_signals(sig_codes)
                for s in out["new_signals"]:
                    ai = ai_scores.get(s["code"], {})
                    s["ai_bigwin"] = ai.get("bigwin_pct")
                    s["ai_pos"] = ai.get("top_pos", [])
                    s["ai_neg"] = ai.get("top_neg", [])
            except Exception:
                pass
        except Exception as e:
            out["new_signals_error"] = str(e)[:60]

    # ── 최신 랭킹 CSV ──
    rank_dir = BASE / "data" / "rankings"
    if rank_dir.exists():
        rank_files = sorted(rank_dir.glob("*.csv"))
        if rank_files:
            latest_rank = rank_files[-1]
            out["rank_date"] = latest_rank.stem
            try:
                rdf = pd.read_csv(latest_rank, dtype={"code": str})
                rdf["code"] = rdf["code"].astype(str).str.zfill(6)
                # 각 종목에 "이미 신호" 여부 표시
                rows = []
                for _, r in rdf.head(30).iterrows():
                    code = str(r["code"]).zfill(6)
                    rows.append({
                        "rank": int(r.get("rank", 0)),
                        "code": code,
                        "name": str(r.get("name", "")),
                        "price": float(r.get("current_price", 0) or 0),
                        "change_pct": float(r.get("change_pct", 0) or 0),
                        "trading_value": int(r.get("trading_value", 0) or 0),
                        "in_signals": code in existing_codes,
                        "is_new_today": str(r.get("signal_date", "")) == today,
                    })
                out["rankings"] = rows
            except Exception as e:
                out["rank_error"] = str(e)[:60]
    return out


def get_file_meta():
    candidates = {
        "paper_signals.csv": PAPER_CSV, "trades_history_v3.csv": HISTORY_CSV,
        "meta_model_v4.json": MODEL_JSON, "meta_model_v4.features.csv": FEAT_CSV,
        "collect_*.log (최신)": _latest_log("collect_*.log"),
        "paper_*.log (최신)":   _latest_log("paper_*.log"),
        "db/kiwoom/snapshot.json": KIWOOM_DIR/"snapshot.json",
        "db/kiwoom/equity_history.csv": KIWOOM_DIR/"equity_history.csv",
    }
    out = []
    for label, p in candidates.items():
        if p.exists():
            st = p.stat()
            out.append({"name": label, "size_kb": round(st.st_size/1024,1), "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        else:
            out.append({"name": label, "size_kb": 0, "mtime": "-"})
    return out


# ── 7. API routes ──────────────────────────────────────────────────────────────
def _json_safe(obj):
    """NaN/Inf → None 재귀 치환. 파이썬 json 은 NaN 을 'NaN' 으로 직렬화하는데
    이는 표준 JSON 이 아니라 브라우저 JSON.parse 가 거부 → 대시보드 전체 로드 실패.
    (PowerShell ConvertFrom-Json 은 허용해서 증상이 가려졌었음.)"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        # NaN 은 자기 자신과 != , Inf 도 함께 걸러 null 로.
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
    return obj


@app.route("/api/all")
def api_all():
    df = request.args.get("date_from", "")
    dt = request.args.get("date_to", "")
    # 개별 getter 예외를 격리해 하나가 실패해도 전체 JSON 반환
    def _safe(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            import traceback
            print(f"[Dashboard] /api/all ERROR in {fn.__name__}: {e}", flush=True)
            traceback.print_exc()
            return {"error": str(e)[:200]}
    try:
        payload = {
            "ts":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "freshness":  _safe(get_freshness),
            "health":     _safe(get_health),
            "cheonok":    _safe(get_cheonok),
            "bt":         _safe(get_backtest_trades, df, dt),
            "sc":         _safe(get_strategy_compare),
            "lab":        _safe(get_strategy_lab),
            "mock":       _safe(get_kiwoom_mock, df, dt),
            "kis":        _safe(get_kis_mock, df, dt),
            "strength":   _safe(get_strength),
            "ai_paper":   _safe(get_ai_paper),
            "pvbt":       _safe(get_paper_vs_bt),
            "ai":         _safe(get_ai_status),
            "ai_registry": _safe(get_ai_registry),
            "train":      _safe(get_training_stats),
            "candidates": _safe(get_candidates),
            "files":      _safe(get_file_meta),
            "logs":       {"sch":  tail_log(_latest_log("collect_*.log"), 80),
                           "live": tail_log(_latest_log("paper_*.log"), 40)},
        }
        return jsonify(_json_safe(payload))
    except Exception as e:
        import traceback
        print(f"[Dashboard] /api/all FATAL: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"error": str(e)[:300], "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}), 200

@app.route("/api/logs")
def api_logs():
    return jsonify({"sch": tail_log(_latest_log("collect_*.log"), 100),
                    "live": tail_log(_latest_log("paper_*.log"), 50)})


@app.route("/api/logfiles")
def api_logfiles():
    """logs/ 의 모든 .log 파일 목록(최신순) — 수동실행 run_*.log 포함."""
    out = []
    try:
        for p in LOG_DIR.glob("*.log"):
            try:
                st = p.stat()
                out.append({"name": p.name, "kb": round(st.st_size / 1024, 1),
                            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")})
            except Exception:
                pass
        out.sort(key=lambda x: x["mtime"], reverse=True)
    except Exception as e:
        return jsonify({"files": [], "error": str(e)[:80]})
    return jsonify({"files": out})


@app.route("/api/logfile/<name>")
def api_logfile(name):
    """선택한 로그 파일의 '전체' 내용(최대 ~500KB). 경로탈출 차단."""
    # 파일명만 허용(디렉토리 구분자/.. 차단)
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".log"):
        return jsonify({"name": name, "content": "", "error": "잘못된 파일명"}), 400
    p = LOG_DIR / name
    if not p.exists() or p.parent.resolve() != LOG_DIR.resolve():
        return jsonify({"name": name, "content": "", "error": "파일 없음"}), 404
    try:
        raw = p.read_bytes()
        truncated = len(raw) > 500_000
        raw = raw[-500_000:]
        for enc in ("utf-8", "cp949"):
            try:
                txt = raw.decode(enc); break
            except UnicodeDecodeError:
                continue
        else:
            txt = raw.decode("utf-8", errors="replace")
        if truncated:
            txt = "...(앞부분 생략 — 최근 500KB만 표시)...\n" + txt
        return jsonify({"name": name, "content": txt, "kb": round(p.stat().st_size / 1024, 1)})
    except Exception as e:
        return jsonify({"name": name, "content": "", "error": str(e)[:80]})

@app.route("/api/marketgrid")
def api_marketgrid():
    """장중시황 2x6 그리드(market_grid.py 생성, 키움 순위) — 코스피·코스닥별 6지표."""
    p = BASE / "db" / "kiwoom" / "market_grid.json"
    if not p.exists():
        return jsonify({"markets": {}, "updated": None})
    try:
        return jsonify(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        return jsonify({"markets": {}, "error": str(e)[:80]})


@app.route("/api/preview")
def api_preview():
    """장중 잠정 예비후보(intraday_preview.py 생성) — 정보용, 매매 트리거 아님."""
    p = BASE / "db" / "kiwoom" / "intraday_preview.json"
    if not p.exists():
        return jsonify({"items": [], "updated": None})
    try:
        return jsonify(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        return jsonify({"items": [], "error": str(e)[:80]})


@app.route("/api/intraday")
def api_intraday():
    """장중 시황 캐시 반환 — intraday_monitor.py 가 생성한 JSON 파일을 서빙."""
    if not INTRADAY_CACHE.exists():
        return jsonify({
            "error": "intraday_monitor.py 를 먼저 실행하세요 (또는 스케줄러 등록)",
            "data": None,
            "updated_at": None,
        })
    try:
        data = json.loads(INTRADAY_CACHE.read_text(encoding="utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)[:80], "data": None})


@app.route("/api/intraday/refresh", methods=["POST"])
def api_intraday_refresh():
    """intraday_monitor.run() 즉시 실행 후 최신 캐시 반환."""
    try:
        import intraday_monitor
        intraday_monitor.run()
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-500:]}), 500
    if not INTRADAY_CACHE.exists():
        return jsonify({"error": "캐시 파일 없음", "data": None})
    try:
        data = json.loads(INTRADAY_CACHE.read_text(encoding="utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)[:80]})


@app.route("/api/paper")
def api_paper():
    """Full paper_signals.csv list."""
    if not PAPER_CSV.exists():
        return jsonify({"total": 0, "rows": []})
    try:
        df = pd.read_csv(PAPER_CSV, dtype={"code": str})
        df["code"] = df["code"].str.zfill(6)
        rows = []
        for _, r in df.iterrows():
            rec = {}
            for c in df.columns:
                v = r[c]
                rec[c] = "" if (isinstance(v, float) and np.isnan(v)) else (str(v) if not isinstance(v, (int, str, float)) else v)
            rows.append(rec)
        return jsonify({"total": len(rows), "rows": rows})
    except Exception as e:
        return jsonify({"total": 0, "rows": [], "error": str(e)[:80]})


# ── 9. 수동 실행 API (dashboard.html 제어판용) ───────────────────────────────────
import subprocess as _sp

_VENV_PYTHON = str(BASE / ".venv" / "Scripts" / "python.exe")
if not Path(_VENV_PYTHON).exists():
    _VENV_PYTHON = sys.executable   # fallback — 현재 인터프리터

_NO_WIN = getattr(_sp, "CREATE_NO_WINDOW", 0x08000000)   # Windows 콘솔창 숨김

# Stock_AI_Project 수집기(개별 수동실행용) — 자체 venv + cwd 필요.
_STOCK_AI_DIR = BASE.parent / "Stock_AI_Project"
_STOCK_AI_PY  = str(_STOCK_AI_DIR / "venv" / "Scripts" / "python.exe")
if not Path(_STOCK_AI_PY).exists():
    _STOCK_AI_PY = _VENV_PYTHON   # fallback
_CREDIT_SINCE = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")

_RUN_TASKS = {
    "live_signal":   [_VENV_PYTHON, str(BASE / "live_signal.py")],
    "paper_tracker": [_VENV_PYTHON, str(BASE / "paper_tracker.py")],
    "kiwoom_buy":    [_VENV_PYTHON, str(BASE / "kiwoom_trader.py"), "buy"],
    "kiwoom_sell":   [_VENV_PYTHON, str(BASE / "kiwoom_trader.py"), "sell"],
    "kiwoom_status": [_VENV_PYTHON, str(BASE / "kiwoom_trader.py"), "status"],
    "kis_signal":    [_VENV_PYTHON, str(BASE / "kis_live_signal.py")],
    "kis_daily":     [_VENV_PYTHON, str(BASE / "kis_trader.py"), "daily"],
    "kis_status":    [_VENV_PYTHON, str(BASE / "kis_trader.py"), "status"],
    "recheck":       [_VENV_PYTHON, str(BASE / "recollect_guard.py")],   # 구 recheck_collector.py 이름변경 반영
    # ── 데이터 수집 / AI 학습 / 전략 랩 (2026-06-20 추가) ──
    "backfill":      ["powershell", "-ExecutionPolicy", "Bypass", "-File",
                      str(BASE.parent / "Stock_AI_Project" / "run_backfill.ps1")],   # Stock_AI 수집기 일괄(자체 cwd/venv)
    "ai_dataset":    [_VENV_PYTHON, str(BASE / "make_trades_history_v3.py")],        # AI 학습 데이터셋 생성
    "ai_train":      [_VENV_PYTHON, str(BASE / "ai_trainer_v4.py")],                 # meta_model_v4 재학습
    "ai_pipeline":   ["cmd", "/c", str(BASE / "run_ai_pipeline.bat"), "auto"],       # 주1회 경량 AI 갱신(수집→데이터셋→학습, 게이팅)
    "strategy_lab":  [_VENV_PYTHON, str(BASE / "strategy_lab.py")],                  # 전략 랩 forward 집계
    "preview":       [_VENV_PYTHON, str(BASE / "intraday_preview.py")],              # 장중 잠정 예비후보
    "market_grid":   [_VENV_PYTHON, str(BASE / "market_grid.py")],                   # 장중시황 2x6 그리드(키움)
    "daily_summary": [_VENV_PYTHON, str(BASE / "daily_summary.py")],                  # 텔레그램 하루요약 수동 전송
    "watchdog":      [_VENV_PYTHON, str(BASE / "watchdog.py")],                       # 조용한 실패 감지 → 텔레그램(수동 점검)
    "kis_stopcheck": [_VENV_PYTHON, str(BASE / "kis_trader.py"), "stopcheck"],        # KIS 장중 손절 모니터
    # ── 개별 데이터 수집기 (정체분만 콕 집어 수동수집 — Stock_AI venv/cwd) ──
    "supply_demand": [_STOCK_AI_PY, "-m", "src.collector.supply_demand"],            # 외국인/기관 수급(KIS)
    "macro":         [_STOCK_AI_PY, "-m", "src.collector.macro"],                    # 거시지표(NASDAQ/VIX/환율)
    "credit":        [_STOCK_AI_PY, "-m", "src.collector.kiwoom_extra",
                      "--backfill", "--since", _CREDIT_SINCE, "--force"],            # 신용/대차(키움)
                      # --force: 수집기의 '주말 전용' 가드 우회 — 수동 버튼은 사용자가 시점을
                      # 직접 고르는 것이므로 평일에도 동작(2026-07-06). 매매창(09:00~05·15:20~30)
                      # 밖 실행 권장은 버튼 안내 그대로 유지.
    "after_market":  [_VENV_PYTHON, str(BASE / "after_market.py")],                  # 시간외 등락률 → stock.db 누적
}

# 작업별 cwd (미지정 시 outputs). Stock_AI 수집기는 프로젝트 루트에서 실행해야 -m 임포트가 됨.
_RUN_CWD = {
    "supply_demand": str(_STOCK_AI_DIR),
    "macro":         str(_STOCK_AI_DIR),
    "credit":        str(_STOCK_AI_DIR),
}


@app.after_request
def _add_cors(resp):
    """dashboard.html(file://)에서 fetch 가능하도록 CORS 허용."""
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/api/run/<task>", methods=["POST", "OPTIONS"])
def api_run(task):
    """수동 실행 엔드포인트 — 로컬 접속만 허용."""
    if request.method == "OPTIONS":
        return "", 204
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "msg": "로컬에서만 허용"}), 403

    # 스케줄러 재시작
    if task == "scheduler_restart":
        bat = BASE.parent / "Stock_AI_Project" / "run_scheduler.bat"
        if bat.exists():
            try:
                _sp.Popen(["cmd", "/c", str(bat)], cwd=str(bat.parent),
                          creationflags=_NO_WIN)
                return jsonify({"ok": True, "msg": "스케줄러 재시작 명령 전송"})
            except Exception as e:
                return jsonify({"ok": False, "msg": str(e)})
        return jsonify({"ok": False, "msg": "run_scheduler.bat 없음"})

    if task not in _RUN_TASKS:
        return jsonify({"ok": False, "msg": f"알 수 없는 작업: {task}"}), 400

    try:
        # 출력은 로그 파일로 (PIPE 미소비 시 긴 작업이 버퍼 차서 멈추는 것 방지).
        log_dir = BASE / "logs"
        log_dir.mkdir(exist_ok=True)
        logf = open(log_dir / f"run_{task}.log", "w", encoding="utf-8", errors="replace")
        proc = _sp.Popen(
            _RUN_TASKS[task], cwd=_RUN_CWD.get(task, str(BASE)),
            stdout=logf, stderr=_sp.STDOUT,
            creationflags=_NO_WIN,
        )
        return jsonify({"ok": True, "msg": f"시작됨 (PID {proc.pid}) → logs/run_{task}.log 에 기록"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ── 10. HTML ───────────────────────────────────────────────────────────────────

HTML = """\
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>천억이 대시보드 v4</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',system-ui,sans-serif;font-size:14px}
a{color:#58a6ff}
/* 제어판 버튼 */
.runbtn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:6px 12px;border-radius:6px;
        cursor:pointer;font-size:13px}
.runbtn:hover{background:#30363d;border-color:#58a6ff}
.runbtn.danger{border-color:#6e2a2a}
.runbtn.danger:hover{background:#3d1f1f;border-color:#f85149}
.runbtn.stale{background:#3d1f1f;border-color:#f85149;color:#ffb3ad;
  box-shadow:0 0 0 1px #f85149;animation:stalepulse 1.6s ease-in-out infinite}
.runbtn.stale::before{content:"\26A0 ";color:#f85149}
@keyframes stalepulse{0%,100%{box-shadow:0 0 2px 0 #f85149}50%{box-shadow:0 0 9px 1px #f85149}}
/* header */
header{background:#161b22;padding:8px 16px;display:flex;justify-content:space-between;align-items:center;
       border-bottom:1px solid #30363d;position:sticky;top:0;z-index:20}
header h1{font-size:16px;font-weight:600}
#ts{font-size:12px;color:#8b949e}
button#btn-refresh{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 12px;
                   border-radius:6px;cursor:pointer;font-size:13px}
button#btn-refresh:hover{background:#30363d}
/* freshness banner */
#freshness-banner{padding:4px 16px;font-size:12px}
.fresh-ok{color:#3fb950}.fresh-warn{color:#d29922}.fresh-err{color:#f85149;font-weight:600}
/* tabs */
.tabs{display:flex;gap:0;border-bottom:1px solid #30363d;background:#161b22;padding:0 12px}
.tab{padding:10px 16px;cursor:pointer;font-size:13px;color:#8b949e;border-bottom:2px solid transparent;
     white-space:nowrap;transition:color .15s}
.tab:hover{color:#c9d1d9}
.tab.active{color:#58a6ff;border-bottom-color:#1f6feb}
/* tab content */
.tab-content{display:none;padding:20px 16px}
.tab-content.active{display:block}
/* cards / sections */
.section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}
.section h2{font-size:13px;font-weight:600;color:#8b949e;margin-bottom:12px;text-transform:uppercase;letter-spacing:.5px}
/* stat boxes */
.stats-row{display:flex;flex-wrap:wrap;gap:12px}
.stat{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:12px 16px;min-width:110px}
.stat .v{font-size:22px;font-weight:700;line-height:1.2}
.stat .l{font-size:11px;color:#8b949e;margin-top:4px}
/* colors */
.pos{color:#3fb950}.neg{color:#f85149}.neu{color:#c9d1d9}
/* tables */
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#8b949e;font-weight:500;padding:6px 8px;border-bottom:1px solid #21262d;font-size:12px}
td{padding:6px 8px;border-bottom:1px solid #161b22}
/* 행 hover 하이라이트 — 가로 전체 박스 + 파란 밑줄 (모든 탭 표 공통) */
tbody tr:hover td{background:#1c2330;box-shadow:inset 0 -2px 0 0 #388bfd}
.r{text-align:right}
/* 모의투자 슬롯 아코디언 (클릭 시 진입/청산 조건 펼침) */
.slotitem{width:100%;background:#0d1117;border:1px solid #21262d;border-radius:6px;margin-bottom:6px;overflow:hidden}
.slothead{display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer;user-select:none}
.slothead:hover{background:#161b22}
.slotname{flex:1;font-weight:600}
.caret{color:#8b949e;font-size:12px}
.slotbody{display:none;padding:0 12px 12px;font-size:12px;color:#c9d1d9;line-height:1.65;border-top:1px solid #21262d}
.slotbody.open{display:block;padding-top:10px}
/* 로그 드롭다운(details/summary) */
details.logdrop>summary{cursor:pointer;font-size:13px;font-weight:600;color:#8b949e;text-transform:uppercase;
  letter-spacing:.5px;list-style:none;padding:2px 0}
details.logdrop>summary::-webkit-details-marker{display:none}
details.logdrop>summary::before{content:"\25B8  ";color:#58a6ff}
details.logdrop[open]>summary::before{content:"\25BE  ";color:#58a6ff}
/* 정렬 가능한 표 헤더 */
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:#58a6ff;text-decoration:underline}
/* log pre */
pre.logbox{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:12px;
           font-size:11px;font-family:monospace;line-height:1.6;white-space:pre-wrap;
           word-break:break-all;max-height:480px;overflow-y:auto;color:#8b949e}
/* file table */
.file-ok{color:#3fb950}.file-warn{color:#d29922}.file-err{color:#f85149}
/* error box */
.err-box{background:#21262d;border:1px solid #f85149;border-radius:6px;padding:10px 14px;
         color:#f85149;font-size:13px;margin:8px 0}
/* intraday */
.intra-price{font-size:20px;font-weight:700}
</style>
</head>
<body>

<header>
  <h1>&#x1F4B9; &#xCC9C;&#xC5B5;&#xC774; &#xB300;&#xC2DC;&#xBCF4;&#xB4DC;</h1>
  <div style="display:flex;align-items:center;gap:12px">
    <span id="ts" style="color:#8b949e;font-size:12px">&#xB85C;&#xB529;&#xC911;...</span>
    <button id="btn-refresh" onclick="load()">&#x21BB; &#xAC31;&#xC2E0;</button>
  </div>
</header>
<div id="freshness-banner"></div>

<div class="tabs">
  <div class="tab active"  onclick="showTab('overview')">&#x1F4CA; &#xAC1C;&#xC694;</div>
  <div class="tab"         onclick="showTab('cheonok')">&#x1F4CC; &#xCC9C;&#xC5B5;&#xC774;</div>
  <div class="tab"         onclick="showTab('mock')">&#x1F3E6; &#xBAA8;&#xC758;&#xACC4;&#xC88C;</div>
  <div class="tab"         onclick="showTab('strength')">&#x1F4AA; &#xAC15;&#xB3C4;&#xB9E4;&#xB9E4;</div>
  <div class="tab"         onclick="showTab('backtest')">&#x1F4C8; &#xBC31;&#xD14C;&#xC2A4;&#xD2B8;</div>
  <div class="tab"         onclick="showTab('aimodel')">&#x1F916; AI&#xBAA8;&#xB378;</div>
  <div class="tab"         onclick="showTab('datalogs')">&#x1F4DC; &#xB370;&#xC774;&#xD130;+&#xB85C;&#xADF8;</div>
  <div class="tab"         onclick="showTab('intraday')">&#x1F4F0; &#xC7A5;&#xC911;&#xC2DC;&#xD669;</div>
</div>

<!-- ===== 개요 ===== -->
<div id="tab-overview" class="tab-content active">
  <div class="section">
    <h2>&#xC8FC;&#xC694; &#xD30C;&#xC77C; &#xD604;&#xD669;</h2>
    <table><thead><tr>
      <th>&#xD30C;&#xC77C;</th><th class="r">&#xD06C;&#xAE30;</th><th>&#xC218;&#xC815;&#xC2DC;&#xAC01;</th><th>&#xC0C1;&#xD0DC;</th>
    </tr></thead>
    <tbody id="dl-files"></tbody>
    </table>
  </div>
  <div class="section">
    <h2>&#xCC9C;&#xC5B5;&#xC774; &#xC131;&#xACFC; (&#xBC31;&#xD14;&#xC2A4;&#xD2B8; &#xD3BC;&#xD3EC;)</h2>
    <div class="stats-row">
      <div class="stat"><div class="v pos" id="ov-cap">-</div><div class="l">&#xCD5C;&#xC885; &#xC790;&#xBCF8;</div></div>
      <div class="stat"><div class="v" id="ov-wr">-</div><div class="l">&#xC2B9;&#xB960;</div></div>
      <div class="stat"><div class="v neg" id="ov-mdd">-</div><div class="l">MDD</div></div>
      <div class="stat"><div class="v" id="ov-sharpe">-</div><div class="l">&#xC0E4;&#xD504;</div></div>
      <div class="stat"><div class="v" id="ov-trades">-</div><div class="l">&#xCD1D; &#xAC70;&#xB798;</div></div>
      <div class="stat"><div class="v" id="ov-active">-</div><div class="l">&#xD65C;&#xC131; &#xC2E0;&#xD638;</div></div>
    </div>
  </div>
  <div class="section">
    <h2>&#xC2DC;&#xC2A4;&#xD15C; &#xC0C1;&#xD0DC;</h2>
    <div class="stats-row">
      <div class="stat"><div class="v" id="ov-dbsize">-</div><div class="l">DB &#xD06C;&#xAE30;</div></div>
      <div class="stat"><div class="v" id="ov-model">-</div><div class="l">&#xBAA8;&#xB378; &#xD559;&#xC2B5;&#xC77C;</div></div>
      <div class="stat"><div class="v" id="ov-news7">-</div><div class="l">&#xB274;&#xC2A4;(7&#xC77C;)</div></div>
      <div class="stat"><div class="v pos" id="ov-newsig">-</div><div class="l">&#xC624;&#xB298; &#xC2E0;&#xADDC;&#xC2E0;&#xD638;</div></div>
    </div>
  </div>
  <div class="section">
    <h2>&#xC624;&#xB298; &#xB9E4;&#xB9E4; &#xD6C4;&#xBCF4;</h2>
    <table><thead><tr>
      <th>&#xC885;&#xBAA9;&#xCF54;&#xB4DC;</th><th>&#xC885;&#xBAA9;&#xBA85;</th><th>&#xC804;&#xB7B5;</th>
      <th class="r">&#xC2E0;&#xD638;&#xC77C;</th><th class="r">AI&#xC810;&#xC218;</th><th class="r">&#xBC18;&#xB3C4;&#xC288; &#xD655;&#xB960;</th>
    </tr></thead>
    <tbody id="ov-cand"><tr><td colspan="6" style="color:#8b949e;text-align:center">&#xB370;&#xC774;&#xD130; &#xB85C;&#xB529;&#xC911;...</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ===== 천억이 ===== -->
<div id="tab-cheonok" class="tab-content">
  <div class="section">
    <h2>&#xB204;&#xC801; &#xC131;&#xACFC; (trades_history_v3.csv)</h2>
    <div class="stats-row">
      <div class="stat"><div class="v" id="ch-trades">-</div><div class="l">&#xCD1D; &#xAC70;&#xB798;</div></div>
      <div class="stat"><div class="v" id="ch-wr">-</div><div class="l">&#xC2B9;&#xB960;</div></div>
      <div class="stat"><div class="v" id="ch-avg">-</div><div class="l">&#xD3C9;&#xADE0;&#xC218;&#xC775;%</div></div>
      <div class="stat"><div class="v neg" id="ch-mdd">-</div><div class="l">MDD</div></div>
      <div class="stat"><div class="v" id="ch-sharpe">-</div><div class="l">&#xC0E4;&#xD504;</div></div>
      <div class="stat"><div class="v pos" id="ch-cap">-</div><div class="l">&#xCD5C;&#xC885;&#xC790;&#xBCF8;</div></div>
    </div>
  </div>
  <div class="section">
    <h2>&#xD65C;&#xC131; &#xC2E0;&#xD638; (paper_signals.csv)</h2>
    <div id="ch-active-info" style="color:#8b949e;font-size:13px;margin-bottom:8px"></div>
    <table><thead><tr>
      <th>&#xC885;&#xBAA9;</th><th>&#xC804;&#xB7B5;</th><th class="r">&#xC2E0;&#xD638;&#xC77C;</th>
      <th class="r">&#xC9C4;&#xC785;&#xAC00;</th><th class="r">&#xBAA9;&#xD45C;&#xCCAD;&#xC0B0;&#xC77C;</th><th class="r">&#xD604;&#xC7AC;%</th>
    </tr></thead>
    <tbody id="ch-signals"><tr><td colspan="6" style="color:#8b949e;text-align:center">&#xD65C;&#xC131; &#xC2E0;&#xD638; &#xC5C6;&#xC74C;</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ===== 모의계좌 ===== -->
<div id="tab-mock" class="tab-content">
  <!-- Kiwoom -->
  <div class="section">
    <h2>키움 잔고</h2>
    <div class="stats-row" id="mock-bal"></div>
  </div>
  <div class="section">
    <h2>&#xD0A4;&#xC6C0; &#xBAA8;&#xC758;&#xD22C;&#xC790; &#xC2AC;&#xB86F;</h2>
    <div class="stats-row" id="mock-slots"></div>
  </div>
  <div class="section">
    <h2>&#xD0A4;&#xC6C0; &#xBCF4;&#xC720; &#xD3EC;&#xC9C0;&#xC158;</h2>
    <table><thead><tr>
      <th>&#xC885;&#xBAA9;</th><th>&#xC804;&#xB7B5;</th><th class="r">강도</th><th class="r">&#xC218;&#xB7C9;</th>
      <th class="r">&#xD3C9;&#xADE0;&#xB2E8;&#xAC00;</th><th class="r">&#xD604;&#xC7AC;&#xAC00;</th><th class="r">&#xD3C9;&#xAC00;&#xAE08;&#xC561;</th><th class="r">&#xC190;&#xC775;(&#xC6D0;)</th><th class="r">&#xC190;&#xC775;&#xB960;</th><th class="r">&#xC9C4;&#xC785;&#xC77C;</th>
    </tr></thead>
    <tbody id="mock-pos"><tr><td colspan="10" style="color:#8b949e;text-align:center">&#xBCF4;&#xC720; &#xD3EC;&#xC9C0;&#xC158; &#xC5C6;&#xC74C;</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>키움 매매내역 <span id="mock-ord-cnt" style="color:#8b949e;font-size:11px;font-weight:400"></span></h2>
    <table><thead><tr>
      <th class="r">시각</th><th>구분</th><th>종목</th><th>전략</th><th class="r">강도</th>
      <th class="r">수량</th><th class="r">가격</th><th class="r">손익(원)</th><th class="r">손익률</th><th>사유</th>
    </tr></thead>
    <tbody id="mock-orders"><tr><td colspan="9" style="color:#8b949e;text-align:center">매매내역 없음</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>키움 전략별 성과 <span style="color:#8b949e;font-size:11px;font-weight:400">&mdash; 실현(청산·FIFO 추정) + 미실현(보유) 분리 · 표본 작아 건수 병기</span></h2>
    <table><thead><tr>
      <th>전략</th><th class="r">청산</th><th class="r">승률</th><th class="r">평균%</th><th class="r">실현손익</th><th class="r">보유</th><th class="r">미실현손익</th>
    </tr></thead>
    <tbody id="mock-stratperf"><tr><td colspan="7" style="color:#8b949e;text-align:center">데이터 없음</td></tr></tbody>
    </table>
  </div>
  <!-- KIS -->
  <div class="section">
    <h2>KIS 잔고</h2>
    <div class="stats-row" id="kis-bal"></div>
  </div>
  <div class="section">
    <h2>KIS 손절 모니터 <span id="kis-stop-ts" style="color:#8b949e;font-size:11px;font-weight:400"></span></h2>
    <table><thead><tr>
      <th>종목</th><th class="r">현재손익</th><th class="r">손절선</th><th class="r">여유(%p)</th><th>상태</th>
    </tr></thead>
    <tbody id="kis-stop-rows"><tr><td colspan="5" style="color:#8b949e;text-align:center">장중 15분마다 갱신 (보유 없음/장외)</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>KIS &#xBAA8;&#xC758;&#xD22C;&#xC790; &#xC2AC;&#xB86F;</h2>
    <div class="stats-row" id="kis-slots"></div>
  </div>
  <div class="section">
    <h2>KIS &#xBCF4;&#xC720; &#xD3EC;&#xC9C0;&#xC158;</h2>
    <table><thead><tr>
      <th>&#xC885;&#xBAA9;</th><th>&#xC804;&#xB7B5;</th><th class="r">강도</th><th class="r">&#xC218;&#xB7C9;</th>
      <th class="r">&#xD3C9;&#xADE0;&#xB2E8;&#xAC00;</th><th class="r">&#xD604;&#xC7AC;&#xAC00;</th><th class="r">&#xD3C9;&#xAC00;&#xAE08;&#xC561;</th><th class="r">&#xC190;&#xC775;(&#xC6D0;)</th><th class="r">&#xC190;&#xC775;&#xB960;</th><th class="r">&#xC9C4;&#xC785;&#xC77C;</th>
    </tr></thead>
    <tbody id="kis-pos"><tr><td colspan="10" style="color:#8b949e;text-align:center">&#xBCF4;&#xC720; &#xD3EC;&#xC9C0;&#xC158; &#xC5C6;&#xC74C;</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>KIS 매매내역 <span id="kis-ord-cnt" style="color:#8b949e;font-size:11px;font-weight:400"></span></h2>
    <table><thead><tr>
      <th class="r">시각</th><th>구분</th><th>종목</th><th>전략</th><th class="r">강도</th>
      <th class="r">수량</th><th class="r">가격</th><th class="r">손익(원)</th><th class="r">손익률</th><th>사유</th>
    </tr></thead>
    <tbody id="kis-orders"><tr><td colspan="9" style="color:#8b949e;text-align:center">매매내역 없음</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>KIS 전략별 성과 <span style="color:#8b949e;font-size:11px;font-weight:400">&mdash; 실현(청산·FIFO 추정) + 미실현(보유) 분리 · 표본 작아 건수 병기</span></h2>
    <table><thead><tr>
      <th>전략</th><th class="r">청산</th><th class="r">승률</th><th class="r">평균%</th><th class="r">실현손익</th><th class="r">보유</th><th class="r">미실현손익</th>
    </tr></thead>
    <tbody id="kis-stratperf"><tr><td colspan="7" style="color:#8b949e;text-align:center">데이터 없음</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ===== 강도매매 ===== -->
<div id="tab-strength" class="tab-content">
  <div class="section">
    <h2>신호 강도 백데이터 <span style="color:#8b949e;font-size:12px;font-weight:400">&mdash; 실거래는 슬롯 균등분배(강도 무관). 여기는 "이 강도면 이렇게 매매했을 것"을 사후 검증하기 위한 누적 기록</span></h2>
    <div class="stats-row" id="st-summary"></div>
  </div>
  <div class="section">
    <h2>전략별 강도 요약</h2>
    <table><thead><tr>
      <th>전략</th><th class="r">기록수</th><th class="r">평균 강도(score_ic)</th><th class="r">평균 거래대금순위</th>
    </tr></thead>
    <tbody id="st-bystrat"><tr><td colspan="4" style="color:#8b949e;text-align:center">데이터 없음</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>강도 가상매매 검증 <span style="color:#8b949e;font-size:12px;font-weight:400">&mdash; 강도 구간별 '실현된' 성과(paper 실측과 조인, 완료 매매만). 실거래 아님 &middot; 키움 매수는 2026-07-02부터 강도 6 미만 스킵</span></h2>
    <div class="stats-row" id="st-virtual"><div style="color:#8b949e;font-size:13px">로딩중...</div></div>
  </div>
  <div class="section">
    <h2>AI 가상매매 검증 <span style="color:#8b949e;font-size:12px;font-weight:400">&mdash; AI확률(ai_prob, big-win 예측) 분위별 실현 성과. USE_AI=False(매매 미개입) &middot; 기록은 2026-06-30부터(차원버그 수정 후) &middot; 첫 완료 표본 ~07-07 예상</span></h2>
    <div class="stats-row" id="st-virtual-ai"><div style="color:#8b949e;font-size:13px">로딩중...</div></div>
  </div>
  <div class="section">
    <h2>가상매매 실행 — AI vs 강도 포트폴리오 <span id="ap-ts" style="color:#8b949e;font-size:12px;font-weight:400"></span></h2>
    <div style="color:#8b949e;font-size:12px;margin-bottom:8px">순수 장부 시뮬(브로커 미사용) &middot; 각 10슬롯 &middot; 시작 1,000만 &middot; 06-30~ &middot; 매일 18:31 재계산 &middot; 진입=신호일 종가 / 청산=만기 종가(익절·손절 없음 — 선별 효과만 분리)</div>
    <div class="stats-row" id="ap-stats"><div style="color:#8b949e;font-size:13px">로딩중...</div></div>
    <div style="color:#c9d1d9;font-size:13px;margin:12px 0 4px">보유 중</div>
    <table style="margin-top:2px"><thead><tr>
      <th>포트폴리오</th><th>종목</th><th>전략</th><th class="r">진입일</th><th class="r">점수</th><th class="r">평가손익률</th>
    </tr></thead>
    <tbody id="ap-pos"><tr><td colspan="6" style="color:#8b949e;text-align:center">보유 없음</td></tr></tbody>
    </table>
    <div style="color:#c9d1d9;font-size:13px;margin:16px 0 4px">전략별 성과 (완료 거래)</div>
    <div id="ap-bystrat" style="color:#8b949e;font-size:13px">데이터 없음</div>
    <div style="color:#c9d1d9;font-size:13px;margin:16px 0 4px">완료 매매내역 (클릭해서 펼치기)</div>
    <div id="ap-trades" style="color:#8b949e;font-size:13px">완료 거래 없음</div>
  </div>
  <div class="section">
    <h2>강도 기록 (최근 300건) <span style="color:#8b949e;font-size:11px;font-weight:400">&mdash; 열 제목 클릭 = 정렬(다시 클릭 = 반대방향)</span></h2>
    <div style="color:#8b949e;font-size:12px;margin-bottom:8px">강도(IC)=IC가중 통합 팩터점수(0~10, 높을수록 강함) · 거래대금순위=당일 전체순위(1=최대) · 슬롯순위=같은 전략 내 강도 우선순위(1=최강)</div>
    <table><thead><tr id="st-head">
      <th class="r sortable" onclick="sortSt('signal_date')">신호일</th><th class="sortable" onclick="sortSt('account')">계좌</th><th class="sortable" onclick="sortSt('strategy')">전략</th><th class="sortable" onclick="sortSt('name')">종목</th>
      <th class="r sortable" onclick="sortSt('entry_close')">진입가</th><th class="r sortable" onclick="sortSt('score_ic')">강도(IC)</th><th class="r sortable" onclick="sortSt('ai_prob')">AI확률</th><th class="r sortable" onclick="sortSt('score_tv')">거래대금순위</th><th class="r sortable" onclick="sortSt('slot_rank')">슬롯순위</th><th class="sortable" onclick="sortSt('market_strong')">시장</th>
    </tr></thead>
    <tbody id="st-rows"><tr><td colspan="10" style="color:#8b949e;text-align:center">강도 기록 없음 &mdash; 다음 신호생성(18:30) 때 첫 데이터 누적</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ===== 백테스트 ===== -->
<div id="tab-backtest" class="tab-content">
  <div class="section">
    <h2>&#xBC31;&#xD14C;&#xC2A4;&#xD2B8; &#xC694;&#xC57D; &mdash; <span id="bt-src" style="color:#58a6ff;font-weight:400"></span></h2>
    <div class="stats-row" id="bt-stats">
      <div style="color:#8b949e;font-size:13px">&#xB370;&#xC774;&#xD130; &#xB85C;&#xB529;&#xC911;...</div>
    </div>
  </div>
  <div class="section">
    <h2>&#xC804;&#xB7B5;&#xBCC4; &#xBE44;&#xAD50; (strategy_compare_*.csv)</h2>
    <table><thead><tr>
      <th>&#xC804;&#xB7B5;</th><th>&#xBAA8;&#xB4DC;</th><th class="r">&#xAC74;</th>
      <th class="r">&#xC2B9;&#xB960;</th><th class="r">&#xD3C9;&#xADE0;%</th>
      <th class="r">PF</th><th class="r">&#xC0E4;&#xD504;</th><th class="r">&#xD3C9;&#xADE0; &#xBCF4;&#xC720;</th><th class="r">&#xB370;&#xC774; &#xCC28;&#xC774;</th>
    </tr></thead>
    <tbody id="sc-rows"><tr><td colspan="9" style="color:#8b949e;text-align:center">strategy_engine.py &#xBA3C;&#xC800; &#xC2E4;&#xD589;&#xD558;&#xC138;&#xC694;</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>&#xAC70;&#xB798; &#xB0B4;&#xC5ED; (&#xCD5C;&#xC2E0; 500&#xAC74;)</h2>
    <table><thead><tr>
      <th>&#xC885;&#xBAA9;</th><th>&#xC804;&#xB7B5;</th><th class="r">&#xC9C4;&#xC785;&#xC77C;</th>
      <th class="r">&#xCCAD;&#xC0B0;&#xC77C;</th><th class="r">net%</th><th class="r">gross%</th>
    </tr></thead>
    <tbody id="bt-rows"><tr><td colspan="6" style="color:#8b949e;text-align:center">&#xB370;&#xC774;&#xD130; &#xB85C;&#xB529;&#xC911;...</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>전략 랩 — 백테스트 vs forward 실측 (갭 = forward − 백테스트 = 낙관편향 크기)</h2>
    <table><thead><tr>
      <th>전략</th><th class="r">BT수</th><th class="r">BT평균%</th>
      <th class="r">FWD수</th><th class="r">FWD평균%</th>
      <th class="r">갭%p</th><th class="r">슬롯수익%</th><th class="r">슬롯MDD%</th>
    </tr></thead>
    <tbody id="lab-rows"><tr><td colspan="8" style="color:#8b949e;text-align:center">strategy_lab.py &#xB204;&#xC801; &#xB300;&#xAE30; &mdash; &#xCD08;&#xAE30;&#xC5D4; &#xD45C;&#xBCF8; &#xC801;&#xC74C;</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ===== AI 모델 ===== -->
<div id="tab-aimodel" class="tab-content">
  <div id="ai-err-box" style="display:none" class="err-box"></div>
  <div class="section">
    <h2>&#xBAA8;&#xB378; &#xC815;&#xBCF4; (meta_model_v4)</h2>
    <div class="stats-row">
      <div class="stat"><div class="v" id="ai-date">-</div><div class="l">&#xB9C8;&#xC9C0;&#xB9C9; &#xD559;&#xC2B5;</div></div>
      <div class="stat"><div class="v" id="ai-age">-</div><div class="l">&#xACBD;&#xACFC;&#xC77C;</div></div>
      <div class="stat"><div class="v" id="ai-auc">-</div><div class="l">AUC</div></div>
      <div class="stat"><div class="v" id="ai-fc">-</div><div class="l">&#xD53C;&#xCC98; &#xC218;</div></div>
    </div>
  </div>
  <div class="section">
    <h2>모델 진화 <span style="color:#8b949e;font-size:12px;font-weight:400">&mdash; 학습마다 선택된 지표조합·성능 기록(최신순). 매주 AI 파이프라인이 한 줄씩 쌓음. 스프레드%p가 +3 이상 안정되면 사이징 연동 검토</span></h2>
    <table><thead><tr>
      <th class="r">학습일</th><th>엔진</th><th>선택 지표그룹</th><th class="r">피처</th>
      <th class="r">test AUC</th><th class="r">스프레드%p</th><th class="r">CV best</th><th class="r">train/test</th>
    </tr></thead>
    <tbody id="ai-reg-rows"><tr><td colspan="8" style="color:#8b949e;text-align:center">아직 기록 없음 &mdash; 다음 AI 학습 때부터 쌓입니다</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>IC &#xAC00;&#xC911;&#xCE58; (Spearman &#xC815;&#xBCF4;&#xACC4;&#xC218;)</h2>
    <table><thead><tr>
      <th>&#xD53C;&#xCC98;</th><th>&#xC124;&#xBA85;</th><th class="r">IC</th><th>&#xBC14;</th>
    </tr></thead>
    <tbody id="ai-ic"><tr><td colspan="4" style="color:#8b949e">&#xB85C;&#xB529;&#xC911;...</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>DB &#xD604;&#xD669; (stock.db)</h2>
    <div id="ai-db-info" style="font-size:13px;color:#8b949e"></div>
    <table style="margin-top:8px"><thead><tr>
      <th>&#xD14C;&#xC774;&#xBE14;</th><th class="r">&#xD589; &#xC218;</th><th class="r">&#xCD5C;&#xC2E0; &#xB0A0;&#xC9DC;</th>
    </tr></thead>
    <tbody id="ai-db-rows"><tr><td colspan="3" style="color:#8b949e">&#xB85C;&#xB529;&#xC911;...</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>&#xB274;&#xC2A4; &#xAC10;&#xC131; (7&#xC77C;)</h2>
    <div class="stats-row">
      <div class="stat"><div class="v pos" id="ai-news-pos">-</div><div class="l">&#xAE34;&#xC815;</div></div>
      <div class="stat"><div class="v neg" id="ai-news-neg">-</div><div class="l">&#xBD80;&#xC815;</div></div>
      <div class="stat"><div class="v" id="ai-news-neu">-</div><div class="l">&#xC911;&#xB9BD;</div></div>
      <div class="stat"><div class="v" id="ai-news-tot">-</div><div class="l">&#xD569;&#xACC4;</div></div>
    </div>
    <table style="margin-top:12px"><thead><tr>
      <th>&#xC885;&#xBAA9;</th><th>&#xC81C;&#xBAA9;</th><th>&#xAC10;&#xC131;</th><th class="r">&#xB0A0;&#xC9DC;</th><th>&#xC18C;&#xC2A4;</th>
    </tr></thead>
    <tbody id="ai-news-rows"></tbody>
    </table>
  </div>
</div>

<!-- ===== 데이터+로그 ===== -->
<div id="tab-datalogs" class="tab-content">
  <div class="section">
    <h2>&#xC218;&#xB3D9; &#xC2E4;&#xD589; (&#xC81C;&#xC5B4;&#xD310;)</h2>
    <div style="color:#8b949e;font-size:12px;margin-bottom:6px">정체된 데이터가 있으면 해당 버튼이 &#xF85149;빨갛게 깜빡입니다 &mdash; 그 버튼을 누르세요.</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
        <span style="color:#8b949e;font-size:12px;min-width:72px">데이터수집</span>
        <button class="runbtn" data-fresh="korea_stocks,usa_stocks,macro_indicators,supply_demand,news" onclick="runTask('backfill','전체 수집(backfill)')">전체 수집</button>
        <button class="runbtn" data-fresh="supply_demand" onclick="runTask('supply_demand','수급 수집')">수급</button>
        <button class="runbtn" data-fresh="macro_indicators" onclick="runTask('macro','거시지표 수집')">거시지표</button>
        <button class="runbtn" data-fresh="credit_balance" onclick="runTask('credit','신용/대차 수집')">신용/대차</button>
        <button class="runbtn" onclick="runTask('after_market','시간외 등락률 수집')">시간외</button>
        <button class="runbtn" onclick="runTask('recheck','데이터 재검증')">재검증</button>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
        <span style="color:#8b949e;font-size:12px;min-width:72px">신호·매매</span>
        <button class="runbtn" data-sig="1" onclick="runTask('live_signal','키움 신호감지')">키움 신호</button>
        <button class="runbtn" data-sig="1" onclick="runTask('kis_signal','KIS 신호감지')">KIS 신호</button>
        <button class="runbtn" onclick="runTask('kiwoom_buy','키움 매수')">키움 매수</button>
        <button class="runbtn" onclick="runTask('kiwoom_sell','키움 매도')">키움 매도</button>
        <button class="runbtn" onclick="runTask('kis_daily','KIS 매매(daily)')">KIS 매매</button>
        <button class="runbtn" onclick="runTask('kis_stopcheck','KIS 손절체크')">KIS 손절체크</button>
        <button class="runbtn" onclick="runTask('kiwoom_status','키움 잔고 새로고침',5000)">키움 잔고</button>
        <button class="runbtn" onclick="runTask('kis_status','KIS 잔고 새로고침',5000)">KIS 잔고</button>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
        <span style="color:#8b949e;font-size:12px;min-width:72px">AI·분석·장중</span>
        <button class="runbtn" onclick="runTask('ai_pipeline','AI 파이프라인(수집→학습)')">AI 파이프라인</button>
        <button class="runbtn" onclick="runTask('ai_dataset','AI 학습 데이터셋')">AI 데이터셋</button>
        <button class="runbtn" onclick="runTask('ai_train','AI 모델 학습')">AI 학습</button>
        <button class="runbtn" onclick="runTask('strategy_lab','전략 랩')">전략 랩</button>
        <button class="runbtn" onclick="runTask('paper_tracker','페이퍼 추적')">페이퍼 추적</button>
        <button class="runbtn" onclick="runTask('preview','장중 예비후보')">장중예비</button>
        <button class="runbtn" onclick="runTask('market_grid','장중시황 그리드')">시황그리드</button>
        <button class="runbtn" onclick="runTask('daily_summary','텔레그램 하루요약')">텔레그램요약</button>
        <button class="runbtn" onclick="runTask('watchdog','시스템 점검(워치독)')">시스템 점검</button>
        <button class="runbtn danger" onclick="runTask('scheduler_restart','스케줄러 재시작')">스케줄러 재시작</button>
      </div>
    </div>
    <div id="run-status" style="margin-top:10px;color:#8b949e;font-size:13px">&#xBC84;&#xD2BC;&#xC744; &#xB204;&#xB974;&#xBA74; &#xD574;&#xB2F9; &#xC791;&#xC5C5;&#xC774; &#xBC31;&#xADF8;&#xB77C;&#xC6B4;&#xB4DC;&#xB85C; &#xC2E4;&#xD589;&#xB429;&#xB2C8;&#xB2E4; (&#xB85C;&#xADF8;: logs/ &#xD3F4;&#xB354;).</div>
  </div>
  <div class="section">
    <details class="logdrop">
      <summary>수집 로그 (collect_*.log)</summary>
      <pre class="logbox" id="log-sch" style="margin-top:10px">로딩중...</pre>
    </details>
  </div>
  <div class="section">
    <details class="logdrop">
      <summary>페이퍼 로그 (paper_*.log)</summary>
      <pre class="logbox" id="log-live" style="margin-top:10px">로딩중...</pre>
    </details>
  </div>
  <div class="section">
    <h2>전체 로그 보기
      <select id="logfile-select" onchange="loadLogFile()" style="margin-left:8px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px 8px;font-size:12px"></select>
      <button class="runbtn" style="padding:4px 10px" onclick="refreshLogFiles()">목록 새로고침</button>
      <button class="runbtn" style="padding:4px 10px" onclick="loadLogFile()">불러오기</button>
      <span id="logfile-info" style="color:#8b949e;font-size:11px;margin-left:6px"></span>
    </h2>
    <pre class="logbox" id="logfile-content" style="max-height:600px">파일을 선택하세요.</pre>
  </div>
</div>

<!-- ===== 장중시황 ===== -->
<div id="tab-intraday" class="tab-content">
  <div class="section">
    <h2>&#xC7A5;&#xC911; &#xC2DC;&#xD669; &mdash; <span id="intra-ts" style="color:#8b949e;font-weight:400"></span>
      <button onclick="refreshIntraday()" style="margin-left:8px;font-size:11px;padding:2px 8px;
        background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;cursor:pointer">&#x21BB; &#xAC31;&#xC2E0;</button>
    </h2>
    <div id="intra-err" class="err-box" style="display:none"></div>
    <div class="stats-row" id="intra-stats"></div>
  </div>
  <div class="section">
    <h2>&#xC9C0;&#xC218; / &#xD658;&#xC728; / &#xC678;&#xAD6D;&#xC778; &#xC21C;&#xB9E4;&#xC218;</h2>
    <table><thead><tr>
      <th>&#xD56D;&#xBAA9;</th><th class="r">&#xD604;&#xC7AC;</th><th class="r">&#xC804;&#xC77C;&#xB300;&#xBE44;</th>
    </tr></thead>
    <tbody id="intra-idx"></tbody>
    </table>
  </div>
  <div class="section">
    <h2>장중 순위 (키움) <span id="grid-ts" style="color:#8b949e;font-size:11px;font-weight:400"></span></h2>
    <div id="market-grid">로딩중...</div>
  </div>
  <div class="section">
    <h2>장중 잠정 예비후보 <span id="prev-ts" style="color:#8b949e;font-size:11px;font-weight:400"></span>
      <span style="color:#d29922;font-size:11px;font-weight:400;margin-left:6px">※ 잠정(종가에 바뀔 수 있음)·정보용, 매매 아님</span>
    </h2>
    <table><thead><tr><th>전략</th><th>종목</th><th class="r">잠정가</th></tr></thead>
    <tbody id="prev-rows"><tr><td colspan="3" style="color:#8b949e;text-align:center">장중 매시간 갱신 (intraday_preview.py)</td></tr></tbody>
    </table>
  </div>
</div>

<script>
// ── helpers ──
function fmt(n){return Number(n||0).toLocaleString()}
function pct(n){const v=Number(n||0);return (v>=0?'+':'')+v.toFixed(2)+'%'}
function clr(n){return Number(n||0)>=0?'pos':'neg'}
function safe(v,fallback='-'){return (v===null||v===undefined||v==='')?fallback:v}
function fmtB(n){const v=Number(n||0);if(v>=1e8)return (v/1e8).toFixed(1)+'억';if(v>=1e4)return (v/1e4).toFixed(0)+'만';return fmt(v)}

function showTab(id){
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',['overview','cheonok','mock','strength','backtest','aimodel','datalogs','intraday'][i]===id));
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.toggle('active',t.id==='tab-'+id));
}

// ── fill helpers ──
function setTxt(id,val){const el=document.getElementById(id);if(el)el.textContent=val}
function setHtml(id,html){const el=document.getElementById(id);if(el)el.innerHTML=html}
function setCls(id,cls){const el=document.getElementById(id);if(el){el.className=el.className.replace(/pos|neg|neu/g,'').trim();el.classList.add(cls)}}

function fillOverview(ch, ai, cand){
  if(!ch)return;
  const cap=Number(ch.final_capital||0);
  setTxt('ov-cap', cap>=1e8?(cap/1e8).toFixed(2)+'억':fmt(cap));
  setTxt('ov-wr', safe(ch.win_rate)+' %');
  setTxt('ov-mdd', safe(ch.mdd)+' %');
  setTxt('ov-sharpe', safe(ch.sharpe));
  setTxt('ov-trades', fmt(ch.trade_count));
  setTxt('ov-active', safe(ch.active_count,'0'));
  setTxt('ov-dbsize', (ai&&ai.db&&ai.db.size_mb||'?')+'MB');
  setTxt('ov-model', ai&&ai.model&&ai.model.last_trained?ai.model.last_trained.slice(0,10):'-');
  setTxt('ov-news7', ai&&ai.news?safe(ai.news.week_total,'0'):'0');
  setTxt('ov-newsig', cand?safe(cand.new_signal_count,'0'):'0');
  // candidates table — get_candidates 는 new_signals 를 준다(과거 signals 아님).
  // 항목: {code,name,entry_price,target_exit,signal_date,ai_bigwin,ai_pos,ai_neg}
  const rows=(cand&&cand.new_signals||cand&&cand.signals||[]);
  setHtml('ov-cand', rows.length?rows.map(r=>
    `<tr><td>${r.code||''}</td><td>${r.name||''}</td><td style="color:#8b949e">${r.strategy||''}</td>
     <td class="r">${String(r.signal_date||'').slice(0,8)}</td>
     <td class="r">${fmt(r.entry_price)}</td>
     <td class="r ${clr(r.ai_bigwin)}">${r.ai_bigwin!=null?Number(r.ai_bigwin).toFixed(0)+'%':(r.prob!=null?(Number(r.prob)*100).toFixed(1)+'%':'-')}</td></tr>`
  ).join(''):'<tr><td colspan="6" style="color:#8b949e;text-align:center">&#xC624;&#xB298; &#xC2E0;&#xADDC; &#xD6C4;&#xBCF4; &#xC5C6;&#xC74C;</td></tr>');
}

function fillCheonok(ch){
  if(!ch)return;
  setTxt('ch-trades', fmt(ch.trade_count));
  setTxt('ch-wr', safe(ch.win_rate)+' %');
  setTxt('ch-avg', pct(ch.avg_return));
  setTxt('ch-mdd', safe(ch.mdd)+' %');
  setTxt('ch-sharpe', safe(ch.sharpe));
  const cap=Number(ch.final_capital||0);
  setTxt('ch-cap', cap>=1e8?(cap/1e8).toFixed(2)+'억':fmt(cap));
  setTxt('ch-active-info', `활성 신호 ${safe(ch.active_count,'0')}건`);
  const sigs=ch.signals||[];
  // 필드명은 백엔드 행 키(code/name/entry_price_close/est_pct)와 일치해야 함 —
  // 종전 stock_code/entry_price/return_pct 는 존재하지 않는 키라 표가 빈칸이었음(2026-07-12)
  setHtml('ch-signals', sigs.length?sigs.map(r=>
    `<tr><td>${r.code||''} ${r.name||''}</td><td style="color:#8b949e;font-size:11px">${r.strategy||''}</td>
     <td class="r">${String(r.signal_date||'').slice(0,8)}</td>
     <td class="r">${fmt(r.entry_price_close)}</td>
     <td class="r">${String(r.target_exit_date||'').slice(0,8)}</td>
     <td class="r ${clr(r.est_pct)}">${r.est_pct!=null?pct(r.est_pct):'-'}</td></tr>`
  ).join(''):'<tr><td colspan="6" style="color:#8b949e;text-align:center">만료된 신호만 있거나 없음</td></tr>');
}

function fillSlots(elId, slots){
  const el=document.getElementById(elId);if(!el)return;
  if(!slots||!slots.length){el.innerHTML='<div style="color:#8b949e;font-size:13px">슬롯 없음</div>';return;}
  el.innerHTML=slots.map(s=>{
    const used=s.used||0, mx=s.max||0;
    const dots='●'.repeat(Math.min(used,mx))+'○'.repeat(Math.max(0,mx-used));
    const stop=(s.stop_pct!==''&&s.stop_pct!=null)?`손절 ${s.stop_pct}%`:'손절 없음';
    return `<div class="slotitem">
      <div class="slothead" onclick="var b=this.parentNode.querySelector('.slotbody');b.classList.toggle('open');this.querySelector('.caret').textContent=b.classList.contains('open')?'▾':'▸'">
        <span class="slotname">${s.label||s.strategy||''} <span style="color:#8b949e;font-size:11px">${s.strategy||''}</span></span>
        <span style="font-size:13px"><b class="${used?'pos':'neu'}">${used}/${mx}</b> <span style="color:#8b949e;letter-spacing:1px">${dots}</span></span>
        <span class="caret">▸</span>
      </div>
      <div class="slotbody">
        <div><b style="color:#3fb950">진입</b> &nbsp;${s.entry||'-'}</div>
        <div style="margin-top:5px"><b style="color:#f85149">청산</b> &nbsp;${s.exit||'-'}</div>
        <div style="margin-top:6px;color:#8b949e">보유 ${s.holding||'?'}영업일 · ${stop} · 진입가=신호일 종가 / 실제 매수=다음 영업일 09시대</div>
      </div>
    </div>`;
  }).join('');
}

function fillBal(elId, snap){
  const el=document.getElementById(elId); if(!el) return;
  if(!snap || snap.deposit==null){ el.innerHTML='<div style="color:#8b949e;font-size:13px">스냅샷 없음</div>'; return; }
  const npos=(snap.positions||[]).length;
  let h='';
  // 총평가금액 = 예수금 + 보유평가 (앱 헤드라인과 동일). 있으면 맨 앞에.
  if(snap.total_eval!=null)
    h+=`<div class="stat"><div class="v">${fmt(snap.total_eval)}원</div><div class="l">총평가금액</div></div>`;
  h+=`<div class="stat"><div class="v" style="font-size:18px">${fmt(snap.deposit)}원</div><div class="l">예수금(정산완료)</div></div>`;
  if(snap.orderable!=null && snap.orderable!==snap.deposit)
    h+=`<div class="stat"><div class="v" style="font-size:18px">${fmt(snap.orderable)}원</div><div class="l">주문가능(D+2)</div></div>`;
  if(snap.eval_pnl!=null)
    h+=`<div class="stat"><div class="v ${clr(snap.eval_pnl)}" style="font-size:18px">${fmt(snap.eval_pnl)}원</div><div class="l">평가손익</div></div>`;
  h+=`<div class="stat"><div class="v">${npos}</div><div class="l">보유 종목</div></div>`;
  h+=`<div class="stat"><div class="v" style="font-size:14px">${snap.date||''} ${snap.time||''}</div><div class="l">스냅샷 기준</div></div>`;
  el.innerHTML=h;
}
function fillOrders(elId, cntId, orders, total){
  const tb=document.getElementById(elId); if(!tb) return;
  const rows=(orders||[]).slice(-50).reverse();   // 최신 50건만(최신순)
  const cnt=document.getElementById(cntId);
  if(cnt) cnt.textContent = total ? `(전체 ${fmt(total)}건 중 최근 ${rows.length})` : '';
  tb.innerHTML = rows.length ? rows.map(o=>{
    const sell=String(o.side||'').toLowerCase().includes('sell');   // 'sell' + 'tp_sell'(익절 지정가)
    const tp=String(o.side||'').toLowerCase()==='tp_sell';
    const d=String(o.date||'');
    const dstr=d.length>=8 ? d.slice(4,6)+'/'+d.slice(6,8)+' ' : '';
    const fail=(o.ok===false || o.ok==='False');
    const hasPnl = sell && !fail && o.pnl!=='' && o.pnl!=null;
    const pnlAmt = hasPnl ? `<span class="${clr(o.pnl)}">${fmt(o.pnl)}원</span>` : '';
    const pnlPct = hasPnl ? `<span class="${clr(o.pnl_pct)}">${pct(o.pnl_pct)}</span>` : '';
    return `<tr>
      <td class="r" style="font-size:11px">${dstr}${o.time||''}</td>
      <td class="${sell?'neg':'pos'}">${tp?'익절주문':(sell?'매도':'매수')}</td>
      <td>${o.name||''} <span style="color:#8b949e;font-size:11px">${o.code||''}</span></td>
      <td style="color:#8b949e;font-size:11px">${o.strategy||''}</td>
      <td class="r">${o.score_ic!=null?(o.score_ic>=6?'<b style="color:#3fb950">'+o.score_ic+'</b>':o.score_ic):''}</td>
      <td class="r">${fmt(o.qty)}</td>
      <td class="r">${fmt(o.price)}</td>
      <td class="r">${pnlAmt}</td>
      <td class="r">${pnlPct}</td>
      <td style="font-size:11px;color:#8b949e">${o.reason||''}${fail?' ✕실패':''}</td></tr>`;
  }).join('') : '<tr><td colspan="10" style="color:#8b949e;text-align:center">매매내역 없음</td></tr>';
}

function fillStopMonitor(elId, tsId, mon){
  const tb=document.getElementById(elId); if(!tb) return;
  const items=(mon&&mon.items)||[];
  const ts=document.getElementById(tsId);
  if(ts) ts.textContent = mon&&mon.updated ? '('+mon.updated+')' : '';
  if(!items.length){
    tb.innerHTML='<tr><td colspan="5" style="color:#8b949e;text-align:center">장중 15분마다 갱신 (보유 없음/장외)</td></tr>';
    return;
  }
  tb.innerHTML=items.map(x=>{
    const fired=x.room_pp<=0, imm=x.imminent;
    const badge=fired?'<span style="color:#f85149;font-weight:bold">● 손절선도달(마감청산)</span>'
              :imm?'<span style="color:#d29922;font-weight:bold">▲ 임박</span>'
              :'<span style="color:#3fb950">정상</span>';
    return `<tr>
      <td>${x.name||''} <span style="color:#8b949e;font-size:11px">${x.code||''}</span></td>
      <td class="r ${clr(x.pnl_pct)}">${(x.pnl_pct>=0?'+':'')+x.pnl_pct}%</td>
      <td class="r neg">${x.stop_pct}%</td>
      <td class="r ${x.room_pp<=3?'neg':'neu'}">${x.room_pp}</td>
      <td>${badge}</td></tr>`;
  }).join('');
}

function fillStratPerf(elId, perf){
  // 전략별 성과: 실현(청산) + 미실현(보유) 분리. 표본이 작아 건수 병기.
  const rows=perf||[];
  setHtml(elId, rows.length?rows.map(r=>`<tr>
    <td style="color:#8b949e;font-size:11px">${r.strategy||''}</td>
    <td class="r">${r.closed_n||0}</td>
    <td class="r">${r.win_pct!=null?r.win_pct+'%':'-'}</td>
    <td class="r ${clr(r.avg_pct)}">${r.avg_pct!=null?pct(r.avg_pct):'-'}</td>
    <td class="r ${clr(r.realized_pnl)}">${fmt(r.realized_pnl)}원</td>
    <td class="r">${r.open_n||0}</td>
    <td class="r ${clr(r.unreal_pnl)}">${fmt(r.unreal_pnl)}원</td></tr>`).join('')
    :'<tr><td colspan="7" style="color:#8b949e;text-align:center">완료·보유 거래 없음</td></tr>');
}

function fillMock(mock, kis){
  // Kiwoom
  if(mock&&!mock.error){
    fillBal('mock-bal', mock.snapshot);
    fillOrders('mock-orders','mock-ord-cnt', mock.orders, mock.order_total);
    fillSlots('mock-slots', mock.slot_status||[]);  // 백엔드 키는 slot_status (2026-06-30 수정)
    fillStratPerf('mock-stratperf', mock.strategy_perf);
    const pos=mock.positions||[];
    setHtml('mock-pos', pos.length?pos.map(r=>{
      const ev=(r.evlt_amt!=null&&r.evlt_amt!=='')?r.evlt_amt:(Number(r.qty)||0)*(Number(r.cur_price||r.current_price)||0);
      return `<tr><td>${r.code||''} ${r.name||''}</td><td style="color:#8b949e;font-size:11px">${r.strategy||''}</td>
       <td class="r">${r.score_ic!=null?(r.score_ic>=6?'<b style="color:#3fb950">'+r.score_ic+'</b>':r.score_ic):'-'}</td>
       <td class="r">${fmt(r.qty)}</td><td class="r">${fmt(r.avg_price)}</td>
       <td class="r">${fmt(r.cur_price||r.current_price)}</td>
       <td class="r">${fmt(ev)}원</td>
       <td class="r ${clr(r.pnl)}">${fmt(r.pnl)}원</td>
       <td class="r ${clr(r.pnl_pct)}">${pct(r.pnl_pct)}</td>
       <td class="r" style="font-size:11px">${String(r.entry_date||'').slice(0,10)}</td></tr>`;
    }).join(''):'<tr><td colspan="9" style="color:#8b949e;text-align:center">보유 포지션 없음</td></tr>');
  }
  // KIS
  if(kis&&!kis.error){
    fillBal('kis-bal', kis.snapshot);
    fillStopMonitor('kis-stop-rows','kis-stop-ts', kis.stop_monitor);
    fillOrders('kis-orders','kis-ord-cnt', kis.orders, kis.order_total);
    fillSlots('kis-slots', kis.slot_status||[]);  // 백엔드 키는 slot_status (2026-06-30 수정)
    fillStratPerf('kis-stratperf', kis.strategy_perf);
    const pos=kis.positions||[];
    setHtml('kis-pos', pos.length?pos.map(r=>{
      const ev=(r.evlt_amt!=null&&r.evlt_amt!=='')?r.evlt_amt:(Number(r.qty)||0)*(Number(r.cur_price||r.current_price)||0);
      return `<tr><td>${r.code||''} ${r.name||''}</td><td style="color:#8b949e;font-size:11px">${r.strategy||''}</td>
       <td class="r">${r.score_ic!=null?(r.score_ic>=6?'<b style="color:#3fb950">'+r.score_ic+'</b>':r.score_ic):'-'}</td>
       <td class="r">${fmt(r.qty)}</td><td class="r">${fmt(r.avg_price)}</td>
       <td class="r">${fmt(r.cur_price||r.current_price)}</td>
       <td class="r">${fmt(ev)}원</td>
       <td class="r ${clr(r.pnl)}">${fmt(r.pnl)}원</td>
       <td class="r ${clr(r.pnl_pct)}">${pct(r.pnl_pct)}</td>
       <td class="r" style="font-size:11px">${String(r.entry_date||'').slice(0,10)}</td></tr>`;
    }).join(''):'<tr><td colspan="9" style="color:#8b949e;text-align:center">보유 포지션 없음</td></tr>');
  }
}

function fillRunButtons(d){
  // 정체 데이터(freshness)·신호정체(health) 에 해당하는 수동버튼을 빨갛게(stale).
  const stale=new Set((d.freshness||[]).map(f=>f.table));
  const sigStale=(d.health||[]).some(h=>String(h.msg||'').indexOf('신호')>=0);
  document.querySelectorAll('.runbtn[data-fresh]').forEach(b=>{
    const tabs=(b.getAttribute('data-fresh')||'').split(',').filter(Boolean);
    b.classList.toggle('stale', tabs.some(t=>stale.has(t)));
  });
  document.querySelectorAll('.runbtn[data-sig]').forEach(b=>{
    b.classList.toggle('stale', sigStale);
  });
}

function fillStrength(st){
  if(!st){ return; }
  const s=st.summary||{};
  setHtml('st-summary', st.total ? `
    <div class="stat"><div class="v">${fmt(s.total)}</div><div class="l">총 기록 신호</div></div>
    <div class="stat"><div class="v">${safe(s.avg_score_ic,'-')}</div><div class="l">평균 강도(IC)</div></div>
    <div class="stat"><div class="v">${safe(s.scored_pct,'0')}%</div><div class="l">채점 성공률</div></div>
    <div class="stat"><div class="v">${s.strategies||0}</div><div class="l">전략 수</div></div>
    <div class="stat"><div class="v">${s.accounts||0}</div><div class="l">계좌 수</div></div>
    <div class="stat"><div class="v" style="font-size:14px">${s.last_date||'-'}</div><div class="l">최근 기록일</div></div>
  ` : '<div style="color:#8b949e;font-size:13px">강도 기록 없음 &mdash; 다음 신호생성(18:30) 때 첫 데이터가 누적됩니다.</div>');
  const bs=st.by_strategy||[];
  setHtml('st-bystrat', bs.length?bs.map(r=>
    `<tr><td>${r.strategy||''}</td><td class="r">${fmt(r.n)}</td>
     <td class="r">${safe(r.avg_ic,'-')}</td><td class="r">${safe(r.avg_tv,'-')}</td></tr>`
  ).join(''):'<tr><td colspan="4" style="color:#8b949e;text-align:center">데이터 없음</td></tr>');
  // 강도 가상매매 검증 패널 — 강도구간별 실현 성과(완료 매매만, 실거래 아님)
  const vt=st.virtual;
  setHtml('st-virtual', (vt&&vt.buckets&&vt.buckets.length)?
    vt.buckets.map(b=>`
      <div class="stat" style="min-width:150px">
        <div class="v ${clr(b.avg_net)}" style="font-size:18px">${pct(b.avg_net)}</div>
        <div class="l">강도 ${b.label} — ${fmt(b.n)}건 평균 · 승률 ${b.win}%</div>
      </div>`).join('')
    +`<div class="stat"><div class="v" style="font-size:14px">${fmt(vt.done_total)}건</div><div class="l">완료 매매(조인 표본)</div></div>`
    :'<div style="color:#8b949e;font-size:13px">아직 실현 표본 없음 — 신호가 보유기간을 마치고 paper_audit(일 09:00)이 돌면 누적됩니다.</div>');
  // AI 가상매매 검증 패널 — ai_prob 분위별 실현 성과(표본 차면 자동 표시)
  const va=st.virtual_ai;
  setHtml('st-virtual-ai', (va&&va.buckets&&va.buckets.length)?
    va.buckets.map(b=>`
      <div class="stat" style="min-width:170px">
        <div class="v ${clr(b.avg_net)}" style="font-size:18px">${pct(b.avg_net)}</div>
        <div class="l">AI확률 ${b.label} — ${fmt(b.n)}건 · 승률 ${b.win}%</div>
      </div>`).join('')
    +`<div class="stat"><div class="v" style="font-size:14px">${fmt(va.done_total)}건</div><div class="l">완료 매매(조인 표본)</div></div>`
    :`<div style="color:#8b949e;font-size:13px">${va?`AI확률 기록 ${fmt(va.logged_total)}건 누적 중 — 완료 매매 ${fmt(va.done_total)}건(9건 이상부터 분위 표시). ai_prob 는 06-30부터 기록되어 첫 만기(rsi 5일)가 ~07-07 도래.`:'AI확률 기록 없음'}</div>`);
  // 강도 기록 표 — 전역 보관 후 정렬 가능 렌더
  window._stRows = st.rows||[];
  window._stSort = window._stSort || {key:null, asc:false};
  renderStRows();
}

function fillAiPaper(ap){
  const el=document.getElementById('ap-stats'); if(!el) return;
  if(!ap||!ap.portfolios){ el.innerHTML='<div style="color:#8b949e;font-size:13px">첫 실행 대기 — 18:31 신호 생성 후 생성됩니다 (수동: 제어판 없이 python ai_paper_trader.py)</div>'; return; }
  const ts=document.getElementById('ap-ts'); if(ts) ts.textContent='('+(ap.updated||'')+' 갱신)';
  const P=ap.portfolios;
  el.innerHTML=['ai','strength'].map(k=>{
    const p=P[k]; if(!p) return '';
    const nm=k==='ai'?'🤖 AI(ai_prob·필터없음)':'💪 강도(score_ic≥6 — 라이브 룰)';
    return `<div class="stat" style="min-width:210px">
      <div class="v ${clr(p.ret_pct)}" style="font-size:19px">${fmt(p.equity)}원 <span style="font-size:13px">(${pct(p.ret_pct)})</span></div>
      <div class="l">${nm} — 보유 ${p.n_pos} · 완료 ${p.n_trades}건${p.win_pct!=null?' · 승률 '+p.win_pct+'%':''}${p.avg_net!=null?' · 평균 '+pct(p.avg_net):''}</div>
    </div>`;
  }).join('');
  const rows=[];
  ['ai','strength'].forEach(k=>{
    (P[k]&&P[k].positions||[]).slice(0,10).forEach(r=>rows.push({k,...r}));
  });
  setHtml('ap-pos', rows.length?rows.map(r=>`<tr>
    <td>${r.k==='ai'?'🤖 AI':'💪 강도'}</td>
    <td>${r.code||''} ${r.name||''}</td>
    <td style="color:#8b949e;font-size:11px">${r.strategy||''}</td>
    <td class="r" style="font-size:11px">${r.entry_date||''}</td>
    <td class="r">${r.score!=null?r.score:''}</td>
    <td class="r ${clr(r.pnl_pct)}">${pct(r.pnl_pct)}</td></tr>`).join('')
    :'<tr><td colspan="6" style="color:#8b949e;text-align:center">보유 없음</td></tr>');

  // 전략별 성과(완료 거래) — 포트폴리오별
  const bsHtml=['ai','strength'].map(k=>{
    const p=P[k]; const bs=(p&&p.by_strategy)||[];
    if(!bs.length) return '';
    const label=k==='ai'?'🤖 AI':'💪 강도';
    const tr=bs.map(s=>`<tr>
      <td style="color:#8b949e;font-size:11px">${s.strategy||''}</td>
      <td class="r">${s.n}</td>
      <td class="r">${s.win_pct!=null?s.win_pct+'%':'-'}</td>
      <td class="r ${clr(s.avg_net)}">${pct(s.avg_net)}</td>
      <td class="r ${clr(s.pnl)}">${fmt(s.pnl)}원</td></tr>`).join('');
    return `<div style="margin-bottom:10px"><div style="font-size:12px;margin-bottom:2px">${label}</div>
      <table><thead><tr><th>전략</th><th class="r">완료</th><th class="r">승률</th><th class="r">평균%</th><th class="r">실현손익</th></tr></thead>
      <tbody>${tr}</tbody></table></div>`;
  }).join('');
  setHtml('ap-bystrat', bsHtml||'완료 거래 없음');

  // 완료 매매내역 — 포트폴리오별 드롭다운(<details>)
  const trHtml=['ai','strength'].map(k=>{
    const p=P[k]; const tds=(p&&p.trades)||[];
    const label=k==='ai'?'🤖 AI':'💪 강도';
    if(!tds.length) return `<details style="margin-bottom:6px"><summary style="cursor:pointer;color:#c9d1d9">${label} — 완료 0건</summary></details>`;
    const body=tds.map(t=>`<tr>
      <td>${t.code||''} ${t.name||''}</td>
      <td style="color:#8b949e;font-size:11px">${t.strategy||''}</td>
      <td class="r" style="font-size:11px">${t.entry_date||''}</td>
      <td class="r" style="font-size:11px">${t.exit_date||''}</td>
      <td class="r">${t.score!=null?t.score:''}</td>
      <td class="r ${clr(t.net_pct)}">${pct(t.net_pct)}</td>
      <td class="r ${clr(t.pnl)}">${fmt(t.pnl)}원</td></tr>`).join('');
    return `<details style="margin-bottom:6px"><summary style="cursor:pointer;color:#c9d1d9">${label} — 완료 ${tds.length}건 (클릭)</summary>
      <table style="margin-top:6px"><thead><tr><th>종목</th><th>전략</th><th class="r">진입</th><th class="r">청산</th><th class="r">점수</th><th class="r">순수익%</th><th class="r">손익</th></tr></thead>
      <tbody>${body}</tbody></table></details>`;
  }).join('');
  setHtml('ap-trades', trHtml||'완료 거래 없음');
}

function renderStRows(){
  const rows=(window._stRows||[]).slice();
  const s=window._stSort||{};
  if(s.key){
    const numeric=['entry_close','score_ic','ai_prob','score_tv','slot_rank','signal_date'].includes(s.key);
    rows.sort((a,b)=>{
      let x=a[s.key], y=b[s.key];
      if(numeric){ x=Number(x)||(x===0?0:-Infinity); y=Number(y)||(y===0?0:-Infinity); }
      else { x=String(x||''); y=String(y||''); }
      return (x<y?-1:x>y?1:0)*(s.asc?1:-1);
    });
  }
  // 헤더 방향 화살표 표시
  document.querySelectorAll('#st-head th.sortable').forEach(th=>{
    th.textContent=th.textContent.replace(/ [▲▼]$/,'');
    const m=th.getAttribute('onclick')||'';
    if(s.key && m.includes(`'${s.key}'`)) th.textContent+=(s.asc?' ▲':' ▼');
  });
  setHtml('st-rows', rows.length?rows.map(r=>{
    const msv=String(r.market_strong);
    const ms=(msv==='True'||msv==='true')?'<span style="color:#3fb950">강세</span>'
            :(msv==='False'||msv==='false')?'<span style="color:#8b949e">약세</span>':'';
    const ic=Number(r.score_ic);
    const icCls=(!isNaN(ic)&&ic>=6)?'pos':'';
    return `<tr>
      <td class="r" style="font-size:11px">${String(r.signal_date||'')}</td>
      <td style="font-size:11px">${r.account||''}</td>
      <td style="color:#8b949e;font-size:11px">${r.strategy||''}</td>
      <td>${r.code||''} ${r.name||''}</td>
      <td class="r">${fmt(r.entry_close)}</td>
      <td class="r ${icCls}" style="font-weight:bold">${safe(r.score_ic,'-')}</td>
      <td class="r">${(r.ai_prob!=null&&r.ai_prob!=='')?(Number(r.ai_prob)*100).toFixed(1)+'%':'-'}</td>
      <td class="r">${safe(r.score_tv,'-')}</td>
      <td class="r">${safe(r.slot_rank,'-')}</td>
      <td>${ms}</td></tr>`;
  }).join(''):'<tr><td colspan="10" style="color:#8b949e;text-align:center">강도 기록 없음</td></tr>');
}

function sortSt(key){
  const s=window._stSort||{key:null,asc:false};
  if(s.key===key) s.asc=!s.asc;         // 같은 열 재클릭 = 방향 토글
  else { s.key=key; s.asc=false; }      // 새 열 = 내림차순부터
  window._stSort=s;
  renderStRows();
}

function fillBacktest(bt, sc){
  if(!bt){setHtml('bt-stats','<div class="err-box">백테스트 데이터 없음</div>');return;}
  if(bt.error){setHtml('bt-stats',`<div class="err-box">${bt.error}</div>`);return;}
  setTxt('bt-src', bt.source||'');
  // stats
  const s=bt.stats;
  if(s&&s.total){
    setHtml('bt-stats',`
      <div class="stat"><div class="v">${fmt(s.total)}</div><div class="l">거래</div></div>
      <div class="stat"><div class="v">${safe(s.win_rate)} %</div><div class="l">승률</div></div>
      <div class="stat"><div class="v ${clr(s.avg_return)}">${pct(s.avg_return)}</div><div class="l">평균%</div></div>
      <div class="stat"><div class="v neg">${safe(s.mdd)} %</div><div class="l">MDD</div></div>
      <div class="stat"><div class="v">${safe(s.sharpe)}</div><div class="l">샤프</div></div>`);
  } else {
    setHtml('bt-stats','<div style="color:#8b949e;font-size:13px">통계 없음 (net_pct 컬럼 필요)</div>');
  }
  // trade rows — exit_compare format vs individual trade format
  const trades=bt.trades||[];
  setHtml('bt-rows', trades.length?trades.slice(0,500).map(r=>{
    if(r.mode!==undefined){
      // exit_compare / strategy_compare 형식
      const da=Number(r.delta_avg||0);
      return `<tr>
        <td style="font-size:11px">${r.strategy||r.stock_code||''}</td>
        <td style="font-size:10px;color:#8b949e">${r.mode||''}</td>
        <td class="r">${String(r.entry_date||'').slice(0,10)}</td>
        <td class="r">${String(r.exit_date||'').slice(0,10)}</td>
        <td class="r ${clr(r.net_pct)}">${pct(r.net_pct)}</td>
        <td class="r ${clr(r.gross_pct)}">${pct(r.gross_pct)}</td></tr>`;
    }
    // 개별 trade (trades_AB_*.csv)
    return `<tr>
      <td>${r.stock_code||r.code||''}</td>
      <td style="font-size:11px;color:#8b949e">${r.strategy||''}</td>
      <td class="r">${String(r.entry_date||r.entry_time||'').slice(0,10)}</td>
      <td class="r">${String(r.exit_date||'').slice(0,10)}</td>
      <td class="r ${clr(r.net_pct)}">${pct(r.net_pct)}</td>
      <td class="r ${clr(r.gross_pct)}">${pct(r.gross_pct)}</td></tr>`;
  }).join(''):'<tr><td colspan="6" style="color:#8b949e;text-align:center">거래 내역 없음</td></tr>');
  // strategy compare (방어적 상한 — 비정상적으로 많이 와도 DOM 폭발 방지)
  const scRows=((sc&&sc.rows)||[]).slice(0,200);
  setHtml('sc-rows', scRows.length?scRows.map(r=>{
    const da=Number(r.delta_avg||0);
    return `<tr>
      <td style="font-size:11px">${r.strategy||''}</td>
      <td style="font-size:10px;color:#8b949e">${r.mode||''}</td>
      <td class="r">${r.n||r.trades||''}</td>
      <td class="r">${Number(r.win_pct||0).toFixed(1)}%</td>
      <td class="r ${clr(r.avg_net)}">${Number(r.avg_net||0).toFixed(2)}%</td>
      <td class="r">${Number(r.profit_factor||0).toFixed(2)}</td>
      <td class="r">${Number(r.sharpe||0).toFixed(2)}</td>
      <td class="r">${Number(r.avg_hold||0).toFixed(1)}d</td>
      <td class="r ${clr(da)}" style="font-weight:bold">${da>=0?'+':''}${da.toFixed(2)}%p</td></tr>`;
  }).join(''):'<tr><td colspan="9" style="color:#8b949e;text-align:center">strategy_engine.py 먼저 실행하세요</td></tr>');
}

function fillLab(lab){
  const rows=((lab&&lab.rows)||[]).slice(0,200);   // 방어적 상한(대량 반환 시 DOM 폭발 방지)
  setHtml('lab-rows', rows.length?rows.map(r=>
    `<tr>
      <td style="font-size:12px">${r.strategy||''}</td>
      <td class="r" style="color:#8b949e">${r.bt_n||0}</td>
      <td class="r ${clr(r.bt_avg_net)}">${Number(r.bt_avg_net||0).toFixed(2)}%</td>
      <td class="r">${r.fwd_n||0}</td>
      <td class="r ${clr(r.fwd_avg_net)}">${r.fwd_n?Number(r.fwd_avg_net||0).toFixed(2)+'%':'-'}</td>
      <td class="r ${clr(r.gap_avg_net)}" style="font-weight:bold">${r.gap_avg_net!=null?((r.gap_avg_net>=0?'+':'')+Number(r.gap_avg_net).toFixed(2)+'%p'):'-'}</td>
      <td class="r ${clr(r.slot_total_ret)}">${Number(r.slot_total_ret||0).toFixed(1)}%</td>
      <td class="r neg">${Number(r.slot_mdd||0).toFixed(1)}%</td></tr>`
  ).join(''):'<tr><td colspan="8" style="color:#8b949e;text-align:center">strategy_lab.py 실행 대기 (BT는 즉시, FWD/갭은 누적)</td></tr>');
}

function fillAIRegistry(reg){
  const rows=(reg&&reg.rows)||[];
  setHtml('ai-reg-rows', rows.length?rows.map(r=>{
    const grp=String(r.groups||'').replace(/\|/g,', ');
    const sp=r.spread_pp;
    const spc=(sp!==''&&sp!=null)?(Number(sp)>=3?'pos':(Number(sp)<0?'neg':'')):'';
    return `<tr>
      <td class="r" style="font-size:11px">${r.run_date||''}</td>
      <td style="font-size:11px">${r.engine||''}</td>
      <td style="font-size:11px">${grp||'-'}</td>
      <td class="r">${r.n_features||''}</td>
      <td class="r">${(r.test_auc!==''&&r.test_auc!=null)?Number(r.test_auc).toFixed(3):'-'}</td>
      <td class="r ${spc}">${(sp!==''&&sp!=null)?((Number(sp)>=0?'+':'')+Number(sp).toFixed(2)):'-'}</td>
      <td class="r">${(r.cv_best!==''&&r.cv_best!=null)?Number(r.cv_best).toFixed(2):'-'}</td>
      <td class="r" style="font-size:11px">${fmt(r.train_n)}/${fmt(r.test_n)}</td></tr>`;
  }).join(''):'<tr><td colspan="8" style="color:#8b949e;text-align:center">아직 기록 없음 &mdash; 다음 AI 학습 때부터 쌓입니다</td></tr>');
}

function fillAI(ai){
  const errBox=document.getElementById('ai-err-box');
  if(!ai){errBox.style.display='block';errBox.textContent='AI 데이터 없음';return;}
  if(ai.error){errBox.style.display='block';errBox.textContent='API 오류: '+ai.error;return;}
  errBox.style.display='none';
  // model info
  const m=ai.model||{};
  setTxt('ai-date', safe(m.last_trained,'-').slice(0,16));
  setTxt('ai-age', m.age_days!=null?m.age_days+'일':'-');
  setTxt('ai-auc', safe(m.auc,'-'));
  setTxt('ai-fc', safe(m.feature_count,'-'));
  // IC weights
  const ic=ai.ic||{};
  if(ic.weights&&ic.weights.length){
    setHtml('ai-ic', ic.weights.map(w=>{
      const bar=Math.round(Math.abs(w.ic)*100);
      const barHtml=`<div style="display:inline-block;width:${bar}px;height:8px;background:${w.ic>=0?'#3fb950':'#f85149'};border-radius:2px"></div>`;
      return `<tr><td style="font-family:monospace;font-size:12px">${w.feat}</td>
        <td style="color:#8b949e;font-size:11px">${w.label||''}</td>
        <td class="r ${clr(w.ic)}">${w.ic>=0?'+':''}${w.ic}</td>
        <td>${barHtml}</td></tr>`;
    }).join(''));
  } else {
    setHtml('ai-ic',`<tr><td colspan="4" style="color:#f85149">${ic.error||'IC 가중치 없음 — scipy 설치 필요: pip install scipy'}</td></tr>`);
  }
  // DB info
  const db=ai.db||{};
  setTxt('ai-db-info', db.size_mb?`stock.db: ${db.size_mb} MB`:(db.error||'DB 없음'));
  const dbTables=['korea_stocks','usa_stocks','supply_demand','macro_indicators','credit_balance','news'];
  setHtml('ai-db-rows', dbTables.map(t=>{
    const info=db[t]||{};
    return `<tr><td>${t}</td><td class="r">${fmt(info.count)}</td><td class="r" style="font-size:11px;color:#8b949e">${safe(info.latest,'-')}</td></tr>`;
  }).join(''));
  // news
  const news=ai.news||{};
  const sent=news.sentiment||{};
  setTxt('ai-news-pos', safe(sent.positive,'0'));
  setTxt('ai-news-neg', safe(sent.negative,'0'));
  setTxt('ai-news-neu', safe(sent.neutral,'0'));
  setTxt('ai-news-tot', safe(news.week_total,'0'));
  setHtml('ai-news-rows', (news.recent||[]).map(r=>{
    const sc=r.sentiment||'';
    const cls=sc==='positive'?'pos':sc==='negative'?'neg':'neu';
    return `<tr><td style="font-size:11px">${r.ticker||''}</td>
      <td style="font-size:12px">${r.title||''}</td>
      <td class="${cls}" style="font-size:11px">${sc}</td>
      <td class="r" style="font-size:11px;color:#8b949e">${r.date||''}</td>
      <td style="font-size:11px;color:#8b949e">${r.source||''}</td></tr>`;
  }).join(''));
}

// ── 로그 파일 뷰어 ──
function refreshLogFiles(keepSel){
  return fetch('/api/logfiles').then(r=>r.json()).then(d=>{
    const sel=document.getElementById('logfile-select'); if(!sel) return d;
    const cur=keepSel||sel.value;
    sel.innerHTML=(d.files||[]).map(f=>`<option value="${f.name}">${f.name} (${f.kb}KB · ${f.mtime})</option>`).join('');
    if(cur && [...sel.options].some(o=>o.value===cur)) sel.value=cur;
    return d;
  }).catch(()=>{});
}
function loadLogFile(){
  const sel=document.getElementById('logfile-select'); if(!sel||!sel.value) return;
  const el=document.getElementById('logfile-content'), info=document.getElementById('logfile-info');
  if(el) el.textContent='불러오는 중...';
  fetch('/api/logfile/'+encodeURIComponent(sel.value)).then(r=>r.json()).then(d=>{
    if(el){ el.textContent=d.content||('(빈 파일)'+(d.error?' '+d.error:'')); el.scrollTop=el.scrollHeight; }
    if(info) info.textContent=d.kb?d.kb+'KB':'';
  }).catch(e=>{ if(el) el.textContent='로드 실패: '+e.message; });
}

function runTask(task, label, reloadMs){
  if(!confirm(label+' 실행할까요?')) return;
  const el=document.getElementById('run-status');
  if(el) el.textContent=label+' 시작 요청 중...';
  fetch('/api/run/'+task,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(el) el.textContent='['+label+'] '+(d.msg||(d.ok?'시작됨':'실패'))+'  ('+new Date().toLocaleTimeString()+')';
    // 수동실행 직후 해당 run_<task>.log 를 로그 뷰어에 자동 표시(2초 뒤 — 로그가 생길 시간).
    if(d.ok){ setTimeout(()=>{ refreshLogFiles('run_'+task+'.log').then(()=>loadLogFile()); }, 2000); }
    if(reloadMs && d.ok){ if(el) el.textContent+=' — '+(reloadMs/1000)+'초 후 갱신'; setTimeout(()=>load(), reloadMs); }
  }).catch(e=>{ if(el) el.textContent='['+label+'] 오류: '+e.message; });
}

function fillLogs(logs, files){
  // logs — tail_log already returns a string
  const schEl=document.getElementById('log-sch');
  const liveEl=document.getElementById('log-live');
  if(schEl) schEl.textContent=(logs&&logs.sch)||'(수집 로그 없음)';
  if(liveEl) liveEl.textContent=(logs&&logs.live)||'(페이퍼 로그 없음)';
  // files
  setHtml('dl-files', (files||[]).map(f=>{
    const ok=f.exists===true||f.exists===undefined;
    const cls=ok?'file-ok':'file-err';
    return `<tr>
      <td style="font-family:monospace;font-size:12px">${f.name||''}</td>
      <td class="r" style="color:#8b949e">${f.size_kb||f.size_mb||'?'}${f.size_mb?'MB':'KB'}</td>
      <td style="font-size:11px;color:#8b949e">${f.mtime||''}</td>
      <td class="${cls}" style="font-size:12px">${ok?'✓':'✗'}</td></tr>`;
  }).join('')||'<tr><td colspan="4" style="color:#8b949e">파일 목록 없음</td></tr>');
}

async function refreshIntraday(){
  try{
    const d=await fetch('/api/intraday/refresh',{method:'POST'}).then(r=>r.json());
    fillIntraday(d);
  }catch(e){
    const eb=document.getElementById('intra-err');
    if(eb){eb.style.display='block';eb.textContent='갱신 실패: '+e.message;}
  }
}

function fillIntraday(d){
  const errEl=document.getElementById('intra-err');
  if(!d||d.error){
    if(errEl){errEl.style.display='block';errEl.textContent=(d&&d.error)||'장중 데이터 없음 (intraday_monitor.py 실행 필요)';}
    return;
  }
  if(errEl)errEl.style.display='none';
  setTxt('intra-ts', d.updated_at||'');
  const data=d.data||{};
  // stats
  const statsRows=[
    {l:'KOSPI', v:data.kospi_price, c:data.kospi_chg_pct},
    {l:'KOSDAQ', v:data.kosdaq_price, c:data.kosdaq_chg_pct},
    {l:'USD/KRW', v:data.usdkrw, c:data.usdkrw_chg},
  ];
  setHtml('intra-stats', statsRows.map(r=>
    `<div class="stat"><div class="v ${clr(r.c)}">${fmt(r.v)}</div><div class="l">${r.l} (${r.c!=null?(Number(r.c)>=0?'+':'')+Number(r.c).toFixed(2)+'%':'-'})</div></div>`
  ).join(''));
  // index table
  const idxFields=[
    ['KOSPI', data.kospi_price, data.kospi_chg_pct],
    ['KOSDAQ', data.kosdaq_price, data.kosdaq_chg_pct],
    ['USD/KRW', data.usdkrw, data.usdkrw_chg],
    ['외국인 순매수', data.for_net_buy, null],
    ['기관 순매수', data.inst_net_buy, null],
  ];
  setHtml('intra-idx', idxFields.map(([l,v,c])=>
    `<tr><td>${l}</td><td class="r">${fmt(v)}</td>
     <td class="r ${c!=null?clr(c):'neu'}">${c!=null?(Number(c)>=0?'+':'')+Number(c).toFixed(2)+'%':'-'}</td></tr>`
  ).join(''));
}

// ── main load ──
let _lastData=null;
async function load(){
  const ts=document.getElementById('ts');
  if(ts)ts.textContent='갱신 중...';
  let d=null;
  try{ d=await fetch('/api/all').then(r=>r.json()); }
  catch(e){ if(ts)ts.textContent='로드 실패: '+e.message; return; }
  _lastData=d;
  if(ts)ts.textContent=d.ts||new Date().toLocaleString();
  // freshness banner
  const fb=document.getElementById('freshness-banner');
  if(fb){
    const hs=(d.health||[]);
    if(hs.length){
      const hasErr=hs.some(h=>h.sev==='err');
      fb.innerHTML='<div class="'+(hasErr?'fresh-err':'fresh-warn')+'">⚠ 점검 필요: '
        +hs.map(h=>h.msg).join(' &nbsp;|&nbsp; ')+'</div>';
    } else {
      fb.innerHTML='<div class="fresh-ok">✓ 시스템 정상 (테이블·신호 이상 없음)</div>';
    }
  }
  // 패널별 렌더 격리 — 한 패널이 터져도 나머지는 그려진다(백엔드 _safe 와 대칭).
  const _r=(fn,...a)=>{try{fn(...a);}catch(e){console.error('[dashboard] render 오류 ('+fn.name+'):',e);}};
  _r(fillOverview, d.cheonok, d.ai, d.candidates);
  _r(fillCheonok, d.cheonok);
  _r(fillMock, d.mock, d.kis);
  _r(fillStrength, d.strength);
  _r(fillAiPaper, d.ai_paper);
  _r(fillRunButtons, d);
  _r(fillBacktest, d.bt, d.sc);
  _r(fillLab, d.lab);
  _r(fillAI, d.ai);
  _r(fillAIRegistry, d.ai_registry);
  _r(fillLogs, d.logs, d.files);
  // intraday — 별도 API
  fetch('/api/intraday').then(r=>r.json()).then(fillIntraday).catch(()=>{});
  fetch('/api/preview').then(r=>r.json()).then(fillPreview).catch(()=>{});
  fetch('/api/marketgrid').then(r=>r.json()).then(fillMarketGrid).catch(()=>{});
}

function _pctS(v){v=Number(v||0);return '<span class="'+(v>=0?'pos':'neg')+'">'+(v>=0?'+':'')+v.toFixed(2)+'%</span>';}
function _amtS(v){v=Number(v||0);return '<span class="'+(v>=0?'pos':'neg')+'">'+(v>=0?'+':'')+v.toLocaleString()+'</span>';}
// [지표키, 제목, 값포맷] — 6개
const _GRID=[
  ['volume','거래량', r=>_pctS(r.change_pct)],
  ['change','상승률', r=>_pctS(r.change_pct)],
  ['netbuy','기관+외인 순매수(천만)', r=>_amtS(r.net)],
  ['netsell','기관+외인 순매도(천만)', r=>_amtS(r.net)],
  ['new_high','신고가', r=>_pctS(r.change_pct)],
  ['sector','업종등락', r=>_pctS(r.change_pct)],
];
function _gridCard(title, rows, valf){
  // 20위까지 담되, 칸 안쪽만 고정높이 스크롤(틀 크기는 5위 표시 때와 동일 유지).
  const inner=(rows&&rows.length)
    ? rows.slice(0,20).map((r,i)=>`<div style="display:flex;justify-content:space-between;gap:6px;font-size:12px;padding:2px 0">
        <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${i+1}. ${r.name||r.code||''}</span>
        <span style="white-space:nowrap">${valf(r)}</span></div>`).join('')
    : '<div style="color:#8b949e;font-size:11px">-</div>';
  return `<div style="flex:1;min-width:150px;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:8px">
    <div style="font-size:11px;color:#8b949e;margin-bottom:4px;font-weight:600">${title}</div>
    <div style="max-height:96px;overflow-y:auto">${inner}</div></div>`;
}
function fillMarketGrid(d){
  const el=document.getElementById('market-grid'); if(!el) return;
  const ts=document.getElementById('grid-ts'); if(ts) ts.textContent=d&&d.updated?'('+d.updated+')':'';
  const mk=(d&&d.markets)||{};
  if(!Object.keys(mk).length){ el.innerHTML='<div style="color:#8b949e;font-size:13px">장중 갱신 대기 (market_grid.py)</div>'; return; }
  let html='';
  for(const [key,label] of [['kospi','코스피'],['kosdaq','코스닥']]){
    const m=mk[key]||{};
    html+=`<div style="font-size:12px;color:#58a6ff;font-weight:600;margin:8px 0 4px">${label}${m.error?' <span style="color:#f85149">'+m.error+'</span>':''}</div>`;
    html+='<div style="display:flex;flex-wrap:wrap;gap:8px">'+
      _GRID.map(([k,t,vf])=>_gridCard(t, m[k], vf)).join('')+'</div>';
  }
  el.innerHTML=html;
}

const _STRAT_KR={high_52w_filt:'52주신고가',rsi_reversal:'RSI반전',rsi_vol:'RSI+거래량'};
function fillPreview(d){
  const tb=document.getElementById('prev-rows'); if(!tb) return;
  const ts=document.getElementById('prev-ts');
  if(ts) ts.textContent=d&&d.updated?'('+d.updated+' 기준)':'';
  const items=(d&&d.items)||[];
  if(!items.length){
    tb.innerHTML='<tr><td colspan="3" style="color:#8b949e;text-align:center">잠정 후보 없음 (장중 갱신)</td></tr>';
    return;
  }
  tb.innerHTML=items.slice(0,40).map(x=>
    `<tr><td style="font-size:11px;color:#8b949e">${_STRAT_KR[x.strategy]||x.strategy}</td>
     <td>${x.name||''} <span style="color:#8b949e;font-size:11px">${x.code||''}</span></td>
     <td class="r">${fmt(x.price)}</td></tr>`
  ).join('');
}

// 탭이 보일 때만 새로고침(백그라운드/최소화 시 불필요한 서버호출·렌더 중단).
window.addEventListener('load',()=>{
  load();
  refreshLogFiles();   // 로그 파일 목록 채우기
  setInterval(()=>{ if(document.visibilityState==='visible') load(); }, 300_000);
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    from flask import Response
    return Response(HTML, mimetype="text/html; charset=utf-8")

if __name__ == "__main__":
    import threading, webbrowser
    url = f"http://localhost:{PORT}"
    print(f"[Dashboard] {url}")
    print("[Dashboard] Press Ctrl+C to stop")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
