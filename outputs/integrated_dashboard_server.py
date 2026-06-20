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


# ── 슬롯 구성 (kiwoom_trader.py 와 동기화) ─────────────────────────────────────
_STRATEGY_MAX_SLOTS = {"high_52w_filt": 4, "rsi_reversal": 4, "rsi_vol": 2}
_STRATEGY_PRIORITY  = ["high_52w_filt", "rsi_reversal", "rsi_vol"]


def _slot_status(paper_df):
    """paper_signals.csv 에서 전략별 슬롯 사용 현황 계산.

    보유 중(target_exit_date >= today) 신호 기준으로 슬롯 카운트.
    target_exit_date 는 int64 (20260619 형식) — 정수 비교 사용.
    """
    today_int = int(datetime.today().strftime("%Y%m%d"))  # 20260619 형식
    if "strategy" not in paper_df.columns:
        paper_df = paper_df.copy()
        paper_df["strategy"] = "high_52w_filt"

    if "target_exit_date" in paper_df.columns:
        active = paper_df[paper_df["target_exit_date"] >= today_int]
    else:
        active = paper_df

    used = {k: 0 for k in _STRATEGY_MAX_SLOTS}
    legacy_used = 0
    for strat, grp in active.groupby("strategy"):
        if strat in used:
            used[strat] = int(len(grp))
        else:
            legacy_used += int(len(grp))  # for_high20_mkt 등 레거시 보유

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
    files = sorted(RESULTS_DIR.glob("strategy_compare_*.csv"))
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

# ── 4. kiwoom mock account ─────────────────────────────────────────────────────
def get_kiwoom_mock(date_from="", date_to=""):
    out = {"snapshot": {}, "equity": [], "orders": [], "order_total": 0}
    snap_path = KIWOOM_DIR / "snapshot.json"
    if snap_path.exists():
        try:
            out["snapshot"] = json.loads(snap_path.read_text(encoding="utf-8"))
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
                df = pd.read_csv(f, dtype={"code": str})
                df["date"] = date_str
                frames.append(df)
            except Exception:
                pass
        if frames:
            combined = pd.concat(frames, ignore_index=True).fillna("")
            out["order_total"] = int(len(combined))
            out["orders"] = combined.tail(500).to_dict("records")
    return out


# ── 4b. KIS mock account (안D 포트폴리오) ─────────────────────────────────────
_KIS_STRATEGY_SLOTS = {"h52w_for3d_mkt": 4, "for_high20_mkt": 4, "gc_for3d": 2}
_KIS_STRATEGY_STOP  = {"h52w_for3d_mkt": -15, "for_high20_mkt": -10, "gc_for3d": -26}
_KIS_STRATEGY_LABEL = {
    "h52w_for3d_mkt": "52주신고가+외국인",
    "for_high20_mkt": "20일신고가+외국인",
    "gc_for3d":       "골든크로스+외국인",
}

def _kis_slot_status():
    from datetime import date as _date
    today_str = _date.today().strftime("%Y-%m-%d")
    active_by_strat = {k: [] for k in _KIS_STRATEGY_SLOTS}
    if KIS_SIGNALS_CSV.exists():
        try:
            df = pd.read_csv(KIS_SIGNALS_CSV, dtype={"code": str})
            df["code"] = df["code"].str.zfill(6)
            df["target_exit_date"] = df["target_exit_date"].astype(str)
            active = df[df["target_exit_date"] >= today_str]
            for strat in _KIS_STRATEGY_SLOTS:
                rows = active[active["strategy"] == strat]
                active_by_strat[strat] = rows["code"].tolist()
        except Exception:
            pass
    slots = []
    for strat in ["h52w_for3d_mkt", "for_high20_mkt", "gc_for3d"]:
        mx = _KIS_STRATEGY_SLOTS[strat]
        codes = active_by_strat.get(strat, [])
        slots.append({
            "strategy": strat,
            "label": _KIS_STRATEGY_LABEL[strat],
            "used": len(codes),
            "max": mx,
            "stop_pct": _KIS_STRATEGY_STOP[strat],
        })
    return slots

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
    # 진입가 추적 (stop-loss 모니터링)
    pos_path = KIS_DIR / "kis_positions.csv"
    if pos_path.exists():
        try:
            pos_df = pd.read_csv(pos_path, dtype={"code": str})
            pos_df["code"] = pos_df["code"].str.zfill(6)
            out["positions"] = pos_df.fillna("").to_dict("records")
        except Exception:
            pass
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
                df = pd.read_csv(f, dtype={"code": str})
                df["date"] = date_str
                frames.append(df)
            except Exception:
                pass
        if frames:
            combined = pd.concat(frames, ignore_index=True).fillna("")
            out["order_total"] = int(len(combined))
            out["orders"] = combined.tail(500).to_dict("records")
    # 슬롯 현황 + 신호 수
    out["slot_status"] = _kis_slot_status()
    if KIS_SIGNALS_CSV.exists():
        try:
            out["signals_total"] = int(len(pd.read_csv(KIS_SIGNALS_CSV)))
        except Exception:
            pass
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


# ── 6. AI project status ───────────────────────────────────────────────────────
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
        from factor_scorer import FactorScorer, FEATURE_META
        scorer = FactorScorer()
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
        df = pd.read_csv(HISTORY_CSV)
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
        feat_vecs.append([fv.get(f, np.nan) for f in _AI_FEAT_ORDER])
        code_list.append((code, fv))

    if not feat_vecs:
        return {}

    try:
        arr = np.array(feat_vecs, dtype=float)
        if _use_xgb:
            probs = model.predict(xgb.DMatrix(arr, feature_names=_AI_FEAT_ORDER))
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
            "cheonok":    _safe(get_cheonok),
            "bt":         _safe(get_backtest_trades, df, dt),
            "sc":         _safe(get_strategy_compare),
            "mock":       _safe(get_kiwoom_mock, df, dt),
            "kis":        _safe(get_kis_mock, df, dt),
            "pvbt":       _safe(get_paper_vs_bt),
            "ai":         _safe(get_ai_status),
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

_RUN_TASKS = {
    "live_signal":   [_VENV_PYTHON, str(BASE / "live_signal.py")],
    "paper_tracker": [_VENV_PYTHON, str(BASE / "paper_tracker.py")],
    "kiwoom_buy":    [_VENV_PYTHON, str(BASE / "kiwoom_trader.py"), "buy"],
    "kiwoom_sell":   [_VENV_PYTHON, str(BASE / "kiwoom_trader.py"), "sell"],
    "kiwoom_status": [_VENV_PYTHON, str(BASE / "kiwoom_trader.py"), "status"],
    "kis_signal":    [_VENV_PYTHON, str(BASE / "kis_live_signal.py")],
    "kis_daily":     [_VENV_PYTHON, str(BASE / "kis_trader.py"), "daily"],
    "kis_status":    [_VENV_PYTHON, str(BASE / "kis_trader.py"), "status"],
    "recheck":       [_VENV_PYTHON, str(BASE / "recheck_collector.py")],
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
        proc = _sp.Popen(
            _RUN_TASKS[task], cwd=str(BASE),
            stdout=_sp.PIPE, stderr=_sp.STDOUT,
            creationflags=_NO_WIN,
        )
        return jsonify({"ok": True, "msg": f"시작됨 (PID {proc.pid})"})
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
.fresh-ok{color:#3fb950}.fresh-warn{color:#d29922}
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
tr:hover td{background:#161b22}
.r{text-align:right}
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
    <h2>&#xD0A4;&#xC6C0; &#xBAA8;&#xC758;&#xD22C;&#xC790; &#xC2AC;&#xB86F;</h2>
    <div class="stats-row" id="mock-slots"></div>
  </div>
  <div class="section">
    <h2>&#xD0A4;&#xC6C0; &#xBCF4;&#xC720; &#xD3EC;&#xC9C0;&#xC158;</h2>
    <table><thead><tr>
      <th>&#xC885;&#xBAA9;</th><th>&#xC804;&#xB7B5;</th><th class="r">&#xC218;&#xB7C9;</th>
      <th class="r">&#xD3C9;&#xADE0;&#xB2E8;&#xAC00;</th><th class="r">&#xD604;&#xC7AC;&#xAC00;</th><th class="r">&#xD3C9;&#xAC00;&#xC190;&#xC775;</th><th class="r">&#xC9C4;&#xC785;&#xC77C;</th>
    </tr></thead>
    <tbody id="mock-pos"><tr><td colspan="7" style="color:#8b949e;text-align:center">&#xBCF4;&#xC720; &#xD3EC;&#xC9C0;&#xC158; &#xC5C6;&#xC74C;</td></tr></tbody>
    </table>
  </div>
  <!-- KIS -->
  <div class="section">
    <h2>KIS &#xBAA8;&#xC758;&#xD22C;&#xC790; &#xC2AC;&#xB86F;</h2>
    <div class="stats-row" id="kis-slots"></div>
  </div>
  <div class="section">
    <h2>KIS &#xBCF4;&#xC720; &#xD3EC;&#xC9C0;&#xC158;</h2>
    <table><thead><tr>
      <th>&#xC885;&#xBAA9;</th><th>&#xC804;&#xB7B5;</th><th class="r">&#xC218;&#xB7C9;</th>
      <th class="r">&#xD3C9;&#xADE0;&#xB2E8;&#xAC00;</th><th class="r">&#xD604;&#xC7AC;&#xAC00;</th><th class="r">&#xD3C9;&#xAC00;&#xC190;&#xC775;</th><th class="r">&#xC9C4;&#xC785;&#xC77C;</th>
    </tr></thead>
    <tbody id="kis-pos"><tr><td colspan="7" style="color:#8b949e;text-align:center">&#xBCF4;&#xC720; &#xD3EC;&#xC9C0;&#xC158; &#xC5C6;&#xC74C;</td></tr></tbody>
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
    <h2>&#xC218;&#xC9D1; &#xB85C;&#xADF8; (collect_*.log)</h2>
    <pre class="logbox" id="log-sch">&#xB85C;&#xB529;&#xC911;...</pre>
  </div>
  <div class="section">
    <h2>&#xD398;&#xC774;&#xD37C; &#xB85C;&#xADF8; (paper_*.log)</h2>
    <pre class="logbox" id="log-live">&#xB85C;&#xB529;&#xC911;...</pre>
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
</div>

<script>
// ── helpers ──
function fmt(n){return Number(n||0).toLocaleString()}
function pct(n){const v=Number(n||0);return (v>=0?'+':'')+v.toFixed(2)+'%'}
function clr(n){return Number(n||0)>=0?'pos':'neg'}
function safe(v,fallback='-'){return (v===null||v===undefined||v==='')?fallback:v}
function fmtB(n){const v=Number(n||0);if(v>=1e8)return (v/1e8).toFixed(1)+'억';if(v>=1e4)return (v/1e4).toFixed(0)+'만';return fmt(v)}

function showTab(id){
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',['overview','cheonok','mock','backtest','aimodel','datalogs','intraday'][i]===id));
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
     <td class="r ${clr(r.ai_bigwin)}">${r.ai_bigwin!=null?(Number(r.ai_bigwin)*100).toFixed(0)+'%':(r.prob!=null?(Number(r.prob)*100).toFixed(1)+'%':'-')}</td></tr>`
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
  setHtml('ch-signals', sigs.length?sigs.map(r=>
    `<tr><td>${r.stock_code||''} ${r.stock_name||''}</td><td style="color:#8b949e;font-size:11px">${r.strategy||''}</td>
     <td class="r">${String(r.signal_date||'').slice(0,8)}</td>
     <td class="r">${fmt(r.entry_price)}</td>
     <td class="r">${String(r.target_exit_date||'').slice(0,8)}</td>
     <td class="r ${clr(r.return_pct)}">${r.return_pct!=null?pct(r.return_pct):'-'}</td></tr>`
  ).join(''):'<tr><td colspan="6" style="color:#8b949e;text-align:center">만료된 신호만 있거나 없음</td></tr>');
}

function fillSlots(elId, slots){
  const el=document.getElementById(elId);if(!el)return;
  if(!slots||!slots.length){el.innerHTML='<div style="color:#8b949e;font-size:13px">슬롯 없음</div>';return;}
  el.innerHTML=slots.map(s=>{
    const used=s.used||s.filled||s.code;
    return `<div class="stat" style="min-width:90px">
      <div class="v ${used?'pos':'neu'}" style="font-size:14px">${s.code||s.slot_label||s.label||'빈슬롯'}</div>
      <div class="l">${s.strategy||s.strat||''} ${used?'●':'○'}</div>
    </div>`;
  }).join('');
}

function fillMock(mock, kis){
  // Kiwoom
  if(mock&&!mock.error){
    fillSlots('mock-slots', mock.slots||[]);
    const pos=mock.positions||[];
    setHtml('mock-pos', pos.length?pos.map(r=>
      `<tr><td>${r.code||''} ${r.name||''}</td><td style="color:#8b949e;font-size:11px">${r.strategy||''}</td>
       <td class="r">${fmt(r.qty)}</td><td class="r">${fmt(r.avg_price)}</td>
       <td class="r">${fmt(r.cur_price||r.current_price)}</td>
       <td class="r ${clr(r.pnl_pct)}">${pct(r.pnl_pct)}</td>
       <td class="r" style="font-size:11px">${String(r.entry_date||'').slice(0,10)}</td></tr>`
    ).join(''):'<tr><td colspan="7" style="color:#8b949e;text-align:center">보유 포지션 없음</td></tr>');
  }
  // KIS
  if(kis&&!kis.error){
    fillSlots('kis-slots', kis.slots||[]);
    const pos=kis.positions||[];
    setHtml('kis-pos', pos.length?pos.map(r=>
      `<tr><td>${r.code||''} ${r.name||''}</td><td style="color:#8b949e;font-size:11px">${r.strategy||''}</td>
       <td class="r">${fmt(r.qty)}</td><td class="r">${fmt(r.avg_price)}</td>
       <td class="r">${fmt(r.cur_price||r.current_price)}</td>
       <td class="r ${clr(r.pnl_pct)}">${pct(r.pnl_pct)}</td>
       <td class="r" style="font-size:11px">${String(r.entry_date||'').slice(0,10)}</td></tr>`
    ).join(''):'<tr><td colspan="7" style="color:#8b949e;text-align:center">보유 포지션 없음</td></tr>');
  }
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
  // strategy compare
  const scRows=(sc&&sc.rows)||[];
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
    if(d.freshness&&d.freshness.length){
      fb.innerHTML='<div class="fresh-warn">⚠ 데이터 미갱신: '
        +d.freshness.map(f=>f.table+'('+f.age_days+'일 경과)').join(' / ')+'</div>';
    } else {
      fb.innerHTML='<div class="fresh-ok">✓ 데이터 신선</div>';
    }
  }
  // 패널별 렌더 격리 — 한 패널이 터져도 나머지는 그려진다(백엔드 _safe 와 대칭).
  const _r=(fn,...a)=>{try{fn(...a);}catch(e){console.error('[dashboard] render 오류 ('+fn.name+'):',e);}};
  _r(fillOverview, d.cheonok, d.ai, d.candidates);
  _r(fillCheonok, d.cheonok);
  _r(fillMock, d.mock, d.kis);
  _r(fillBacktest, d.bt, d.sc);
  _r(fillAI, d.ai);
  _r(fillLogs, d.logs, d.files);
  // intraday — 별도 API
  fetch('/api/intraday').then(r=>r.json()).then(fillIntraday).catch(()=>{});
}

window.addEventListener('load',()=>{ load(); setInterval(load, 300_000); });
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
