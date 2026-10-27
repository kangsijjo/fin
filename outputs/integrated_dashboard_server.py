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
    from flask import Flask, jsonify, request, session, redirect, Response
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
def _port_from_argv(default=5050):
    """실행 인자로 포트 지정 — **숫자가 아닌 인자는 무시**한다.

    [2026-08-30] 종전엔 int(sys.argv[1]) 를 그대로 했다. 그래서 argv 가 있는
    환경(pytest 등)에서는 **import 만 해도 ValueError 로 죽었다** — 모듈이
    자기가 __main__ 이라고 가정한 것. daily_audit 의 import 부수효과와 같은 부류.
    """
    for a in sys.argv[1:]:
        if a.isdigit():
            return int(a)
    return default


PORT        = _port_from_argv()
BASE        = Path(__file__).parent
AI_DIR      = BASE.parent / "Stock_AI_Project"
DB_PATH     = AI_DIR / "data" / "stock.db"
LOG_DIR     = BASE / "logs"           # 실제 로그 위치: C:\fin\outputs\logs\
RESULTS_DIR = BASE / "results"
KIWOOM_DIR  = BASE / "db" / "kiwoom"

# ── 원격 접속 인증 (2026-08-30 신설) ─────────────────────────────────────────
# 배경: 대시보드는 host="0.0.0.0" 으로 떠 있는데 인증이 하나도 없었다. 잔고,
# 보유종목, 매매이력, 로그가 그대로 보인다. Tailscale 로 집 밖에서 보게 되면서
# 이대로 두면 안 된다.
#
# 설계 원칙
#   · 로컬(같은 PC)은 종전대로 무인증 — 대시보드 버튼과 자동화가 그대로 동작해야 한다.
#   · 비밀번호 미설정 = 원격 접속 **차단**(fail-closed). 실수로 열리는 쪽이 아니라
#     실수로 막히는 쪽으로 기운다.
#   · 비밀번호는 .env 의 DASH_PASSWORD 에서만 읽는다 — 코드에도 git 에도 값이 없다.
#   · 세션 키는 db/ 에 보관해 서버를 재시작해도 폰에서 다시 로그인하지 않게 한다.
import hmac as _hmac
import secrets as _secrets
import time as _time


def _load_dotenv(path=None):
    """.env 를 os.environ 으로 — kis_trader 와 같은 방식(추가 의존성 없음)."""
    p = path or (BASE / ".env")
    try:
        if not os.path.exists(p):
            return
        with open(p, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                # 인라인 주석은 '공백 뒤 #' 부터만 제거 — 값 안의 '#' 보존
                for _i, _ch in enumerate(v):
                    if _ch == "#" and (_i == 0 or v[_i - 1] in " 	"):
                        v = v[:_i]
                        break
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv()
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "").strip()
_LOCAL_ADDRS = ("127.0.0.1", "::1")
_PUBLIC_PATHS = ("/login", "/favicon.ico")
_LOGIN_FAILS = {"n": 0, "until": 0.0}


def _dash_secret_key():
    """세션 서명 키 — db/ 에 보관(gitignore 대상)해 재시작해도 로그인이 유지된다."""
    p = BASE / "db" / "dashboard_secret.txt"
    try:
        if p.exists():
            k = p.read_text(encoding="utf-8").strip()
            if len(k) >= 32:
                return k
        p.parent.mkdir(parents=True, exist_ok=True)
        k = _secrets.token_hex(32)
        p.write_text(k, encoding="utf-8")
        return k
    except Exception:
        return _secrets.token_hex(32)      # 저장 실패해도 동작(재시작 시 재로그인)


app.secret_key = _dash_secret_key()
app.permanent_session_lifetime = timedelta(days=30)
# 타 사이트발 요청에는 세션 쿠키가 실리지 않게(Lax) — CSRF 4번째 겹. JS 에서 쿠키 접근 금지.
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)


def _via_proxy(req):
    """프록시를 거쳐 왔는가 — remote_addr 을 믿을 수 없다는 신호.

    Cloudflare Tunnel/ngrok 류는 PC 안에서 127.0.0.1 로 프록시한다. 그러면
    remote_addr 이 **로컬로 찍혀** '로컬만 허용' 가드가 통째로 무력화된다.
    Tailscale 은 100.x.x.x 로 그대로 오므로 해당 없음. 앞으로 터널을 붙이더라도
    이 가드가 조용히 뚫리지 않도록 헤더 존재만으로 원격 취급한다.
    """
    return any(req.headers.get(h) for h in
               ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP"))


def _is_local(req):
    return (req.remote_addr in _LOCAL_ADDRS) and not _via_proxy(req)


# 상태를 바꾸는 POST 가 '대시보드 페이지 자신'에서 왔는지 확인한다.
_ACTION_HEADER = "X-Dashboard-Action"


def _same_origin_action(req):
    """CSRF 방어 — 세 겹이고 각각 단독으로도 충분하다.

    배경(2026-09-04, 외부 평가에서 지적 → 재현 확인): /api/run 은 remote_addr 이
    127.0.0.1 이면 통과했다. 그런데 사용자가 악성 페이지를 열어두면 그 페이지의 JS 가
      fetch("http://127.0.0.1:5050/api/run/kiwoom_buy", {method:"POST", mode:"no-cors"})
    를 보낼 수 있고, 브라우저 입장에서 이건 **내 PC 에서 나가는 요청**이라 remote_addr 이
    127.0.0.1 로 찍힌다. 가드를 통과해 kiwoom_trader.py buy 가 실행된다. 응답은 못 읽지만
    주문은 이미 나갔다. 같은 날 붙인 인증 계층도 로컬은 면제라 소용이 없다.
    (Flask 테스트 클라이언트 재현: 타 출처 POST → 400 '알 수 없는 작업' = 실행기 도달)

    ① 커스텀 헤더 필수 — 타 출처는 이 헤더를 붙일 수 없다. no-cors 모드와 HTML form 은
       커스텀 헤더 자체가 불가능하고, 일반 fetch 는 preflight 를 거치는데 우리는 CORS 를
       허용하지 않으므로 브라우저가 막는다. **값은 비밀이 아니어도 된다** — 방어의 핵심은
       값이 아니라 '커스텀 헤더가 존재한다'는 사실이다(시크릿 토큰은 관리 부담만 늘린다).
    ② Origin 이 있으면 우리 호스트와 같아야 한다.
    ③ Sec-Fetch-Site 가 있으면 same-origin 이어야 한다.
    """
    if req.headers.get(_ACTION_HEADER) != "1":
        return False
    origin = req.headers.get("Origin")
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc != req.host:
            return False
    sfs = req.headers.get("Sec-Fetch-Site")
    if sfs and sfs not in ("same-origin", "none"):
        return False
    return True


def _page(title, body, code=200):
    html = ("<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title>"
            "<style>body{font-family:system-ui,-apple-system,'Malgun Gothic',sans-serif;"
            "background:#0f1115;color:#e6e6e6;display:flex;min-height:100vh;margin:0;"
            "align-items:center;justify-content:center}"
            ".c{max-width:22rem;width:90%;text-align:center}"
            "h1{font-size:1.1rem;margin:0 0 .8rem}p{color:#9aa0a6;font-size:.85rem;line-height:1.6}"
            "input{width:100%;padding:.7rem;margin:.6rem 0;border-radius:.4rem;border:1px solid #333;"
            "background:#191c22;color:#e6e6e6;font-size:1rem;box-sizing:border-box}"
            "button{width:100%;padding:.7rem;border:0;border-radius:.4rem;background:#2b6cb0;"
            "color:#fff;font-size:1rem;cursor:pointer}"
            ".e{color:#ff6b6b;font-size:.85rem;min-height:1.2rem}</style>"
            f"<div class=c>{body}</div>")
    return Response(html, code, mimetype="text/html; charset=utf-8")


@app.before_request
def _require_auth():
    if _is_local(request):
        return None                                   # 같은 PC — 종전과 동일
    if request.path in _PUBLIC_PATHS:
        return None
    if not DASH_PASSWORD:
        return _page("설정 필요",
                     "<h1>원격 접속이 설정되지 않았습니다</h1>"
                     f"<p>{BASE / '.env'} 에 <b>DASH_PASSWORD=원하는비밀번호</b> 를 "
                     "추가하고 대시보드를 재시작하세요.</p>", 403)
    if session.get("ok") is True:
        return None
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def dash_login():
    if _is_local(request):
        return redirect("/")
    if not DASH_PASSWORD:
        return _page("설정 필요", "<h1>원격 접속이 설정되지 않았습니다</h1>", 403)

    err = ""
    now = _time.time()
    if now < _LOGIN_FAILS["until"]:
        wait = int(_LOGIN_FAILS["until"] - now)
        return _page("잠김", f"<h1>잠시 잠겼습니다</h1><p>{wait}초 후 다시 시도하세요.</p>", 429)

    if request.method == "POST":
        # compare_digest — 맞는 글자 수에 따라 응답시간이 달라지지 않게 한다
        if _hmac.compare_digest(request.form.get("pw") or "", DASH_PASSWORD):
            session["ok"] = True
            session.permanent = True
            _LOGIN_FAILS["n"] = 0
            return redirect("/")
        _LOGIN_FAILS["n"] += 1
        if _LOGIN_FAILS["n"] >= 10:                   # 무차별 대입 완화
            _LOGIN_FAILS["until"] = now + 300
            _LOGIN_FAILS["n"] = 0
        _time.sleep(0.5)
        err = "비밀번호가 맞지 않습니다."

    return _page("천억이", "<h1>천억이 대시보드</h1>"
                 f"<div class=e>{err}</div>"
                 "<form method=post><input type=password name=pw placeholder='비밀번호' "
                 "autofocus autocomplete='current-password'>"
                 "<button type=submit>들어가기</button></form>")


KIS_DIR     = BASE / "db" / "kis"
MACRO_DAILY = BASE / "macro_data" / "daily"

PAPER_CSV       = BASE / "paper_signals.csv"
KIS_SIGNALS_CSV = BASE / "kis_paper_signals.csv"
STRENGTH_LOG_CSV = BASE / "db" / "signal_strength_log.csv"
CUT_MONITOR_CSV  = BASE / "db" / "strength_cut_monitor.csv"   # 전략별임계 모니터(Phase 1)
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


def _log_encoding(path, sample=65536):
    """로그 파일의 인코딩을 '파일 선두'에서 판정한다.

    [2026-07-21 한글깨짐 수정] 종전엔 tail 로 잘라낸 바이트 조각에 대고 utf-8→cp949
    순으로 시도했는데, 조각의 시작이 문자 중간이면 **양쪽 다 실패**해 최후 폴백인
    `utf-8 errors=replace` 가 파일 전체를 물음표로 만들었다(실측: collect 45KB 로그가
    한글 22자 / 깨짐문자 4,502개). 파일 선두는 항상 문자 경계이므로 여기서 판정하면
    조각 경계와 무관하게 정확하다. 판정 후엔 조각을 그 인코딩으로 replace 디코드 —
    깨지는 건 경계의 한두 글자뿐이다.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(sample)
    except Exception:
        return "utf-8"
    if not head:
        return "utf-8"
    for cut in (0, 1, 2, 3):        # 샘플 끝에서 잘린 멀티바이트 문자 무시
        try:
            head[:len(head) - cut].decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            continue
    return "cp949"                  # utf-8 로 성립 불가 → 레거시 cp949 로그


def _decode_log(raw: bytes, path) -> str:
    """tail 바이트 조각 → 문자열 (파일 선두 기준 인코딩으로 안전 디코드)."""
    return raw.decode(_log_encoding(path), errors="replace")


def tail_log(path, n=60):
    """로그 파일 마지막 n줄을 문자열로 반환 (JS textContent 호환)."""
    if not Path(path).exists():
        return ""
    try:
        raw = Path(path).read_bytes()[-20000:]
        return "\n".join(_decode_log(raw, path).strip().splitlines()[-n:])
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
        # [2026-10-26 §77] 워치독은 보는데 대시보드 버튼 깜빡임엔 없던 3개(달력일 기준 — 주말 포함 여유)
        ("korea_indicators", "SELECT MAX(date) FROM korea_indicators", 5),
        ("program_trading",  "SELECT MAX(date) FROM program_trading", 5),
        ("foreign_ratio",    "SELECT MAX(date) FROM foreign_ratio", 6),
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
# [2026-10-21] live_config 단일 출처 — _slot_status 가 호출될 때마다 다시 읽는다
def _kw_slot_conf():
    import live_config as LC
    A = LC.account("kiwoom")
    if A.get("slot_mode") == "pool":                 # [§75] 칸 없음 — 전략별 상한 없음(전체 10)
        return {k: LC.SLOTS_TOTAL for k in A["priority"]}, list(A["priority"])
    return dict(A["slots"]), list(A["priority"])


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
    _maxs, _prio = _kw_slot_conf()
    used = _ledger_slot_counts(KIWOOM_DIR / "kiwoom_positions.csv", _maxs)
    slots = []
    for strat in _prio:
        mx = _maxs[strat]
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


def get_strategy_full():
    """[2026-10-12] 전체 전략 과거 백테스트(진짜 지표) — strategy_engine.py --full 산출
    results/strategy_full_YYYYMMDD.csv 최신본. 이름이 정확히 날짜형인 파일만 고른다
    (구 '전략별 비교'는 strategy_compare_xover_*.csv 같은 실험 파일이 정렬상 최신으로 잡혀
    2행짜리 표가 떠 있었다)."""
    import re as _re
    out = {"rows": [], "updated": "", "source": ""}
    if not RESULTS_DIR.exists():
        return out
    files = sorted(f for f in RESULTS_DIR.glob("strategy_full_*.csv")
                   if _re.fullmatch(r"strategy_full_\d{8}\.csv", f.name))
    if not files:
        return out
    latest = files[-1]
    out["source"] = latest.name
    out["updated"] = latest.name[len("strategy_full_"):-4]
    try:
        df = pd.read_csv(latest, encoding="utf-8-sig",
                         dtype={"period_start": str, "period_end": str, "oos_cutoff": str})
        df = df.astype(object).where(pd.notnull(df), None)
        out["rows"] = df.to_dict("records")
        if len(df) and "period_start" in df.columns:
            # 미계산 행(NaN)이 섞이면 CSV 에 '20180614.0' 처럼 실수로 저장된다 → '.0' 제거
            _d = lambda col: [str(r).split(".")[0] for r in df.get(col, pd.Series(dtype=object)).tolist() if r]
            ps, pe, oc = _d("period_start"), _d("period_end"), _d("oos_cutoff")
            out["period"] = f"{min(ps)}~{max(pe)}" if ps and pe else ""
            out["oos_cutoff"] = max(oc) if oc else ""
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

    주문로그엔 매입가가 없어 같은 code 의 직전 매수 가격과 시간순 FIFO 매칭한다.
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


def _round_trip_trades(combined, strength_idx):
    """주문로그를 라운드트립(매수→매도)으로 묶어 한 종목 진입·청산을 한 줄로(2026-07-13).

    FIFO 로 매수 로트와 매도를 매칭(_attach_realized_pnl 과 동일 규약). 매도가는 이미
    보정된 값(combined 은 _attach_realized_pnl 적용본). 미청산 매수는 매도열 공란(보유중).
    tp_sell(익절 '발주')은 제외 — 실제 체결은 별도 'sell' 합성행으로 들어옴.
    반환: 최신순 dict 리스트(code,name,strategy,score_ic,buy_date,buy_time,buy_price,
          sell_date,sell_time,sell_price,qty,pnl,pnl_pct,reason,open)."""
    from collections import deque
    out = []
    try:
        df = combined.copy()
        df["_d"] = df.get("date", "").astype(str)
        df["_t"] = df.get("time", "").astype(str)
        df = df.sort_values(["_d", "_t"])
        lots = {}   # code -> deque of buy lot dicts
        for _, r in df.iterrows():
            ok = str(r.get("ok", "")).strip().lower() not in ("false", "0", "")
            if not ok:
                continue
            side = str(r.get("side", "")).lower()
            code = str(r.get("code", "")).zfill(6)
            try:
                qty = int(float(r.get("qty", 0) or 0)); price = float(r.get("price", 0) or 0)
            except (TypeError, ValueError):
                continue
            if side == "buy":
                if qty <= 0:
                    continue
                sc = _pick_strength(strength_idx, code, r.get("strategy", ""), r.get("date", "")) \
                    if strength_idx else None
                lots.setdefault(code, deque()).append({
                    "code": code, "name": str(r.get("name", "") or ""),
                    "strategy": str(r.get("strategy", "") or ""), "score_ic": sc,
                    "buy_date": str(r.get("date", "")), "buy_time": str(r.get("time", "")),
                    "buy_price": int(round(price)), "qty": qty})
            elif side == "sell":   # tp_sell(발주)은 제외
                rem = qty
                q = lots.get(code)
                while rem > 0 and q:
                    lot = q[0]
                    take = min(rem, lot["qty"])
                    bp, sp = lot["buy_price"], int(round(price))
                    out.append({
                        "code": code, "name": lot["name"], "strategy": lot["strategy"],
                        "score_ic": lot["score_ic"],
                        "buy_date": lot["buy_date"], "buy_time": lot["buy_time"], "buy_price": bp,
                        "sell_date": str(r.get("date", "")), "sell_time": str(r.get("time", "")),
                        "sell_price": sp, "qty": take,
                        "pnl": int(round((sp - bp) * take)) if (bp > 0 and sp > 0) else "",
                        "pnl_pct": round((sp / bp - 1) * 100, 2) if bp > 0 else "",
                        "reason": str(r.get("reason", "") or r.get("msg", "") or ""),
                        "open": False})
                    lot["qty"] -= take; rem -= take
                    if lot["qty"] <= 0:
                        q.popleft()
        # 남은 매수 로트 = 보유중(매도열 공란)
        for code, q in lots.items():
            for lot in q:
                out.append({
                    "code": code, "name": lot["name"], "strategy": lot["strategy"],
                    "score_ic": lot["score_ic"],
                    "buy_date": lot["buy_date"], "buy_time": lot["buy_time"], "buy_price": lot["buy_price"],
                    "sell_date": "", "sell_time": "", "sell_price": "", "qty": lot["qty"],
                    "pnl": "", "pnl_pct": "", "reason": "보유중", "open": True})
    except Exception:
        return out
    # 최신순: 청산건은 매도일, 보유건은 매수일 기준
    out.sort(key=lambda t: (t["sell_date"] or t["buy_date"], t["sell_time"] or t["buy_time"]),
             reverse=True)
    return out


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
            _sidx = _signal_strength_index("kiwoom")
            out["trades"] = _round_trip_trades(combined, _sidx)   # 라운드트립 매매내역(한 줄)
            out["trade_total"] = len(out["trades"])
            for p in out["positions"]:   # 보유 포지션 강도 부착
                p["score_ic"] = _pick_strength(_sidx, p.get("code"), p.get("strategy"), p.get("entry_date"))
    out["slot_status"] = _kiwoom_slot_status()   # 키움 모의투자 슬롯(진입/청산 조건 포함)
    return out


# ── 4b. KIS mock account (안D 포트폴리오) ─────────────────────────────────────
# ── 모의투자 슬롯 정의(전략별 슬롯수 + 진입/청산 조건) — 슬롯 클릭 드롭다운용 ──
# [2026-10-21] 구성은 live_config(대시보드 '모의매매 전략 설정') 단일 출처 → 요청마다 다시 읽는다(설정을 바꾸면
# 대시보드 재시작 없이 바로 반영). 아래는 '진입 조건 문구'의 바탕(강도 부분은 설정에서 붙인다).
_LIVE_BASE_LABEL = {
    "h52w_for3d_mkt": "52주신고가+외국인", "for_high20_mkt": "20일신고가+외국인", "gc_for3d": "골든크로스+외국인",
    "high_500d_filt": "500일 신고가+거래량", "high_52w_filt": "52주 신고가+거래량",
    "rsi_reversal": "RSI 과매도 반전", "rsi_vol": "RSI 과매도+거래량급증",
}
_LIVE_BASE_ENTRY = {
    "h52w_for3d_mkt": "52주 신고가 돌파 + 외국인 3일 연속 순매수 + 시장강세 게이트(전종목 평균등락 60일MA>0) + 거래대금 ≥ 30억",
    "for_high20_mkt": "20일 신고가(종가 ≥ 직전20일 최고) + 외국인 3일 연속 순매수 + 시장강세 게이트 + 거래대금 ≥ 30억",
    "gc_for3d": "골든크로스(MA20이 MA60 상향돌파) + 외국인 3일 연속 순매수 + 거래대금 ≥ 30억 (시장게이트 없음)",
    "high_500d_filt": "종가 > 직전 500영업일 최고가(약 2년 신고가) + 시장강세 게이트(전종목 평균등락 60일MA>0) + 당일 거래대금 ≥ 30일평균 ×1.5 + 거래대금 ≥ 30억",
    "high_52w_filt": "종가 > 직전 252영업일 신고가(52주 돌파) + 시장강세 게이트(전종목 평균등락 60일MA>0) + 당일 거래대금 ≥ 30일평균 ×1.5 + 거래대금 ≥ 30억",
    "rsi_reversal": "RSI(14) < 30 (과매도) + 거래대금 ≥ 10억",
    "rsi_vol": "RSI(14) < 30 + 당일 거래대금 > 20일평균 ×2.0(거래량 급증) + 거래대금 ≥ 10억",
}


def _live_tables(acct):
    """계좌의 현재 운용 구성 → (slots, labels, holds, entries, exits, stops%) — 설정에서 매번 만든다."""
    import live_config as LC
    A = LC.account(acct)
    base = _STRAT_BASE_INFO()
    slots = {ln: A["slots"][ln] for ln in A["priority"]}
    if A.get("slot_mode") == "pool":                 # [§75] 칸 없음 — 전략마다 전체 10칸까지 쓸 수 있음(표시용)
        slots = {ln: LC.SLOTS_TOTAL for ln in A["priority"]}
    labels, holds, entries, exits, stops = {}, {}, {}, {}, {}
    for ln in A["priority"]:
        d = base.get(ln) or {}
        labels[ln] = _LIVE_BASE_LABEL.get(ln) or d.get("title") or ln
        ent = _LIVE_BASE_ENTRY.get(ln) or " + ".join(d.get("buy") or []) or ln
        entries[ln] = ent + (" · 강도 필터 면제(강도순 정렬만)" if ln in A["exempt"]
                             else f" + 강도 ≥ {A['min_strength_by'].get(ln, A['min_strength']):g}"
                                  + ("(전략별)" if A['min_strength_by'].get(ln, A['min_strength']) != A['min_strength'] else ""))
        holds[ln] = A["holding"][ln]
        st, tk = A["stop"].get(ln), A["take"].get(ln)
        stops[ln] = round(st * 100) if st is not None else ""
        parts = []
        if tk is not None:
            parts.append(f"익절 +{tk * 100:g}% " + ("장중 지정가(매일 아침 발주)" if acct == "kiwoom"
                                                   else "(전일 종가 판정 → 다음날 시가 매도)"))
        if st is not None:
            parts.append(f"{st * 100:g}% 손절" + (" (15:21 평가손익 판정 → 마감 동시호가 매도)" if acct == "kiwoom"
                                                  else "(전일 종가 판정 → 다음날 시가 매도)"))
        exits[ln] = " 또는 ".join(parts + [f"진입 {holds[ln]}영업일째 종가"]) + ("" if parts else " (손절·익절 없음)")
    return slots, labels, holds, entries, exits, stops


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
    return _slot_status_detail(KIS_DIR / "kis_positions.csv", *_live_tables("kis"))


def _kiwoom_slot_status():
    return _slot_status_detail(KIWOOM_DIR / "kiwoom_positions.csv", *_live_tables("kiwoom"))

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
            _sidx = _signal_strength_index("kis")
            out["trades"] = _round_trip_trades(combined, _sidx)   # 라운드트립 매매내역(한 줄)
            out["trade_total"] = len(out["trades"])
            for p in out.get("positions", []):   # 보유 포지션 강도 부착
                p["score_ic"] = _pick_strength(_sidx, p.get("code"), p.get("strategy"), p.get("entry_date"))
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


def get_cut_monitor(limit=200):
    """전략별 임계 '모니터 전용' 집계(strength_cut_monitor.py 산출)를 요약해 반환.

    현행 5.7(단일 절대임계) vs 전략별 고정임계 상위40%/50% — **매매에는 개입하지 않고**
    '그 규칙이었으면 무엇을 샀고 얼마였을까'만 비교한다(시스템_설명서 §39).
    """
    out = {"rules": [], "rows": [], "by_strategy": [], "daily": [],
           "generated": "", "days": 0, "note": ""}
    if not CUT_MONITOR_CSV.exists():
        out["note"] = "아직 집계 없음 — strength_cut_monitor.py 를 한 번 실행하세요."
        return out
    try:
        df = pd.read_csv(CUT_MONITOR_CSV, dtype={"code": str, "signal_date": str,
                                                 "entry_date": str, "exit_date": str},
                         encoding="utf-8-sig")
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    if df.empty:
        out["note"] = "집계 결과가 비어 있습니다."
        return out
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["net_pct"] = pd.to_numeric(df["net_pct"], errors="coerce")
    df["done"] = pd.to_numeric(df["done"], errors="coerce").fillna(0).astype(int)
    out["generated"] = datetime.fromtimestamp(CUT_MONITOR_CSV.stat().st_mtime
                                              ).strftime("%Y-%m-%d %H:%M")
    out["days"] = int(df["signal_date"].nunique())
    out["span"] = f"{df['signal_date'].min()}~{df['signal_date'].max()}"

    for rule, g in df.groupby("rule", sort=False):
        d = g[g["done"] == 1]
        # 누적 수익: 슬롯 10분할 가정(라이브와 동일) — 매매당 자본의 1/10 을 태운다
        cum = float(np.prod([1 + v / 1000.0 for v in d["net_pct"].dropna()]) - 1) * 100
        out["rules"].append({
            "rule": rule,
            "n": int(len(g)), "done": int(len(d)), "open": int((g["done"] == 0).sum()),
            "avg": round(float(d["net_pct"].mean()), 2) if len(d) else None,
            "med": round(float(d["net_pct"].median()), 2) if len(d) else None,
            "win": round(float((d["net_pct"] > 0).mean() * 100), 1) if len(d) else None,
            "worst": round(float(d["net_pct"].min()), 2) if len(d) else None,
            "cum": round(cum, 2) if len(d) else None,
            "kiwoom": int((g["acct"] == "kiwoom").sum()),
            "kis": int((g["acct"] == "kis").sum()),
            "strats": int(g["strategy"].nunique()),
        })

    dd = df[df["done"] == 1]
    if len(dd):
        for (rule, strat), g in dd.groupby(["rule", "strategy"]):
            out["by_strategy"].append({
                "rule": rule, "strategy": strat, "n": int(len(g)),
                "avg": round(float(g["net_pct"].mean()), 2),
                "win": round(float((g["net_pct"] > 0).mean() * 100), 1)})
    for sd, g in df.groupby("signal_date"):
        row = {"date": sd}
        for rule in df["rule"].unique():
            row[rule] = int((g["rule"] == rule).sum())
        out["daily"].append(row)
    out["daily"] = out["daily"][-30:]

    cols = ["rule", "signal_date", "entry_date", "acct", "strategy", "code", "name",
            "score", "entry_price", "exit_date", "net_pct", "why", "done"]
    cols = [c for c in cols if c in df.columns]
    out["rows"] = (df.sort_values(["signal_date", "rule"], ascending=[False, True])
                     .head(limit)[cols].fillna("").to_dict("records"))
    return out


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
            log_txt = _decode_log(_sch.read_bytes()[-30000:], _sch)   # 인코딩 자동판정(2026-07-21)
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
# 추론 피처 순서 — feature_spec 단일 출처에서만 가져온다(2026-06-29 단일화).
#   사본을 두지 않는다: 사본은 반드시 표류하고, 표류한 사본은 조용한 오답을 만든다.
try:
    from feature_spec import FALLBACK_MODEL_ORDER as _AI_FEAT_ORDER
except Exception:
    # [2026-08-20] 하드코딩 사본을 제거했다. 종전 폴백은 정본(FALLBACK_MODEL_ORDER 32개)과
    # 이미 어긋난 24개였고(for_hold_ratio/for_ratio_chg20 누락 + 전략 원-핫 9개 중 3개만),
    # 하필 그 길이가 현재 학습 차원(24)과 같아 **차원은 맞는데 피처가 다른** 상태로 추론이
    # 통과할 수 있었다 — 차원 불일치는 예외로 드러나지만 이건 조용한 오답이 된다.
    # feature_spec 단일 출처를 못 읽으면 AI 점수를 포기하는 편이 안전하다.
    # (2026-06-29 단일화의 취지가 바로 이 사본 표류를 없애는 것이었다.)
    _AI_FEAT_ORDER = []
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
    if not feat_order:
        # 피처 계약을 확정하지 못하면 추론하지 않는다 — 틀린 확률보다 빈 값이 낫다.
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

    # ── 프로그램매매 5일 절단합 (factor_scorer prm_net_5d_raw 와 동일 정의, 2026-09-08) ─────
    # 신호일(ld_str) 이하 최근 5행, 음수 날은 0 절단, 5행 미만이면 결측. 테이블 없으면 safe_db 가 [] → NaN 유지.
    pt_rows = safe_db(
        f"SELECT ticker, prm_net_amt FROM ("
        f"  SELECT ticker, prm_net_amt, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) rn "
        f"  FROM program_trading WHERE ticker IN ({codes_q}) AND date <= '{ld_str}' AND prm_net_amt IS NOT NULL"
        f") WHERE rn <= 5"
    )
    _pt_acc = {}
    for r in pt_rows or []:
        _pt_acc.setdefault(str(r[0]).zfill(6), []).append(float(r[1] or 0))
    pt_map = {c: sum(max(v, 0.0) for v in vs) for c, vs in _pt_acc.items() if len(vs) == 5}

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
            # 5일 절단합(백만원) / 당일 거래대금(원) — 학습 정의. 데이터·거래대금 없으면 NaN.
            "prm_net_5d_ratio": (pt_map[code] / float(r.get("trading_value"))
                                 if code in pt_map and float(r.get("trading_value") or 0) > 0
                                 else np.nan),
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
            "sfull":      _safe(get_strategy_full),
            "mock":       _safe(get_kiwoom_mock, df, dt),
            "kis":        _safe(get_kis_mock, df, dt),
            "strength":   _safe(get_strength),
            "cutmon":     _safe(get_cut_monitor),
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
        # [2026-07-21] 조각 경계에서 양쪽 디코드가 실패해 전체가 물음표가 되던 버그 —
        # 파일 선두 기준으로 인코딩을 판정한 뒤 조각을 replace 디코드(_decode_log).
        txt = _decode_log(raw, p)
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
    if not _same_origin_action(request):          # CSRF 차단(2026-09-04)
        return jsonify({"error": "대시보드 페이지에서만 실행할 수 있습니다"}), 403
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
    "strategy_full": [_VENV_PYTHON, str(BASE / "strategy_engine.py"), "--full"],     # 전 전략 과거 백테스트(진짜 지표, 20~30분)
    "preview":       [_VENV_PYTHON, str(BASE / "intraday_preview.py")],              # 장중 잠정 예비후보
    "market_grid":   [_VENV_PYTHON, str(BASE / "market_grid.py")],                   # 장중시황 2x6 그리드(키움)
    "daily_summary": [_VENV_PYTHON, str(BASE / "daily_summary.py")],                  # 텔레그램 하루요약 수동 전송
    "watchdog":      [_VENV_PYTHON, str(BASE / "watchdog.py")],                       # 조용한 실패 감지 → 텔레그램(수동 점검)
    "kis_stopcheck": [_VENV_PYTHON, str(BASE / "kis_trader.py"), "stopcheck"],        # KIS 장중 손절 모니터
    # ── 개별 데이터 수집기 (정체분만 콕 집어 수동수집 — Stock_AI venv/cwd) ──
    "supply_demand": [_STOCK_AI_PY, "-m", "src.collector.supply_demand"],            # 외국인/기관 수급(KIS)
    "macro":         [_STOCK_AI_PY, "-m", "src.collector.macro"],                    # 거시지표(NASDAQ/VIX/환율)
    "credit":        [_STOCK_AI_PY, "-m", "src.collector.kiwoom_extra",
                      "--backfill", "--since", "{SINCE45}", "--force"],              # 신용/대차(키움) — 날짜는 누를 때 계산
                      # --force: 수집기의 '주말 전용' 가드 우회 — 수동 버튼은 사용자가 시점을
                      # 직접 고르는 것이므로 평일에도 동작(2026-07-06). 매매창(09:00~05·15:20~30)
                      # 밖 실행 권장은 버튼 안내 그대로 유지.
    "after_market":  [_VENV_PYTHON, str(BASE / "after_market.py")],                  # 시간외 등락률 → stock.db 누적
    # ── [2026-10-26 §77] 워치독이 감시하는데 수동 버튼이 없던 데이터 4종(스케줄러와 같은 명령) ──
    "indicators":    [_STOCK_AI_PY, "run_pipeline.py", "--step", "indicators"],        # 기술지표(korea_indicators) 전 섹터 재계산(45~90분)
    "program":       [_STOCK_AI_PY, "-m", "src.collector.kiwoom_program",
                      "--since", "{SINCE30}", "--force"],                            # 프로그램매매(키움 ka90013) 30일 공백
    "foreign_ratio": [_STOCK_AI_PY, "foreign_ratio_collector.py", "--backfill"],     # 외국인 지분율(KRX) 미수집 날짜
    "news":          [_STOCK_AI_PY, "-m", "src.processor.news", "historical", "all"],  # 뉴스/공시(DART) 전 섹터
}

# [2026-10-26 §77] 증권사 API 키를 쓰는 수집 — 매매가 도는 시간(장전 예열·09:00 매수·15:05 손절·15:21 만기)에 누르면
# 같은 키로 동시에 호출돼 서로 실패한다(10-22 07:14 신용 버튼 → 09:02~09:15 '네트워크 실패' 146건, 키움 토큰 재발급은
# 트레이더 토큰도 무효로 만들 수 있음). 이 시간엔 버튼을 막고 이유를 알려 준다.
_BROKER_TASKS = {"backfill", "supply_demand", "credit", "program", "after_market", "recheck"}
_TRADE_WINDOWS = (("0840", "0925"), ("1500", "1545"))


def _trade_window_block(task, now=None):
    """매매 시간대면 막는 이유 문구, 아니면 ''."""
    if task not in _BROKER_TASKS:
        return ""
    now = now or datetime.now()
    try:
        from market_calendar import is_trading_day
        td = is_trading_day(now)
    except Exception:
        td = now.weekday() < 5
    hm = now.strftime("%H%M")
    for a, b in _TRADE_WINDOWS:
        if td and a <= hm < b:
            return (f"지금({now:%H:%M})은 자동매매가 증권사 API 를 쓰는 시간({a[:2]}:{a[2:]}~{b[:2]}:{b[2:]})이라 "
                    f"막았습니다 — 같은 키로 동시에 부르면 매매와 수집이 서로 실패합니다. 이 시간이 지난 뒤 다시 누르세요.")
    return ""


def _resolve_cmd(cmd, now=None):
    """명령 안 날짜 자리표시자를 '누른 시점' 기준으로 채움(종전엔 대시보드 시작 시각으로 고정돼 며칠 켜 두면 밀렸다)."""
    now = now or datetime.now()
    rep = {"{SINCE45}": (now - timedelta(days=45)).strftime("%Y-%m-%d"),
           "{SINCE30}": (now - timedelta(days=30)).strftime("%Y-%m-%d")}
    return [rep.get(x, x) for x in cmd]

# 작업별 cwd (미지정 시 outputs). Stock_AI 수집기는 프로젝트 루트에서 실행해야 -m 임포트가 됨.
_RUN_CWD = {
    "supply_demand": str(_STOCK_AI_DIR),
    "macro":         str(_STOCK_AI_DIR),
    "credit":        str(_STOCK_AI_DIR),
    "indicators":    str(_STOCK_AI_DIR),
    "program":       str(_STOCK_AI_DIR),
    "foreign_ratio": str(_STOCK_AI_DIR),
    "news":          str(_STOCK_AI_DIR),
}

_RUN_PROCS: dict = {}   # task -> 마지막 Popen. 중복실행 가드용(2026-07-17).
# ai_data/ 산출물(historical_features·trades_history·meta_model)을 공유하는 작업군 —
# 동시 실행 시 학습이 재작성 중인 파일을 읽는 오염 발생(12:44 실사례) → 상호배제.
_AI_GROUP = {"ai_dataset", "ai_train", "ai_pipeline"}


@app.after_request
def _security_headers(resp):
    """[2026-09-04] CORS 와일드카드 제거 — CSRF 의 문을 닫는다.

    종전엔 "dashboard.html(file://)에서 fetch 가능하도록" Access-Control-Allow-Origin: *
    를 붙였다. 그런데 / 가 HTML 을 직접 서빙하므로 file:// 경로는 이미 쓰이지 않는다.
    남은 효과는 하나 — **아무 사이트의 JS 가 이 서버의 응답을 읽을 수 있게** 해주는 것.
    잔고·보유종목·매매이력이 그 대상이었다. 헤더를 붙이지 않으면 브라우저가 타 출처
    요청을 막아준다. 같은 출처(대시보드 페이지 자신)는 CORS 와 무관하게 동작한다.
    """
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


@app.route("/api/run/<task>", methods=["POST", "OPTIONS"])
def api_run(task):
    """수동 실행 엔드포인트 — 로컬 접속만 허용."""
    if request.method == "OPTIONS":
        return "", 204
    # 타 사이트 페이지가 내 브라우저를 통해 보내는 요청 차단 — _same_origin_action 주석 참고.
    if not _same_origin_action(request):
        return jsonify({"ok": False,
                        "msg": "대시보드 페이지에서만 실행할 수 있습니다(CSRF 차단)"}), 403
    # 프록시 경유면 remote_addr 을 믿을 수 없다 — _via_proxy 주석 참고.
    # 원격에서 스케줄러 재시작/AI 파이프라인을 돌릴 수 있으면 안 된다.
    if not _is_local(request):
        return jsonify({"ok": False, "msg": "로컬에서만 허용(원격 실행 불가)"}), 403

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
    _blk = _trade_window_block(task)
    if _blk:
        return jsonify({"ok": False, "msg": _blk}), 423

    # ── 중복실행 가드(2026-07-17) ──
    # (a) 같은 작업이 도는 중이면 재시작 금지 — 로그("w") 덮어쓰기·이중실행 방지.
    # (b) AI 그룹은 상호배제 — 12:44 ai_dataset+ai_train 동시 실행으로 학습이
    #     재작성 중인 historical_features.csv 를 읽는 오염이 실제 발생(계약테스트 적발).
    prev = _RUN_PROCS.get(task)
    if prev is not None and prev.poll() is None:
        return jsonify({"ok": False,
                        "msg": f"{task} 이미 실행 중(PID {prev.pid}) — 완료 후 다시 시도"})
    if task in _AI_GROUP:
        for other in _AI_GROUP - {task}:
            p = _RUN_PROCS.get(other)
            if p is not None and p.poll() is None:
                return jsonify({"ok": False,
                                "msg": f"AI 상호배제: {other} 실행 중(PID {p.pid}) — "
                                       f"ai_data 파일 동시 접근 방지, 완료 후 실행"})

    try:
        # 출력은 로그 파일로 (PIPE 미소비 시 긴 작업이 버퍼 차서 멈추는 것 방지).
        log_dir = BASE / "logs"
        log_dir.mkdir(exist_ok=True)
        logf = open(log_dir / f"run_{task}.log", "w", encoding="utf-8", errors="replace")
        proc = _sp.Popen(
            _resolve_cmd(_RUN_TASKS[task]), cwd=_RUN_CWD.get(task, str(BASE)),
            stdout=logf, stderr=_sp.STDOUT,
            creationflags=_NO_WIN,
        )
        _RUN_PROCS[task] = proc
        return jsonify({"ok": True, "msg": f"시작됨 (PID {proc.pid}) → logs/run_{task}.log 에 기록"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ── 9b. 기간 백테스트 테스트기 (2026-10-18) ─────────────────────────────────────
# period_backtest.py 를 **별도 프로세스**로 돌린다(일봉 0.8GB 를 서버에 올리지 않는다).
# 진행·결과는 db/period_bt/{job}.json 에 실시간 기록 → 화면이 3초마다 /api/pbt/status 로 읽는다.
# 상태를 바꾸는 POST(점검·실행)는 /api/run 과 같은 규칙: CSRF 커스텀 헤더 + 로컬 전용.
_PBT_DIR = BASE / "db" / "period_bt"
_PBT_PROC = {"proc": None, "job": None}
_PBT_JOB_RE = re.compile(r"pbt_\d{8}_\d{6}")
_DATE_RE = re.compile(r"\d{8}")


def _pbt_names():
    import strategy_engine as _E
    return [s.name for s in _E.ALL_STRATEGIES]


def _pbt_parse(req):
    """요청 본문 검증 → (start, end, names, luck, strength) 또는 (None, 오류문구).
    strength: 강도 비교 임계 목록(예 [4.0, 5.0, 5.7]) — period_backtest.parse_strength 로 검증(0~10, 최대 5개)."""
    b = req.get_json(silent=True) or {}
    start, end = str(b.get("start", "")), str(b.get("end", ""))
    if not (_DATE_RE.fullmatch(start) and _DATE_RE.fullmatch(end)):
        return None, "날짜는 YYYYMMDD 8자리여야 합니다"
    names = b.get("names") or []
    if not isinstance(names, list) or not names:
        return None, "전략을 하나 이상 고르세요"
    valid = set(_pbt_names())
    bad = [n for n in names if n not in valid]
    if bad:
        return None, f"알 수 없는 전략: {', '.join(map(str, bad[:5]))}"
    try:
        import period_backtest as P
        strength = P.parse_strength(b.get("strength", ""))
        stop, take = P.parse_exits(b.get("stop"), b.get("take"))      # [2026-10-21] 손절·익절
    except (ValueError, TypeError) as e:
        return None, str(e)
    mode = str(b.get("exit_mode") or "close")
    if mode not in ("close", "intraday"):
        return None, "판정 방식은 close / intraday"
    sv = str(b.get("strength_version") or "v1")
    if sv not in ("v1", "v2"):
        return None, "강도 방식은 v1 / v2"
    ex = (["--stop", f"{stop:g}"] if stop is not None else []) + (["--take", f"{take:g}"] if take is not None else [])
    if ex:
        ex += ["--exit-mode", mode]
    if strength and sv == "v2":
        ex += ["--strength-version", "v2"]
    return (start, end, names, bool(b.get("luck")), strength, ex), None


@app.route("/api/pbt/meta")
def api_pbt_meta():
    """전략 목록(워밍업·필요 데이터) + 프리셋 + 가격 데이터 범위 + 마지막 작업."""
    import period_backtest as P
    import strategy_engine as E
    cal = P.trading_calendar()
    live = getattr(E, "LIVE_MAP", {})
    import strategy_explain as X
    strategies = [{"name": s.name, "live": live.get(s.name, ""), "warmup": P.warmup_days(s),
                   "needs": [P._AUX_LABEL[k] for k in P.aux_needs(s)],
                   "desc": X.explain(s)} for s in E.ALL_STRATEGIES]
    top10 = []
    try:
        sf = get_strategy_full()
        rows = [r for r in sf.get("rows", []) if r.get("real_cagr_pct") is not None]
        top10 = [r["strategy"] for r in sorted(rows, key=lambda r: -float(r["real_cagr_pct"]))[:10]]
    except Exception:
        pass
    cov = None
    try:
        cov = json.load(open(P.COVERAGE_JSON, encoding="utf-8"))
    except Exception:
        pass
    jobs = sorted(_PBT_DIR.glob("pbt_*.json"), key=lambda p: p.stat().st_mtime) if _PBT_DIR.exists() else []
    p = _PBT_PROC["proc"]
    return jsonify({"strategies": strategies,
                    "presets": {"live": [s["name"] for s in strategies if s["live"]],
                                "top10": top10, "all": [s["name"] for s in strategies]},
                    "data": [cal[0], cal[-1]] if cal else None, "coverage": cov,
                    "common_rules": X.COMMON_RULES,
                    "last_job": jobs[-1].stem if jobs else None,
                    "running": _PBT_PROC["job"] if (p is not None and p.poll() is None) else None})


_STRAT_INFO_CACHE = {}


def _STRAT_BASE_INFO():
    """엔진 55전략 + 라이브 이름 별칭 + 옛 이름 설명(서버 기동 후 1회 생성·캐시). 운용 규칙은 build_strategy_info 가 붙인다."""
    if _STRAT_INFO_CACHE:
        return _STRAT_INFO_CACHE
    import strategy_engine as E
    import strategy_explain as X
    info = {}
    for s in list(E.ALL_STRATEGIES) + list(X.legacy_strategies().values()):
        d = X.explain(s)
        if d:
            info[s.name] = dict(d, engine=s.name)
    for live_name, eng in X.LIVE_ALIAS.items():
        if eng in info:
            info[live_name] = dict(info[eng], engine=eng)
    for n, note in X.RETIRED.items():
        if n in info:
            info[n] = dict(info[n], retired=note)
    info["probe_fill"] = {"title": "장전 탐침 체결분(전략 아님)", "idea": "08:5X 동시호가 주문이 받아지는지 시험한 주문이 실제로 체결된 것",
                          "buy": [], "sell": ["원장에 즉시 만기로 등록 — 다음 매도 시각에 정리"], "data": [], "caution": []}
    _STRAT_INFO_CACHE.update(info)
    return _STRAT_INFO_CACHE


def build_strategy_info():
    """전 탭 마우스오버 설명용 {이름: 설명} — 기본 설명 + **지금** 설정된 모의매매 규칙(계좌·슬롯·사는/파는 조건).
    (2026-10-19 신설, 10-21 설정 기능과 연결: 운용 규칙은 요청마다 live_config 에서)"""
    import live_config as LC
    info = {k: dict(v) for k, v in _STRAT_BASE_INFO().items()}
    for acct, lab in (("kiwoom", "키움"), ("kis", "KIS")):
        slots, labels, holds, entries, exits, stops = _live_tables(acct)
        for n, k in slots.items():
            if n not in info:
                info[n] = {"title": n, "buy": [], "sell": [], "data": [], "caution": []}
            info[n]["live"] = {"account": lab, "slots": k, "label": labels.get(n, n),
                               "entry": entries.get(n, ""), "exit": exits.get(n, ""),
                               "stop": stops.get(n) if stops.get(n) != "" else None}
            info[n].pop("retired", None)            # 다시 운용하면 '운용 종료' 표시는 뗀다
    return info


@app.route("/api/strategy_info")
def api_strategy_info():
    try:
        info = build_strategy_info()
        # 한글 표기(슬롯 라벨 등) → 전략 이름: 영문 이름이 안 보이는 칸에서도 설명이 뜨게
        al = {v: k for a in ("kiwoom", "kis") for k, v in _live_tables(a)[1].items() if k in info}
        al.update({v: k for k, v in _LIVE_BASE_LABEL.items() if k in info and v not in al})
        return jsonify({"ok": True, "info": info, "aliases": al})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {str(e)[:150]}", "info": {}})


@app.route("/api/pbt/check", methods=["POST"])
def api_pbt_check():
    if not _same_origin_action(request):
        return jsonify({"ok": False, "msg": "대시보드 페이지에서만 실행할 수 있습니다(CSRF 차단)"}), 403
    if not _is_local(request):
        return jsonify({"ok": False, "msg": "로컬에서만 허용"}), 403
    parsed, err = _pbt_parse(request)
    if err:
        return jsonify({"ok": False, "msg": err}), 400
    start, end, names, luck, strength, ex_arg = parsed
    st_arg = (["--strength", ",".join(f"{v:g}" for v in strength)] if strength else []) + ex_arg
    cmd = [_VENV_PYTHON, str(BASE / "period_backtest.py"), "--check", "--start", start,
           "--end", end, "--names", ",".join(names)] + (["--luck"] if luck else []) + st_arg
    try:
        r = _sp.run(cmd, cwd=str(BASE), capture_output=True, timeout=180, creationflags=_NO_WIN)
        out = r.stdout.decode("utf-8", errors="replace")
        line = next((l for l in out.splitlines() if l.startswith("PBT_CHECK ")), None)
        if not line:
            err_txt = r.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["출력 없음"]
            return jsonify({"ok": False, "msg": f"점검 실패: {err_txt[0][:150]}"}), 500
        d = json.loads(line[len("PBT_CHECK "):])
        d["msg"] = "점검 완료"
        return jsonify(d)
    except _sp.TimeoutExpired:
        return jsonify({"ok": False, "msg": "점검 시간 초과(180초)"}), 504
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:150]}), 500


@app.route("/api/pbt/run", methods=["POST"])
def api_pbt_run():
    if not _same_origin_action(request):
        return jsonify({"ok": False, "msg": "대시보드 페이지에서만 실행할 수 있습니다(CSRF 차단)"}), 403
    if not _is_local(request):
        return jsonify({"ok": False, "msg": "로컬에서만 허용(원격 실행 불가)"}), 403
    parsed, err = _pbt_parse(request)
    if err:
        return jsonify({"ok": False, "msg": err}), 400
    start, end, names, luck, strength, ex_arg = parsed
    st_arg = (["--strength", ",".join(f"{v:g}" for v in strength)] if strength else []) + ex_arg
    p = _PBT_PROC["proc"]
    if p is not None and p.poll() is None:
        return jsonify({"ok": False, "msg": f"이미 실행 중({_PBT_PROC['job']}) — 끝난 뒤 다시 시도",
                        "job": _PBT_PROC["job"]}), 409
    job = datetime.now().strftime("pbt_%Y%m%d_%H%M%S")
    cmd = [_VENV_PYTHON, str(BASE / "period_backtest.py"), "--start", start, "--end", end,
           "--names", ",".join(names), "--job", job] + (["--luck"] if luck else []) + st_arg
    try:
        (BASE / "logs").mkdir(exist_ok=True)
        logf = open(BASE / "logs" / "run_period_backtest.log", "w", encoding="utf-8", errors="replace")
        _PBT_PROC["proc"] = _sp.Popen(cmd, cwd=str(BASE), stdout=logf, stderr=_sp.STDOUT,
                                      creationflags=_NO_WIN)
        _PBT_PROC["job"] = job
        return jsonify({"ok": True, "job": job, "msg": f"시작됨 — {len(names)}전략"
                        + (f" × 강도 {len(strength) + 1}줄" if strength else "")})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:150]}), 500


# ── 9d. 모의매매 전략 설정 (2026-10-21) ─────────────────────────────────────────
# 계좌별 전략(최대 4)·슬롯(합 10)·강도 기준·전략별 강도 면제·손절·익절을 대시보드에서 바꾼다.
# 저장 = live_config.save(검증·백업·이력). 거래일 08:30~15:40 은 잠금. 화면은 두 번 확인하고, 서버는 confirm=2 를 요구.
# 정기 신호(18:30)가 지났거나 휴장일이면 저장 직후 regen_signals.py 로 오늘 신호를 다시 만든다 → 다음 거래일 09:00 부터 새 방식.
_LC_REGEN = {"proc": None}
_LAG_NOTE = {
    "credit": "신용잔고는 1~2거래일 늦게 공개 — 이 판은 실매매 신호가 안 나옴(백테스트는 당일 값을 써 낙관적). 실매매는 _lag2 / _wait2 판을 고르세요",
    "short": "공매도 비중은 다음날 공표 — 이 판은 실매매 신호가 안 나옴. 실매매는 _lag2 / _wait2 판을 고르세요",
    "individual": "개인 순매수는 18:30 신호 때 KRX 에서 바로 받아 씀(못 받으면 그날은 건너뜀)",
}


def _lc_engine_list():
    import strategy_engine as E
    import period_backtest as P
    import live_config as LC
    base = _STRAT_BASE_INFO()
    out = []
    for s in E.ALL_STRATEGIES:
        v = vars(s)
        hold = int(v.get("holding_days") or v.get("holding") or 20)
        needs = P.aux_needs(s)
        out.append({"engine": s.name, "live": LC.live_name(s.name), "title": (base.get(s.name) or {}).get("title", s.name),
                    "holding": hold, "needs": [P._AUX_LABEL[k] for k in needs],
                    "note": (f"공개 지연 {v.get('data_lag')}거래일 반영판 — 실매매에서도 신호가 나옴(백테스트와 같은 정보)"
                             if v.get("data_lag") else " / ".join(_LAG_NOTE[k] for k in needs if k in _LAG_NOTE))})
    return out


def _lc_view(cfg):
    import live_config as LC
    view = {"updated": cfg.get("updated"), "note": cfg.get("note"), "fallback": cfg.get("_fallback"), "accounts": {}}
    for a in LC.ACCOUNTS:
        A = cfg["accounts"][a]
        view["accounts"][a] = {"label": LC.ACCOUNT_LABEL[a], "min_strength": A["min_strength"],
                               "slot_mode": A.get("slot_mode", "slots"),
                               "strength_version": A.get("strength_version", "v1"),
                               "strategies": [dict(x, live=LC.live_name(x["engine"])) for x in A["strategies"]]}
    return view


@app.route("/api/live_config")
def api_live_config():
    import live_config as LC
    locked, why = LC.is_locked()
    hist = []
    try:
        with open(LC.history_path(), encoding="utf-8") as f:
            hist = [json.loads(l) for l in f.readlines()[-10:] if l.strip()][::-1]
    except Exception:
        pass
    p = _LC_REGEN["proc"]
    v2 = {}
    try:
        import strength_model as SM
        for f in (Path(SM.MODEL_DIR).glob("*.json") if Path(SM.MODEL_DIR).exists() else []):
            m = SM.load_model(f.stem)
            if m:
                v2[f.stem] = {"trained": m.get("trained"), "n": m.get("n"), "n_ic": len(m.get("ic", {}))}
    except Exception:
        pass
    return jsonify({"ok": True, "config": _lc_view(LC.load()), "engines": _lc_engine_list(), "v2_models": v2,
                    "locked": locked, "lock_msg": why, "regen_after_save": LC.needs_regen(),
                    "regen_running": bool(p is not None and p.poll() is None),
                    "slots_total": LC.SLOTS_TOTAL, "max_strategies": LC.MAX_STRATEGIES, "history": hist})


def _lc_from_request(b):
    """요청 본문 → 저장용 설정(보유일은 엔진 값으로 강제 — 화면 값은 믿지 않는다)."""
    import live_config as LC
    hold = {e["engine"]: e["holding"] for e in _lc_engine_list()}
    acc = (b or {}).get("accounts") or {}
    new = {"accounts": {}}
    for a in LC.ACCOUNTS:
        A = acc.get(a) or {}
        rows = []
        for x in (A.get("strategies") or [])[:LC.MAX_STRATEGIES + 1]:
            def num(v):
                if v is None or v == "":
                    return None
                return float(v)
            e = str(x.get("engine", ""))
            rows.append({"engine": e, "slots": int(x.get("slots") or 0), "exempt": bool(x.get("exempt")),
                         "stop": num(x.get("stop")), "take": num(x.get("take")), "holding": hold.get(e, 20),
                         "min_strength": num(x.get("min_strength"))})      # [§71] 전략별 강도 기준(비우면 계좌 기본)
        new["accounts"][a] = {"min_strength": float(A.get("min_strength", 5.7)),
                              "slot_mode": str(A.get("slot_mode") or "slots"),
                              "strength_version": str(A.get("strength_version") or "v1"), "strategies": rows}
    return new


@app.route("/api/live_config/preview", methods=["POST"])
def api_live_config_preview():
    if not _same_origin_action(request):
        return jsonify({"ok": False, "msg": "대시보드 페이지에서만 실행할 수 있습니다(CSRF 차단)"}), 403
    if not _is_local(request):
        return jsonify({"ok": False, "msg": "로컬에서만 허용"}), 403
    import live_config as LC
    try:
        new = _lc_from_request(request.get_json(silent=True))
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "errors": [f"입력 값 오류: {e}"]}), 400
    errs = LC.validate(new, {e["engine"] for e in _lc_engine_list()})
    cur = LC.load()
    changes = [] if errs else LC.diff(cur, new)
    locked, why = LC.is_locked()
    regen = LC.needs_regen()
    try:
        ov = [w["msg"] for w in _ov_check({a: [s["engine"] for s in new["accounts"][a]["strategies"]] for a in LC.ACCOUNTS})["warnings"]
              if w["level"] == "warn"]
    except Exception:
        ov = []
    return jsonify({"ok": not errs, "errors": errs, "changes": changes, "locked": locked, "lock_msg": why, "overlap": ov,
                    "effective": ("저장 즉시 오늘 신호를 새 설정으로 다시 만듭니다 → 다음 거래일 09:00 매수부터 새 방식"
                                  if regen else "오늘 18:30 정기 신호부터 새 설정 → 다음 거래일 09:00 매수부터 새 방식")})


@app.route("/api/live_config", methods=["POST"])
def api_live_config_save():
    if not _same_origin_action(request):
        return jsonify({"ok": False, "msg": "대시보드 페이지에서만 실행할 수 있습니다(CSRF 차단)"}), 403
    if not _is_local(request):
        return jsonify({"ok": False, "msg": "로컬에서만 허용(원격 변경 불가)"}), 403
    import live_config as LC
    b = request.get_json(silent=True) or {}
    if b.get("confirm") != 2:
        return jsonify({"ok": False, "msg": "두 번 확인이 필요합니다"}), 400
    locked, why = LC.is_locked()
    if locked:
        return jsonify({"ok": False, "msg": why}), 423
    try:
        new = _lc_from_request(b)
        cfg, changes = LC.save(new, note=str(b.get("note", ""))[:200],
                               engine_names={e["engine"] for e in _lc_engine_list()})
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "msg": str(e)[:400]}), 400
    regen = LC.needs_regen()
    msg = "저장됨 — 오늘 18:30 신호부터 새 설정, 다음 거래일 09:00 매수부터 적용"
    # 강도 v2 를 쓰는 계좌의 전략 중 모델이 없는 것은 학습(전략당 1~3분). 재생성이 필요하면 regen_signals 가 먼저 학습한다.
    try:
        import strength_model as SM
        v2_missing = sorted({s["engine"] for a in LC.ACCOUNTS if cfg["accounts"][a].get("strength_version") == "v2"
                             for s in cfg["accounts"][a]["strategies"] if not SM.load_model(s["engine"])})
    except Exception:
        v2_missing = []
    if v2_missing and not regen:
        (BASE / "logs").mkdir(exist_ok=True)
        _sp.Popen([_VENV_PYTHON, str(BASE / "strength_model.py"), "--train", ",".join(v2_missing)], cwd=str(BASE),
                  stdout=open(BASE / "logs" / "run_strength_v2_train.log", "w", encoding="utf-8", errors="replace"),
                  stderr=_sp.STDOUT, creationflags=_NO_WIN)
        msg += f" · 강도 v2 모델 학습 시작({', '.join(v2_missing)} — 끝나기 전 신호는 v1 점수로 대체)"
    if regen:
        p = _LC_REGEN["proc"]
        if p is None or p.poll() is not None:
            (BASE / "logs").mkdir(exist_ok=True)
            logf = open(BASE / "logs" / "run_regen_signals.log", "w", encoding="utf-8", errors="replace")
            _LC_REGEN["proc"] = _sp.Popen([_VENV_PYTHON, str(BASE / "regen_signals.py")], cwd=str(BASE),
                                          stdout=logf, stderr=_sp.STDOUT, creationflags=_NO_WIN)
        msg = "저장됨 — 오늘 신호를 새 설정으로 다시 만드는 중(1~3분) → 다음 거래일 09:00 매수부터 적용"
    try:
        import notifier
        notifier.safe_send("🛠 [모의매매 전략 설정 변경]\n" + ("\n".join("· " + c for c in changes) or "· 변경 없음")
                           + "\n" + msg)
    except Exception:
        pass
    return jsonify({"ok": True, "msg": msg, "changes": changes, "config": _lc_view(cfg), "regen": regen})


# ══ 2026-10-22 AI·강도 가상매매 전략 설정(§68) — 강도매매 탭. 실계좌와 별개 파일(db/virtual_config.json) ══
# 저장 = virtual_config.save(검증·백업·적용일 이력). 장중 잠금 없음(주문이 없는 장부 시뮬). 화면은 두 번 확인, 서버는 confirm=2.
def _vc_live_names():
    import live_config as LC
    out = set()
    for a in LC.ACCOUNTS:
        out |= set(LC.account(a)["priority"])
    return out


@app.route("/api/virtual_config")
def api_virtual_config():
    import live_config as LC
    import virtual_config as VC
    cfg = VC.load()
    hist = cfg.get("history") or []
    today = datetime.now().strftime("%Y%m%d")
    active, pending = VC.state(today)
    src = pending or (active if active and not active.get("legacy") else None)
    draft = (src or {}).get("portfolios") or VC.default_portfolios()
    v2 = {}
    try:
        import strength_model as SM
        for f in (Path(SM.MODEL_DIR).glob("*.json") if Path(SM.MODEL_DIR).exists() else []):
            m = SM.load_model(f.stem)
            if m:
                v2[f.stem] = {"trained": m.get("trained"), "n": m.get("n"), "n_ic": len(m.get("ic", {}))}
    except Exception:
        pass
    view = lambda e: None if not e else {"effective": e["effective"], "saved": e.get("saved"), "legacy": bool(e.get("legacy")),
                                         "desc": VC.describe_legacy(e) if e.get("legacy") else "",
                                         "portfolios": e.get("portfolios")}
    hist_v = [{"effective": e["effective"], "saved": e.get("saved"), "legacy": bool(e.get("legacy")),
               "desc": VC.describe_legacy(e) if e.get("legacy") else " / ".join(
                   f"{VC.LABEL[p]}: " + ", ".join(f"{s['engine']}({s['slots']})" for s in e["portfolios"][p]["strategies"])
                   for p in VC.PORTFOLIOS)} for e in sorted(hist, key=lambda x: x["effective"], reverse=True)[:8]]
    return jsonify({"ok": True, "active": view(active), "pending": view(pending), "draft": draft,
                    "fallback": cfg.get("_fallback"), "engines": _lc_engine_list(),
                    "live_names": sorted(_vc_live_names()), "aliases": LC.ALIAS, "v2_models": v2,
                    "effective": VC.effective_date(), "slots_total": VC.SLOTS_TOTAL,
                    "max_strategies": VC.MAX_STRATEGIES, "labels": VC.LABEL, "history": hist_v,
                    "legacy_now": VC.describe_legacy(VC.legacy_snapshot()) if not hist else ""})


def _vc_effective_msg(eff):
    return f"{eff[:4]}-{eff[4:6]}-{eff[6:]} 신호부터 새 설정 (그 전 기록·이미 산 종목은 종전 규칙 그대로)"


@app.route("/api/virtual_config/preview", methods=["POST"])
def api_virtual_config_preview():
    if not _same_origin_action(request):
        return jsonify({"ok": False, "msg": "대시보드 페이지에서만 실행할 수 있습니다(CSRF 차단)"}), 403
    if not _is_local(request):
        return jsonify({"ok": False, "msg": "로컬에서만 허용"}), 403
    import virtual_config as VC
    hold = {e["engine"]: e["holding"] for e in _lc_engine_list()}
    try:
        new = VC.clean((request.get_json(silent=True) or {}).get("portfolios"), hold)
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "errors": [f"입력 값 오류: {e}"]}), 400
    errs = VC.validate(new, set(hold))
    eff = VC.effective_date()
    changes = []
    if not errs:
        prev = VC.entry_for(VC.load().get("history") or [], eff)
        changes = VC.diff(None if (prev is None or prev.get("legacy")) else prev["portfolios"], new)
    try:
        ov = [w["msg"] for w in _ov_check({p: [s["engine"] for s in new[p]["strategies"]] for p in VC.PORTFOLIOS})["warnings"]
              if set(w["places"]) & set(VC.PORTFOLIOS)]
    except Exception:
        ov = []
    return jsonify({"ok": not errs, "errors": errs, "changes": changes, "effective": _vc_effective_msg(eff), "overlap": ov})


@app.route("/api/virtual_config", methods=["POST"])
def api_virtual_config_save():
    if not _same_origin_action(request):
        return jsonify({"ok": False, "msg": "대시보드 페이지에서만 실행할 수 있습니다(CSRF 차단)"}), 403
    if not _is_local(request):
        return jsonify({"ok": False, "msg": "로컬에서만 허용(원격 변경 불가)"}), 403
    import virtual_config as VC
    b = request.get_json(silent=True) or {}
    if b.get("confirm") != 2:
        return jsonify({"ok": False, "msg": "두 번 확인이 필요합니다"}), 400
    hold = {e["engine"]: e["holding"] for e in _lc_engine_list()}
    try:
        new = VC.clean(b.get("portfolios"), hold)
        cfg, changes, eff = VC.save(new, note=str(b.get("note", ""))[:200], engine_names=set(hold))
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "msg": str(e)[:400]}), 400
    msg = "저장됨 — " + _vc_effective_msg(eff)
    try:      # 강도 v2 를 고른 강도 포트의 전략 중 모델이 없는 것은 학습(전략당 1~3분, 없으면 v1 점수로 대체)
        import strength_model as SM
        miss = sorted({s["engine"] for s in new["strength"]["strategies"]
                       if new["strength"].get("strength_version") == "v2" and not SM.load_model(s["engine"])})
    except Exception:
        miss = []
    if miss:
        (BASE / "logs").mkdir(exist_ok=True)
        _sp.Popen([_VENV_PYTHON, str(BASE / "strength_model.py"), "--train", ",".join(miss)], cwd=str(BASE),
                  stdout=open(BASE / "logs" / "run_strength_v2_train.log", "w", encoding="utf-8", errors="replace"),
                  stderr=_sp.STDOUT, creationflags=_NO_WIN)
        msg += f" · 강도 v2 모델 학습 시작({', '.join(miss)})"
    try:
        import notifier
        notifier.safe_send("🧪 [AI·강도 가상매매 설정 변경]\n" + ("\n".join("· " + c for c in changes) or "· 변경 없음")
                           + "\n" + msg)
    except Exception:
        pass
    return jsonify({"ok": True, "msg": msg, "changes": changes})


# ══ 2026-10-23 전략 겹침 경고(§72) — 모의계좌·AI·강도 가상 설정 화면 공통. 표 = db/strategy_overlap.json(주간 갱신) ══
def _ov_saved_places():
    """저장된 설정 기준 4곳의 전략(엔진 이름) — 화면에서 한쪽만 편집 중일 때 다른 쪽 값으로 씀."""
    import live_config as LC
    import virtual_config as VC
    cfg = LC.load()
    out = {a: [s["engine"] for s in cfg["accounts"][a]["strategies"]] for a in LC.ACCOUNTS}
    act, pend = VC.state()
    e = pend or act
    if e and not e.get("legacy"):
        for p in VC.PORTFOLIOS:
            out[p] = [s["engine"] for s in e["portfolios"][p]["strategies"]]
    return out


def _ov_check(places):
    import strategy_overlap as SO
    m = SO.load()
    full = _ov_saved_places()
    full.update({k: list(v) for k, v in (places or {}).items() if k in SO.PLACES and isinstance(v, list)})
    return {"warnings": SO.warnings(full, m) if m else [], "available": bool(m),
            "generated": (m or {}).get("generated"), "window": (m or {}).get("window")}


@app.route("/api/strategy_overlap/check")
def api_strategy_overlap_check():
    """읽기 전용 — places(JSON: {kiwoom|kis|ai|strength: [엔진…]})에 없는 곳은 저장된 설정으로 채워 겹침 경고를 돌려준다."""
    try:
        places = json.loads(request.args.get("places") or "{}")
    except Exception:
        places = {}
    try:
        return jsonify({"ok": True, **_ov_check(places)})
    except Exception as e:
        return jsonify({"ok": False, "warnings": [], "available": False, "msg": str(e)[:200]})


@app.route("/api/pbt/jobs")
def api_pbt_jobs():
    """지난 기간 백테스트 기록(최신순) — 요약만. 전체 결과는 /api/pbt/status?job= 로 연다. (2026-10-19)"""
    out = []
    files = sorted(_PBT_DIR.glob("pbt_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if _PBT_DIR.exists() else []
    for p in files:
        if not _PBT_JOB_RE.fullmatch(p.stem):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue                                   # 쓰는 중이거나 깨진 파일
        prm = d.get("params") or {}
        rs = [r for r in d.get("results") or [] if r.get("cagr") is not None]
        base = [r for r in rs if r.get("strength_min") is None] or rs
        top = sorted(base, key=lambda r: -float(r["cagr"]))[:5]
        lk = d.get("luck") or {}
        out.append({
            "job": p.stem, "status": d.get("status"), "started": d.get("started"),
            "finished": d.get("finished"), "elapsed_sec": d.get("elapsed_sec"),
            "start": prm.get("start"), "end": prm.get("eff_end") or prm.get("end"),
            "names": prm.get("names") or [], "strength": prm.get("strength") or [], "luck": bool(prm.get("luck")),
            "stop": prm.get("stop"), "take": prm.get("take"), "exit_mode": prm.get("exit_mode"),
            "strength_version": prm.get("strength_version"),
            "market_cagr": (d.get("market") or {}).get("cagr"),
            "n_results": len(d.get("results") or []), "n_warn": len(d.get("warnings") or []),
            "error": d.get("error"),
            "top": [{"strategy": r["strategy"], "cagr": r.get("cagr"), "mdd": r.get("mdd"),
                     "sharpe": r.get("sharpe"), "n_trades": r.get("n_trades"), "total_ret": r.get("total_ret")}
                    for r in top],
            "luck_sum": {k: lk.get(k) for k in ("spa_cash", "pbo", "dsr", "best")} if "pbo" in lk else None,
        })
    return jsonify({"ok": True, "jobs": out})


@app.route("/api/pbt/status")
def api_pbt_status():
    job = request.args.get("job", "")
    if not _PBT_JOB_RE.fullmatch(job):
        return jsonify({"ok": False, "msg": "잘못된 작업 ID"}), 400
    path = _PBT_DIR / f"{job}.json"
    p = _PBT_PROC["proc"]
    alive = p is not None and p.poll() is None and _PBT_PROC["job"] == job
    if not path.exists():
        return jsonify({"ok": True, "job": job, "status": "starting" if alive else "missing"})
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return jsonify({"ok": True, "job": job, "status": "running"})   # 쓰는 중(원자적 교체 직전)
    if d.get("status") == "running" and not alive and _PBT_PROC["job"] == job and p is not None:
        d["status"] = "error"
        d["error"] = "실행 프로세스가 비정상 종료됨 — logs/run_period_backtest.log 확인"
    d["ok"] = True
    return jsonify(d)


# ── 9c. 부족한 데이터 수동 수집 (2026-10-18) ─────────────────────────────────────
# 테스트기 점검에서 '데이터 부족'이 나오면 그 구간을 버튼으로 수집한다.
# 실제 수집은 collect_aux.py(별도 프로세스)가 기존 수집기를 기간 인자로 부른다 — 명령은 그 표(KINDS)에서만 만든다.
# 동시 1건(KRX 스로틀·키움 키 공유). 상태는 db/period_bt/collect_{kind}.json.
_COLLECT_PROC = {"proc": None, "kind": None}
_COLLECT_STALE_SEC = 90          # 실행기가 5초마다 상태를 쓴다 — 이보다 오래 안 바뀌면 중단된 것


def _collect_state(C, kind):
    path = Path(C.state_path(kind))
    if not path.exists():
        return None
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "running"}                    # 원자적 교체 직전
    p = _COLLECT_PROC["proc"]
    alive = p is not None and p.poll() is None and _COLLECT_PROC["kind"] == kind
    if st.get("status") == "running" and not alive and _time.time() - path.stat().st_mtime > _COLLECT_STALE_SEC:
        st["status"] = "stale"
        st["error"] = f"실행기가 멈췄습니다(대시보드 재시작·PC 종료 등) — logs/collect_{kind}.log 확인 후 다시 수집"
    return st


@app.route("/api/pbt/collect_status")
def api_pbt_collect_status():
    import collect_aux as C
    kinds = [{"kind": k, "label": v["label"], "src": v["src"], "eta": v["eta"], "range": v["range"],
              "aux": v["aux"], "state": _collect_state(C, k)} for k, v in C.KINDS.items()]
    cov = None
    try:
        import period_backtest as P
        cov = json.load(open(P.COVERAGE_JSON, encoding="utf-8")).get("items")
    except Exception:
        pass
    p = _COLLECT_PROC["proc"]
    running = _COLLECT_PROC["kind"] if (p is not None and p.poll() is None) else None
    if not running:                       # collect_all.bat 처럼 대시보드 밖에서 돈 수집
        try:
            if _time.time() - os.path.getmtime(C.LOCK_PATH) < C.LOCK_STALE_SEC:
                running = "external"
        except OSError:
            pass
    return jsonify({"ok": True, "kinds": kinds, "aux_to_kind": C.AUX_TO_KIND, "coverage": cov,
                    "all": _collect_state(C, "all"), "plan_note": f"개인·공매도 = 가격 데이터 시작일부터, "
                    f"신용 = {C.CREDIT_SINCE} 부터 · 빈 날만", "running": running})


@app.route("/api/pbt/collect", methods=["POST"])
def api_pbt_collect():
    if not _same_origin_action(request):
        return jsonify({"ok": False, "msg": "대시보드 페이지에서만 실행할 수 있습니다(CSRF 차단)"}), 403
    if not _is_local(request):
        return jsonify({"ok": False, "msg": "로컬에서만 허용(원격 실행 불가)"}), 403
    import collect_aux as C
    b = request.get_json(silent=True) or {}
    kind, start, end = str(b.get("kind", "")), str(b.get("start", "")), str(b.get("end", ""))
    if kind != "all" and kind not in C.KINDS:
        return jsonify({"ok": False, "msg": f"알 수 없는 수집 종류: {kind[:30]}"}), 400
    if kind != "all" and (not (_DATE_RE.fullmatch(start) and _DATE_RE.fullmatch(end)) or start > end):
        return jsonify({"ok": False, "msg": "날짜는 YYYYMMDD 8자리, 시작 ≤ 종료"}), 400
    p = _COLLECT_PROC["proc"]
    if p is not None and p.poll() is None:
        return jsonify({"ok": False, "msg": f"이미 수집 중({_COLLECT_PROC['kind']}) — 끝난 뒤 다시 시도",
                        "kind": _COLLECT_PROC["kind"]}), 409
    # collect_all.bat 로 돌고 있는 수집도 막는다 — 실행기가 5초마다 자물쇠 파일을 갱신
    try:
        if _time.time() - os.path.getmtime(C.LOCK_PATH) < C.LOCK_STALE_SEC:
            return jsonify({"ok": False, "msg": "다른 수집(collect_all.bat 등)이 실행 중 — 끝난 뒤 다시 시도"}), 409
    except OSError:
        pass
    cmd = [_VENV_PYTHON, str(BASE / "collect_aux.py"), "--kind", kind]
    if kind != "all":
        cmd += ["--start", start, "--end", end]
    try:
        (BASE / "logs").mkdir(exist_ok=True)
        logf = open(BASE / "logs" / "run_collect_aux.log", "w", encoding="utf-8", errors="replace")
        _COLLECT_PROC["proc"] = _sp.Popen(cmd, cwd=str(BASE), stdout=logf, stderr=_sp.STDOUT,
                                          creationflags=_NO_WIN)
        _COLLECT_PROC["kind"] = kind
        lab = "부족분 한 번에" if kind == "all" else C.KINDS[kind]["label"]
        return jsonify({"ok": True, "kind": kind, "msg": f"{lab} 수집 시작"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:150]}), 500


# ── 10. HTML ───────────────────────────────────────────────────────────────────

HTML = """\
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>천억이 대시보드 v4</title>
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
*{box-sizing:border-box;margin:0;padding:0;font-family:'Pretendard',system-ui,sans-serif;}
body{background:#0b0f19;color:#e2e8f0;font-size:14px;line-height:1.5;}
a{color:#60a5fa;text-decoration:none;transition:color .2s}
a:hover{color:#93c5fd}
/* 제어판 버튼 */
.runbtn{background:#1e293b;border:1px solid #334155;color:#f8fafc;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;transition:all .2s;box-shadow:0 1px 2px rgba(0,0,0,.1)}
.runbtn:hover{background:#2dd4bf;border-color:#14b8a6;color:#042f2e;transform:translateY(-1px)}
.runbtn.danger{border-color:#7f1d1d;color:#fca5a5}
.runbtn.danger:hover{background:#ef4444;border-color:#b91c1c;color:#fff}
.runbtn.stale{background:#450a0a;border-color:#dc2626;color:#fca5a5;animation:stalepulse 1.6s ease-in-out infinite}
.runbtn.stale::before{content:"\\26A0 ";color:#ef4444}
@keyframes stalepulse{0%,100%{box-shadow:0 0 2px 0 #dc2626}50%{box-shadow:0 0 10px 2px #dc2626}}
/* header */
header{background:rgba(15,23,42,.85);backdrop-filter:blur(8px);padding:12px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1e293b;position:sticky;top:0;z-index:20;box-shadow:0 4px 6px -1px rgba(0,0,0,.1)}
header h1{font-size:18px;font-weight:700;letter-spacing:-.02em;color:#f8fafc}
#ts{font-size:12px;color:#94a3b8}
button#btn-refresh{background:#3b82f6;border:none;color:#fff;padding:6px 14px;border-radius:6px;font-weight:600;transition:background .2s;cursor:pointer}
button#btn-refresh:hover{background:#2563eb}
/* freshness banner */
#freshness-banner{padding:6px 20px;font-size:13px;font-weight:500}
.fresh-ok{color:#10b981}.fresh-warn{color:#f59e0b}.fresh-err{color:#ef4444;font-weight:700}
/* tabs */
.tabs{display:flex;gap:8px;border-bottom:1px solid #1e293b;background:#0f172a;padding:8px 20px 0}
.tab{padding:10px 16px;cursor:pointer;font-size:14px;font-weight:600;color:#64748b;border-bottom:3px solid transparent;border-radius:6px 6px 0 0;transition:all .2s;margin-bottom:-1px;white-space:nowrap}
.tab:hover{color:#e2e8f0;background:#1e293b}
.tab.active{color:#38bdf8;border-bottom-color:#38bdf8;background:#1e293b}
/* tab content */
.tab-content{display:none;padding:24px 20px;animation:fadeIn .3s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
.tab-content.active{display:block}
/* cards / sections */
.section{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 4px 6px -1px rgba(0,0,0,.1)}
.section h2{font-size:14px;font-weight:700;color:#9ca3af;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px;display:flex;align-items:center}
/* stat boxes */
.stats-row{display:flex;flex-wrap:wrap;gap:16px}
.stat{background:#1f2937;border:1px solid #374151;border-radius:10px;padding:16px;min-width:130px;flex:1;box-shadow:inset 0 1px 0 rgba(255,255,255,.05);transition:transform .2s}
.stat:hover{transform:translateY(-2px)}
.stat .v{font-size:24px;font-weight:800;letter-spacing:-.02em;color:#f8fafc;line-height:1.2}
.stat .l{font-size:12px;font-weight:500;color:#9ca3af;margin-top:6px;text-transform:uppercase;letter-spacing:.5px}
/* colors */
.pos{color:#10b981}.neg{color:#ef4444}.neu{color:#cbd5e1}
/* tables */
table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px}
th{text-align:left;color:#9ca3af;font-weight:600;padding:10px 12px;border-bottom:2px solid #374151;background:#111827;position:sticky;top:0;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
td{padding:12px;border-bottom:1px solid #1f2937;color:#e2e8f0;transition:background .2s}
tbody tr:hover td{background:#1f2937;box-shadow:inset 2px 0 0 #38bdf8}
.r{text-align:right}
/* 모의투자 슬롯 아코디언 */
.slotitem{background:#1f2937;border:1px solid #374151;border-radius:8px;margin-bottom:8px;overflow:hidden;transition:all .2s;width:100%}
.slotitem:hover{border-color:#4b5563}
.slothead{display:flex;align-items:center;gap:12px;padding:12px 16px;cursor:pointer;user-select:none;background:#111827}
.slothead:hover{background:#1f2937}
.slotname{flex:1;font-weight:700;color:#e2e8f0}
.caret{color:#9ca3af;font-size:14px;transition:transform .2s}
.slotbody{display:none;padding:0 16px 16px;font-size:13px;color:#cbd5e1;line-height:1.7;border-top:1px solid #374151;background:#111827}
.slotbody.open{display:block;padding-top:12px}
/* 로그 드롭다운 */
details.logdrop>summary{cursor:pointer;font-size:14px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:1px;list-style:none;padding:6px 0;transition:color .2s}
details.logdrop>summary:hover{color:#e2e8f0}
details.logdrop>summary::-webkit-details-marker{display:none}
details.logdrop>summary::before{content:"\\25B8  ";color:#38bdf8;margin-right:6px}
details.logdrop[open]>summary::before{content:"\\25BE  "}
/* 정렬 가능한 표 헤더 */
th.sortable{cursor:pointer;user-select:none;transition:color .2s}
th.sortable:hover{color:#38bdf8}
/* 표 필터(2026-08-09) — initTableFilter 가 표 위에 자동 삽입 */
.tblfilter-wrap{display:flex;align-items:center;gap:8px;margin:2px 0 8px}
input.tblfilter{flex:0 1 280px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;
  border-radius:6px;padding:5px 10px;font-size:12px;font-family:inherit}
input.tblfilter:focus{outline:none;border-color:#38bdf8}
.tblfilter-cnt{font-size:11px;color:#64748b}
.tblfilter-cnt.hit{color:#38bdf8}
/* log pre */
pre.logbox{background:#0b0f19;border:1px solid #1f2937;border-radius:8px;padding:16px;font-size:12px;font-family:'JetBrains Mono',monospace;line-height:1.6;white-space:pre-wrap;word-break:break-all;max-height:480px;overflow-y:auto;color:#a1a1aa;box-shadow:inset 0 2px 4px rgba(0,0,0,.2)}
/* file table */
.file-ok{color:#10b981}.file-warn{color:#f59e0b}.file-err{color:#ef4444}
/* error box */
.err-box{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:12px 16px;color:#fca5a5;font-size:13px;margin:8px 0;display:flex;align-items:center;gap:8px}
.err-box::before{content:"\\26A0";font-size:16px}
/* intraday */
.intra-price{font-size:22px;font-weight:800;letter-spacing:-.02em}
/* custom scrollbar */
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:#0f172a}
::-webkit-scrollbar-thumb{background:#334155;border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#475569}

/* 2026-10-19 섹션 접기/펴기 + 전략 마우스오버 설명 */
.section > .sec-head{cursor:pointer;user-select:none}
.section > .sec-head .sec-caret{display:inline-block;width:16px;color:#64748b;font-size:12px;transition:transform .15s}
.section.sec-collapsed > .sec-head .sec-caret{transform:rotate(-90deg)}
.section.sec-collapsed > :not(.sec-head){display:none !important}
.section.sec-collapsed{padding-top:12px;padding-bottom:12px}
.section.sec-collapsed > .sec-head{margin-bottom:0}
.sec-bar{display:flex;justify-content:flex-end;gap:6px;margin:-6px 0 8px}
.sec-bar button{background:#1f2937;color:#cbd5e1;border:1px solid #334155;border-radius:6px;padding:2px 10px;font-size:12px;cursor:pointer}
.strat-hot{text-decoration:underline dotted #60a5fa;text-underline-offset:3px;cursor:help}
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
  <div class="tab"         onclick="showTab('cutmon')">&#x1F9EA; &#xC784;&#xACC4;&#xBAA8;&#xB2C8;&#xD130;</div>
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
  <div class="section" id="lc-mock-sec">
    <h2>🛠 모의매매 전략 설정 <span style="color:#8b949e;font-size:11px;font-weight:400">— 전략·슬롯·강도·손절·익절을 골라 다음 거래일부터 이 방식으로 매매 (두 번 확인)</span></h2>
    <div id="lc-mock"><span style="color:#64748b;font-size:12px">불러오는 중…</span></div>
  </div>
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
      <th>종목</th><th class="r">매수시간</th><th class="r">매도시각</th><th>전략</th><th class="r">강도</th>
      <th class="r">수량</th><th class="r">매수가격</th><th class="r">매도가격</th><th class="r">손익(원)</th><th class="r">손익률</th><th>사유</th>
    </tr></thead>
    <tbody id="mock-orders"><tr><td colspan="11" style="color:#8b949e;text-align:center">매매내역 없음</td></tr></tbody>
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
      <th>종목</th><th class="r">매수시간</th><th class="r">매도시각</th><th>전략</th><th class="r">강도</th>
      <th class="r">수량</th><th class="r">매수가격</th><th class="r">매도가격</th><th class="r">손익(원)</th><th class="r">손익률</th><th>사유</th>
    </tr></thead>
    <tbody id="kis-orders"><tr><td colspan="11" style="color:#8b949e;text-align:center">매매내역 없음</td></tr></tbody>
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
  <div class="section" id="vc-strength-sec">
    <h2>🧪 AI·강도 가상매매 전략 설정 <span style="color:#8b949e;font-size:11px;font-weight:400">— 아래 '가상매매 실행' 두 포트폴리오의 전략·슬롯·기준·손절·익절을 골라 다음 거래일 신호부터 적용 (두 번 확인 · 실계좌 무관)</span></h2>
    <div id="vc-strength"><span style="color:#64748b;font-size:12px">불러오는 중…</span></div>
  </div>
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

<!-- ===== 임계 모니터 (Phase 1, 모니터 전용) ===== -->
<div id="tab-cutmon" class="tab-content">
  <div class="section">
    <h2>&#xC804;&#xB7B5;&#xBCC4; &#xC784;&#xACC4; &#xBAA8;&#xB2C8;&#xD130; <span style="color:#f0883e;font-size:12px;font-weight:700">&mdash; &#xBAA8;&#xB2C8;&#xD130; &#xC804;&#xC6A9; &middot; &#xC2E4;&#xAC70;&#xB798;&#xC5D0; &#xAC1C;&#xC785;&#xD558;&#xC9C0; &#xC54A;&#xC74C;</span></h2>
    <div style="color:#8b949e;font-size:12px;margin-bottom:10px;line-height:1.7">
      &#xD604;&#xD589;&#xC740; <b>&#xAC15;&#xB3C4; 5.7 &#xB2E8;&#xC77C; &#xC808;&#xB300;&#xC784;&#xACC4;</b>&#xB2E4;. &#xADF8;&#xB7F0;&#xB370; &#xD559;&#xC2B5;&#xBD84;&#xD3EC; &#xC0C1;&#xC704; 30% &#xCEF7;&#xC774;
      high_52w_filt 3.16 ~ rsi_reversal 6.83 &#xB85C; <b>3.7&#xC810;</b> &#xBC8C;&#xC5B4;&#xC838; &#xC788;&#xC5B4;,
      5.7 &#xD558;&#xB098;&#xB294; &#xBAA8;&#xBA58;&#xD140; 4&#xC804;&#xB7B5;&#xC744; &#xD1B5;&#xACFC;&#xC728; 0%&#xB85C; &#xB044;&#xBA74;&#xC11C; rsi_reversal &#xC740; &#xC624;&#xD788;&#xB824; &#xB290;&#xC2AC;&#xAC8C; &#xD1B5;&#xACFC;&#xC2DC;&#xD0A8;&#xB2E4;.
      &#xC5EC;&#xAE30;&#xC11C;&#xB294; <b>&#xC804;&#xB7B5;&#xBCC4;&#xB85C; &#xB208;&#xAE08;&#xC744; &#xB9DE;&#xCD98; &#xC784;&#xACC4;</b>&#xC600;&#xB2E4;&#xBA74; &#xBB34;&#xC5C7;&#xC744; &#xC0C0;&#xC744;&#xC9C0;&#xB9CC; &#xAE30;&#xB85D;&#xD55C;&#xB2E4;.
      &#xB9E4;&#xB9E4; &#xCF54;&#xB4DC;&#xB294; &#xAC74;&#xB4DC;&#xB9AC;&#xC9C0; &#xC54A;&#xC558;&#xB2E4; &mdash; &#xC21C;&#xC218; &#xC9D1;&#xACC4;&#xB2E4;. (&#xC2DC;&#xC2A4;&#xD15C;_&#xC124;&#xBA85;&#xC11C; &sect;39)
      <br>&#xD310;&#xB2E8; &#xAE30;&#xC900;: <b>&#xD3C9;&#xADE0; net% &#xC640; &#xB204;&#xC801;&#xC744; &#xBD84;&#xB9AC;&#xD574; &#xBCF8;&#xB2E4;.</b> &#xC2B9;&#xB960;&#xC740; &#xC775;&#xC808;&#xADDC;&#xCE59;&#xC758; &#xC0B0;&#xBB3C;&#xC774;&#xB77C; &#xADFC;&#xAC70;&#xB85C; &#xC4F0;&#xC9C0; &#xC54A;&#xB294;&#xB2E4;.
    </div>
    <div style="color:#8b949e;font-size:12px" id="cm-meta"></div>
  </div>
  <div class="section">
    <h2>&#xADDC;&#xCE59;&#xBCC4; &#xBE44;&#xAD50;</h2>
    <table><thead><tr>
      <th>&#xADDC;&#xCE59;</th><th class="r">&#xB9E4;&#xC218;</th><th class="r">&#xC644;&#xB8CC;</th><th class="r">&#xBCF4;&#xC720;&#xC911;</th>
      <th class="r">&#xD3C9;&#xADE0; net%</th><th class="r">&#xC911;&#xC559;</th><th class="r">&#xB204;&#xC801;(10&#xBD84;&#xD560;)</th>
      <th class="r">&#xCD5C;&#xC545;</th><th class="r">&#xD0A4;&#xC6C0;</th><th class="r">KIS</th><th class="r">&#xC804;&#xB7B5;&#xC218;</th>
    </tr></thead>
    <tbody id="cm-rules"><tr><td colspan="11" style="color:#8b949e;text-align:center">&#xC9D1;&#xACC4; &#xC5C6;&#xC74C;</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h2>&#xADDC;&#xCE59;&times;&#xC804;&#xB7B5; &#xC131;&#xACFC; <span style="color:#8b949e;font-size:12px;font-weight:400">&mdash; &#xC644;&#xB8CC; &#xB9E4;&#xB9E4;&#xB9CC;</span></h2>
    <div id="cm-bystrat" style="color:#8b949e;font-size:13px">&#xB370;&#xC774;&#xD130; &#xC5C6;&#xC74C;</div>
  </div>
  <div class="section">
    <h2>&#xC77C;&#xC790;&#xBCC4; &#xB9E4;&#xC218; &#xAC74;&#xC218; <span style="color:#8b949e;font-size:12px;font-weight:400">&mdash; &#xCD5C;&#xADFC; 30&#xC77C;</span></h2>
    <div id="cm-daily" style="color:#8b949e;font-size:13px">&#xB370;&#xC774;&#xD130; &#xC5C6;&#xC74C;</div>
  </div>
  <div class="section">
    <h2>&#xAC00;&#xC0C1; &#xB9E4;&#xB9E4; &#xB0B4;&#xC5ED; <span style="color:#8b949e;font-size:12px;font-weight:400">&mdash; &#xCD5C;&#xADFC; 200&#xAC74;</span></h2>
    <table><thead><tr>
      <th>&#xADDC;&#xCE59;</th><th class="r">&#xC2E0;&#xD638;&#xC77C;</th><th class="r">&#xC9C4;&#xC785;&#xC77C;</th><th>&#xACC4;&#xC88C;</th><th>&#xC804;&#xB7B5;</th>
      <th>&#xC885;&#xBAA9;</th><th class="r">&#xAC15;&#xB3C4;</th><th class="r">&#xC9C4;&#xC785;&#xAC00;</th><th class="r">&#xCCAD;&#xC0B0;&#xC77C;</th>
      <th class="r">net%</th><th>&#xC0AC;&#xC720;</th>
    </tr></thead>
    <tbody id="cm-rows"><tr><td colspan="11" style="color:#8b949e;text-align:center">&#xB0B4;&#xC5ED; &#xC5C6;&#xC74C;</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ===== 백테스트 ===== -->
<div id="tab-backtest" class="tab-content">
  <div class="section" id="pbt-sec">
    <h2>기간 백테스트 테스트기 <span style="color:#8b949e;font-size:11px;font-weight:400">— 기간·전략을 골라 바로 계산 · 전략마다 10슬롯 단독 · 데이터 부족은 실행 전 점검(대시보드+텔레그램)</span></h2>
    <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px">
      <label style="font-size:13px;color:#cbd5e1">시작 <input type="date" id="pbt-start" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 6px"></label>
      <label style="font-size:13px;color:#cbd5e1">종료 <input type="date" id="pbt-end" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 6px"></label>
      <span style="font-size:12px;color:#64748b" id="pbt-range"></span>
      <span style="flex:1"></span>
      <label style="font-size:13px;color:#cbd5e1"><span class="pbt-tip" data-tip="강도 비교(선택)|쉼표로 여러 값: 예 4, 5, 5.7 → 전략마다 [강도 없음] + [≥4] [≥5] [≥5.7] 줄이 나란히 나옴|강도 줄은 그날 후보를 강도 높은 순으로 담음(모의매매와 같은 규칙) · 0 = 거르지 않고 강도순으로만 담기|강도는 신호마다 그날 데이터로 다시 계산 — 가중치는 해마다 그 이전 매매로만 학습(미래 미사용)|처음 한 번은 수 분, 같은 날 다시 돌리면 저장분 사용">강도 비교</span>
        <input type="text" id="pbt-strength" placeholder="예: 4, 5, 5.7" style="width:110px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 6px"></label>
      <select id="pbt-sver" title="강도 방식 — v1: 라이브 6전략 공통 가중치(종전) / v2: 전략마다 자기 과거 매매로 학습(§62)" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 6px;font-size:12px">
        <option value="v1">강도 v1(공통)</option><option value="v2">강도 v2(전략별)</option></select>
      <label style="font-size:13px;color:#cbd5e1"><span class="pbt-tip" data-tip="손절·익절(선택)|비우면 없음(만기에만 팖) · 모든 전략·강도 줄에 같은 규칙|손절은 음수(예 -10), 익절은 양수(예 20) — 진입가(다음날 시가) 기준|판정 '종가 기준' = 그날 종가가 선을 넘으면 다음날 시가에 팖(KIS 모의매매 손절과 같음, 보수적)|'장중 도달' = 그날 저가·고가가 닿으면 그 가격에 팖(키움 익절 지정가와 비슷, 시가부터 넘어 열리면 시가)">손절 %</span>
        <input type="number" id="pbt-stop" placeholder="없음" step="1" min="-50" max="-1" style="width:66px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 6px"></label>
      <label style="font-size:13px;color:#cbd5e1">익절 %
        <input type="number" id="pbt-take" placeholder="없음" step="1" min="1" max="300" style="width:66px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 6px"></label>
      <select id="pbt-exitmode" title="손절·익절 판정 방식" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 6px;font-size:12px">
        <option value="close">종가 기준(다음날 시가)</option><option value="intraday">장중 도달(그 가격)</option></select>
      <label style="font-size:13px;color:#cbd5e1" title="전략 5개 이상·거래일 80일 이상일 때 SPA·PBO·DSR 수행(1~2분 추가)"><input type="checkbox" id="pbt-luck"> 운 검정(SPA·PBO)</label>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px">
      <span style="font-size:12px;color:#94a3b8">전략:</span>
      <button class="runbtn" id="pbt-live-btn" onclick="pbtSel('live')">★ 라이브</button>
      <button class="runbtn" onclick="pbtSel('top10')">상위 10개</button>
      <button class="runbtn" onclick="pbtSel('all')">전체</button>
      <button class="runbtn" onclick="pbtSel('none')">선택 해제</button>
      <span id="pbt-count" style="font-size:12px;color:#94a3b8"></span>
      <span style="flex:1"></span>
      <button class="runbtn" onclick="pbtCheck()">데이터 점검</button>
      <button class="runbtn" style="background:#2563eb;color:#fff" onclick="pbtRun()">실행</button>
    </div>
    <div id="pbt-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:2px 10px;max-height:190px;overflow:auto;padding:8px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;font-size:12px"></div>
    <details id="pbt-colbox" style="margin-top:10px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:6px 10px" ontoggle="if(this.open) pbtColLoad()">
      <summary style="cursor:pointer;font-size:13px;color:#cbd5e1">&#x1F4E5; 데이터 현황 · 수동 수집 <span id="pbt-colsum" style="color:#64748b;font-size:12px"></span></summary>
      <div id="pbt-col" style="margin-top:8px;overflow-x:auto"></div>
    </details>
    <details id="pbt-histbox" style="margin-top:8px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:6px 10px" ontoggle="if(this.open) pbtHistLoad()">
      <summary style="cursor:pointer;font-size:13px;color:#cbd5e1">&#x1F4C2; 지난 계산 기록 <span id="pbt-histsum" style="color:#64748b;font-size:12px"></span></summary>
      <div id="pbt-hist" style="margin-top:8px"></div>
    </details>
    <div id="pbt-warn" style="margin-top:10px"></div>
    <div id="pbt-prog" style="margin-top:8px;font-size:12px;color:#94a3b8"></div>
    <div id="pbt-res" style="margin-top:8px;overflow-x:auto"></div>
    <div id="pbt-luckbox" style="margin-top:10px"></div>
  </div>
  <div class="section">
    <h2>&#xBC31;&#xD14C;&#xC2A4;&#xD2B8; &#xC694;&#xC57D; &mdash; <span id="bt-src" style="color:#58a6ff;font-weight:400"></span></h2>
    <div class="stats-row" id="bt-stats">
      <div style="color:#8b949e;font-size:13px">&#xB370;&#xC774;&#xD130; &#xB85C;&#xB529;&#xC911;...</div>
    </div>
  </div>
  <div class="section">
    <h2>전체 전략 과거 백테스트 — 10슬롯 자본곡선 기준 <span id="sfull-meta" style="color:#8b949e;font-size:11px;font-weight:400"></span>
      <button class="runbtn" style="margin-left:8px" onclick="runTask('strategy_full','전체 전략 백테스트')">다시 계산(20~30분)</button></h2>
    <div style="font-size:12px;color:#94a3b8;margin:4px 0 10px;line-height:1.6">
      <b>신호 평균%</b> = 신호가 뜬 매매 전체의 평균 · <b>슬롯 실매매%</b> = 10슬롯이 실제로 산 매매(거래대금 큰 순)의 평균 —
      두 값이 다르면 자본곡선은 슬롯 쪽을 따른다(예: rsi_reversal 신호 +0.71% / 실매매 −1.62% → 자본 −100%).
      CAGR·MDD·샤프는 일별 평가(MTM) 자본곡선. <b>최근2년</b> = 표본외 구간(과거에만 좋았던 전략 판별).<br>
      ⚠ 엔진 가정: 한 전략이 10슬롯을 단독 사용, 보유일 만기 청산(라이브의 KIS 손절·키움 익절 미반영), 비용 왕복 ≈0.33%(매도 연도 실제 거래세).
      라이브 포트(전략 혼합 4/4/2·강도 정렬)와 같지 않다 — 전략 간 '상대 비교'용. ★ = 라이브 운용 중. 매주 일요일 AI 파이프라인 뒤 자동 갱신.
    </div>
    <div style="overflow-x:auto"><table><thead><tr>
      <th>전략</th><th class="r">매매수</th><th class="r">승률</th><th class="r">신호 평균%</th>
      <th class="r">슬롯 실매매%</th><th class="r">CAGR%</th><th class="r">MDD%</th><th class="r">샤프</th>
      <th class="r">과거 평균%</th><th class="r">최근2년 평균%</th><th class="r">최근2년 CAGR%</th><th class="r">보유일</th>
    </tr></thead>
    <tbody id="sfull-rows"><tr><td colspan="12" style="color:#8b949e;text-align:center">'다시 계산' 버튼 또는 python strategy_engine.py --full</td></tr></tbody>
    </table></div>
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
        <button class="runbtn" data-fresh="program_trading" onclick="runTask('program','프로그램매매 수집(최근 30일 공백)')">프로그램매매</button>
        <button class="runbtn" data-fresh="korea_indicators" onclick="runTask('indicators','기술지표 재계산(45~90분)')">기술지표</button>
        <button class="runbtn" data-fresh="foreign_ratio" onclick="runTask('foreign_ratio','외국인 지분율 수집')">외국인 지분율</button>
        <button class="runbtn" data-fresh="news" onclick="runTask('news','뉴스/공시 수집')">뉴스/공시</button>
        <button class="runbtn" onclick="runTask('after_market','시간외 등락률 수집')">시간외</button>
        <button class="runbtn" onclick="runTask('recheck','데이터 재검증')">재검증</button>
        <button class="runbtn" title="백테스트용 보조 데이터(가격·외국인·기관 → 개인 순매수 → 공매도 → 신용) 빈 날만, 끝나면 텔레그램 보고 — 백테스트 탭의 '부족분 한 번에 수집'과 같음" onclick="pbtCollect('all')">보조데이터 부족분</button>
      </div>
      <div style="color:#64748b;font-size:11px;margin:-2px 0 2px 78px">증권사 키를 쓰는 수집(전체·수급·신용/대차·프로그램매매·시간외·재검증)은 거래일 08:40~09:25·15:00~15:45 엔 막힙니다(자동매매와 키 충돌)</div>
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
      <select id="logfile-select" onchange="loadLogFile()" style="margin-left:8px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:6px 10px;font-size:13px"></select>
      <button class="runbtn" style="padding:6px 12px; margin-left: 4px;" onclick="refreshLogFiles()">목록 새로고침</button>
      <button class="runbtn" style="padding:6px 12px; margin-left: 4px;" onclick="loadLogFile()">불러오기</button>
      <span id="logfile-info" style="color:#8b949e;font-size:12px;margin-left:8px"></span>
    </h2>
    <pre class="logbox" id="logfile-content" style="max-height:600px; margin-top: 10px;">파일을 선택하세요.</pre>
  </div>
</div>

<!-- ===== 장중시황 ===== -->
<div id="tab-intraday" class="tab-content">
  <div class="section">
    <h2>&#xC7A5;&#xC911; &#xC2DC;&#xD669; &mdash; <span id="intra-ts" style="color:#8b949e;font-weight:400"></span>
      <button onclick="refreshIntraday()" style="margin-left:8px;font-size:12px;padding:4px 10px;
        background:#1e293b;border:1px solid #334155;color:#f8fafc;border-radius:6px;cursor:pointer">&#x21BB; &#xAC31;&#xC2E0;</button>
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
      <span style="color:#f59e0b;font-size:12px;font-weight:500;margin-left:8px">※ 잠정(종가에 바뀔 수 있음)·정보용, 매매 아님</span>
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
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',['overview','cheonok','mock','strength','cutmon','backtest','aimodel','datalogs','intraday'][i]===id));
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.toggle('active',t.id==='tab-'+id));
  if(id==='backtest' && typeof pbtInit==='function') pbtInit();   // 기간 백테스트 테스트기(2026-10-18) — 처음 열 때 1회 로드
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
  // candidates table
  const rows=(cand&&cand.new_signals||cand&&cand.signals||[]);
  setHtml('ov-cand', rows.length?rows.map(r=>
    `<tr><td style="font-weight: 500;">${r.code||''}</td><td>${r.name||''}</td><td style="color:#9ca3af">${r.strategy||''}</td>
     <td class="r" style="color:#9ca3af">${String(r.signal_date||'').slice(0,8)}</td>
     <td class="r">${fmt(r.entry_price)}</td>
     <td class="r ${clr(r.ai_bigwin)}" style="font-weight: 600;">${r.ai_bigwin!=null?Number(r.ai_bigwin).toFixed(0)+'%':(r.prob!=null?(Number(r.prob)*100).toFixed(1)+'%':'-')}</td></tr>`
  ).join(''):'<tr><td colspan="6" style="color:#9ca3af;text-align:center">&#xC624;&#xB298; &#xC2E0;&#xADDC; &#xD6C4;&#xBCF4; &#xC5C6;&#xC74C;</td></tr>');
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
    `<tr><td style="font-weight: 500;">${r.code||''} ${r.name||''}</td><td style="color:#9ca3af;font-size:12px">${r.strategy||''}</td>
     <td class="r" style="color:#9ca3af">${String(r.signal_date||'').slice(0,8)}</td>
     <td class="r">${fmt(r.entry_price_close)}</td>
     <td class="r" style="color:#9ca3af">${String(r.target_exit_date||'').slice(0,8)}</td>
     <td class="r ${clr(r.est_pct)}" style="font-weight: 600;">${r.est_pct!=null?pct(r.est_pct):'-'}</td></tr>`
  ).join(''):'<tr><td colspan="6" style="color:#9ca3af;text-align:center">만료된 신호만 있거나 없음</td></tr>');
}

function fillSlots(elId, slots){
  const el=document.getElementById(elId);if(!el)return;
  if(!slots||!slots.length){el.innerHTML='<div style="color:#9ca3af;font-size:13px">슬롯 없음</div>';return;}
  el.innerHTML=slots.map(s=>{
    const used=s.used||0, mx=s.max||0;
    const dots='●'.repeat(Math.min(used,mx))+'○'.repeat(Math.max(0,mx-used));
    const stop=(s.stop_pct!==''&&s.stop_pct!=null)?`손절 ${s.stop_pct}%`:'손절 없음';
    return `<div class="slotitem">
      <div class="slothead" onclick="var b=this.parentNode.querySelector('.slotbody');b.classList.toggle('open');this.querySelector('.caret').textContent=b.classList.contains('open')?'▾':'▸'">
        <span class="slotname">${s.label||s.strategy||''} <span style="color:#9ca3af;font-size:12px;font-weight:normal;margin-left:6px">${s.strategy||''}</span></span>
        <span style="font-size:14px;font-weight:600;"><b class="${used?'pos':'neu'}">${used}/${mx}</b> <span style="color:#64748b;letter-spacing:2px;margin-left:4px">${dots}</span></span>
        <span class="caret">▸</span>
      </div>
      <div class="slotbody">
        <div><b style="color:#10b981">진입</b> &nbsp;${s.entry||'-'}</div>
        <div style="margin-top:6px"><b style="color:#ef4444">청산</b> &nbsp;${s.exit||'-'}</div>
        <div style="margin-top:8px;color:#94a3b8;background:rgba(255,255,255,0.03);padding:8px;border-radius:6px;">보유 ${s.holding||'?'}영업일 · ${stop} · 진입가=신호일 종가 / 실제 매수=다음 영업일 09시대</div>
      </div>
    </div>`;
  }).join('');
}

function fillBal(elId, snap){
  const el=document.getElementById(elId); if(!el) return;
  if(!snap || snap.deposit==null){ el.innerHTML='<div style="color:#9ca3af;font-size:13px">스냅샷 없음</div>'; return; }
  const npos=(snap.positions||[]).length;
  let h='';
  if(snap.total_eval!=null)
    h+=`<div class="stat"><div class="v">${fmt(snap.total_eval)}원</div><div class="l">총평가금액</div></div>`;
  h+=`<div class="stat"><div class="v" style="font-size:20px">${fmt(snap.deposit)}원</div><div class="l">예수금(정산완료)</div></div>`;
  if(snap.orderable!=null && snap.orderable!==snap.deposit)
    h+=`<div class="stat"><div class="v" style="font-size:20px">${fmt(snap.orderable)}원</div><div class="l">주문가능(D+2)</div></div>`;
  if(snap.eval_pnl!=null)
    h+=`<div class="stat"><div class="v ${clr(snap.eval_pnl)}" style="font-size:20px">${fmt(snap.eval_pnl)}원</div><div class="l">평가손익</div></div>`;
  h+=`<div class="stat"><div class="v">${npos}</div><div class="l">보유 종목</div></div>`;
  h+=`<div class="stat"><div class="v" style="font-size:14px">${snap.date||''} ${snap.time||''}</div><div class="l">스냅샷 기준</div></div>`;
  el.innerHTML=h;
}

function fillOrders(elId, cntId, trades, total){
  const tb=document.getElementById(elId); if(!tb) return;
  const rows=(trades||[]).slice(0,80);
  const cnt=document.getElementById(cntId);
  if(cnt) cnt.textContent = total ? `(전체 ${fmt(total)}건 중 최근 ${rows.length})` : '';
  const dm=d=>{const s=String(d||'');return s.length>=8?s.slice(4,6)+'/'+s.slice(6,8):'';};
  tb.innerHTML = rows.length ? rows.map(t=>{
    const bt=(dm(t.buy_date)+' '+(t.buy_time||'').slice(0,5)).trim();
    const st=t.open?'<span style="color:#64748b;font-weight:600">보유중</span>'
                   :(dm(t.sell_date)+' '+(t.sell_time||'').slice(0,5)).trim();
    const hasPnl = !t.open && t.pnl!=='' && t.pnl!=null;
    const pnlAmt = hasPnl ? `<span class="${clr(t.pnl)}" style="font-weight:600">${fmt(t.pnl)}원</span>` : '';
    const pnlPct = hasPnl ? `<span class="${clr(t.pnl_pct)}" style="font-weight:600">${pct(t.pnl_pct)}</span>` : '';
    const sc=t.score_ic;
    return `<tr${t.open?' style="background:rgba(56,189,248,0.05)"':''}>
      <td style="font-weight:500">${t.name||''} <span style="color:#94a3b8;font-size:12px;font-weight:normal">${t.code||''}</span></td>
      <td class="r" style="font-size:12px;color:#cbd5e1">${bt}</td>
      <td class="r" style="font-size:12px;color:#cbd5e1">${st}</td>
      <td style="color:#94a3b8;font-size:12px">${t.strategy||''}</td>
      <td class="r">${sc!=null?(sc>=5.7?'<b style="color:#10b981">'+sc+'</b>':sc):''}</td>
      <td class="r">${fmt(t.qty)}</td>
      <td class="r">${fmt(t.buy_price)}</td>
      <td class="r">${t.open?'':fmt(t.sell_price)}</td>
      <td class="r">${pnlAmt}</td>
      <td class="r">${pnlPct}</td>
      <td style="font-size:12px;color:#94a3b8">${t.reason||''}</td></tr>`;
  }).join('') : '<tr><td colspan="11" style="color:#9ca3af;text-align:center">매매내역 없음</td></tr>';
}

function fillStopMonitor(elId, tsId, mon){
  const tb=document.getElementById(elId); if(!tb) return;
  const items=(mon&&mon.items)||[];
  const ts=document.getElementById(tsId);
  if(ts) ts.textContent = mon&&mon.updated ? '('+mon.updated+')' : '';
  if(!items.length){
    tb.innerHTML='<tr><td colspan="5" style="color:#9ca3af;text-align:center">장중 15분마다 갱신 (보유 없음/장외)</td></tr>';
    return;
  }
  tb.innerHTML=items.map(x=>{
    const fired=x.room_pp<=0, imm=x.imminent;
    const badge=fired?'<span style="color:#ef4444;font-weight:bold;background:rgba(239,68,68,0.1);padding:4px 8px;border-radius:4px;">● 손절선도달(마감청산)</span>'
              :imm?'<span style="color:#f59e0b;font-weight:bold;background:rgba(245,158,11,0.1);padding:4px 8px;border-radius:4px;">▲ 임박</span>'
              :'<span style="color:#10b981">정상</span>';
    return `<tr>
      <td style="font-weight:500">${x.name||''} <span style="color:#94a3b8;font-size:12px;font-weight:normal">${x.code||''}</span></td>
      <td class="r ${clr(x.pnl_pct)}" style="font-weight:600">${(x.pnl_pct>=0?'+':'')+x.pnl_pct}%</td>
      <td class="r neg">${x.stop_pct}%</td>
      <td class="r ${x.room_pp<=3?'neg':'neu'}" style="font-weight:600">${x.room_pp}</td>
      <td>${badge}</td></tr>`;
  }).join('');
}

function fillStratPerf(elId, perf){
  const rows=perf||[];
  setHtml(elId, rows.length?rows.map(r=>`<tr>
    <td style="color:#cbd5e1;font-weight:500">${r.strategy||''}</td>
    <td class="r">${r.closed_n||0}</td>
    <td class="r" style="font-weight:600">${r.win_pct!=null?r.win_pct+'%':'-'}</td>
    <td class="r ${clr(r.avg_pct)}" style="font-weight:600">${r.avg_pct!=null?pct(r.avg_pct):'-'}</td>
    <td class="r ${clr(r.realized_pnl)}" style="font-weight:600">${fmt(r.realized_pnl)}원</td>
    <td class="r">${r.open_n||0}</td>
    <td class="r ${clr(r.unreal_pnl)}" style="font-weight:600">${fmt(r.unreal_pnl)}원</td></tr>`).join('')
    :'<tr><td colspan="7" style="color:#9ca3af;text-align:center">완료·보유 거래 없음</td></tr>');
}

function fillMock(mock, kis){
  if(mock&&!mock.error){
    fillBal('mock-bal', mock.snapshot);
    fillOrders('mock-orders','mock-ord-cnt', mock.trades, mock.trade_total);
    fillSlots('mock-slots', mock.slot_status||[]);
    fillStratPerf('mock-stratperf', mock.strategy_perf);
    const pos=mock.positions||[];
    setHtml('mock-pos', pos.length?pos.map(r=>{
      const ev=(r.evlt_amt!=null&&r.evlt_amt!=='')?r.evlt_amt:(Number(r.qty)||0)*(Number(r.cur_price||r.current_price)||0);
      return `<tr><td style="font-weight:500">${r.code||''} ${r.name||''}</td><td style="color:#94a3b8;font-size:12px">${r.strategy||''}</td>
       <td class="r">${r.score_ic!=null?(r.score_ic>=5.7?'<b style="color:#10b981">'+r.score_ic+'</b>':r.score_ic):'-'}</td>
       <td class="r">${fmt(r.qty)}</td><td class="r">${fmt(r.avg_price)}</td>
       <td class="r">${fmt(r.cur_price||r.current_price)}</td>
       <td class="r">${fmt(ev)}원</td>
       <td class="r ${clr(r.pnl)}" style="font-weight:600">${fmt(r.pnl)}원</td>
       <td class="r ${clr(r.pnl_pct)}" style="font-weight:600">${pct(r.pnl_pct)}</td>
       <td class="r" style="font-size:12px;color:#94a3b8">${String(r.entry_date||'').slice(0,10)}</td></tr>`;
    }).join(''):'<tr><td colspan="10" style="color:#9ca3af;text-align:center">보유 포지션 없음</td></tr>');
  }
  if(kis&&!kis.error){
    fillBal('kis-bal', kis.snapshot);
    fillStopMonitor('kis-stop-rows','kis-stop-ts', kis.stop_monitor);
    fillOrders('kis-orders','kis-ord-cnt', kis.trades, kis.trade_total);
    fillSlots('kis-slots', kis.slot_status||[]);
    fillStratPerf('kis-stratperf', kis.strategy_perf);
    const pos=kis.positions||[];
    setHtml('kis-pos', pos.length?pos.map(r=>{
      const ev=(r.evlt_amt!=null&&r.evlt_amt!=='')?r.evlt_amt:(Number(r.qty)||0)*(Number(r.cur_price||r.current_price)||0);
      return `<tr><td style="font-weight:500">${r.code||''} ${r.name||''}</td><td style="color:#94a3b8;font-size:12px">${r.strategy||''}</td>
       <td class="r">${r.score_ic!=null?(r.score_ic>=5.7?'<b style="color:#10b981">'+r.score_ic+'</b>':r.score_ic):'-'}</td>
       <td class="r">${fmt(r.qty)}</td><td class="r">${fmt(r.avg_price)}</td>
       <td class="r">${fmt(r.cur_price||r.current_price)}</td>
       <td class="r">${fmt(ev)}원</td>
       <td class="r ${clr(r.pnl)}" style="font-weight:600">${fmt(r.pnl)}원</td>
       <td class="r ${clr(r.pnl_pct)}" style="font-weight:600">${pct(r.pnl_pct)}</td>
       <td class="r" style="font-size:12px;color:#94a3b8">${String(r.entry_date||'').slice(0,10)}</td></tr>`;
    }).join(''):'<tr><td colspan="10" style="color:#9ca3af;text-align:center">보유 포지션 없음</td></tr>');
  }
}

function fillRunButtons(d){
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

function fillCutMonitor(cm){
  if(!cm) return;
  if(cm.note||cm.error){
    setHtml('cm-meta','<span style="color:#f0883e">'+(cm.error||cm.note)+'</span>');
    return;
  }
  setHtml('cm-meta', `집계 ${cm.generated||'-'} · 신호일 ${cm.days||0}일 (${cm.span||'-'})`);

  const rules=cm.rules||[];
  // 현행을 기준으로 삼아 신규 규칙의 우열을 색으로 표시한다
  const base=rules.find(r=>String(r.rule).indexOf('현행')>=0);
  setHtml('cm-rules', rules.length? rules.map(r=>{
    const isBase=(base&&r.rule===base.rule);
    const d=(base&&r.avg!=null&&base.avg!=null&&!isBase)?(r.avg-base.avg):null;
    const dtxt=(d==null)?'':` <span class="${d>0?'pos':'neg'}" style="font-size:11px">(${d>0?'+':''}${d.toFixed(2)}%p)</span>`;
    return `<tr${isBase?' style="background:rgba(88,166,255,.07)"':''}>
      <td style="font-weight:600">${r.rule||''}${isBase?' <span style="color:#58a6ff;font-size:11px">기준</span>':''}</td>
      <td class="r">${fmt(r.n)}</td><td class="r">${fmt(r.done)}</td>
      <td class="r" style="color:#8b949e">${fmt(r.open)}</td>
      <td class="r ${clr(r.avg)}" style="font-weight:700">${r.avg!=null?pct(r.avg):'-'}${dtxt}</td>
      <td class="r ${clr(r.med)}">${r.med!=null?pct(r.med):'-'}</td>
      <td class="r ${clr(r.cum)}" style="font-weight:600">${r.cum!=null?pct(r.cum):'-'}</td>
      <td class="r neg">${r.worst!=null?pct(r.worst):'-'}</td>
      <td class="r">${fmt(r.kiwoom)}</td><td class="r">${fmt(r.kis)}</td>
      <td class="r">${fmt(r.strats)}</td></tr>`;
  }).join('') : '<tr><td colspan="11" style="color:#8b949e;text-align:center">집계 없음</td></tr>');

  const bs=cm.by_strategy||[];
  if(bs.length){
    const byRule={};
    bs.forEach(x=>{ (byRule[x.rule]=byRule[x.rule]||[]).push(x); });
    setHtml('cm-bystrat', Object.keys(byRule).map(k=>{
      const items=byRule[k].sort((a,b)=>b.n-a.n).map(x=>
        `<span style="display:inline-block;margin:2px 10px 2px 0">${x.strategy}
          <b class="${clr(x.avg)}">${pct(x.avg)}</b>
          <span style="color:#8b949e;font-size:11px">(${x.n}건)</span></span>`).join('');
      return `<div style="margin-bottom:8px"><span style="color:#c9d1d9;font-weight:600">${k}</span><br>${items}</div>`;
    }).join(''));
  }

  const dl=cm.daily||[];
  if(dl.length){
    const keys=rules.map(r=>r.rule);
    setHtml('cm-daily',
      '<table style="margin-top:4px"><thead><tr><th class="r">신호일</th>'+
      keys.map(k=>'<th class="r">'+k+'</th>').join('')+'</tr></thead><tbody>'+
      dl.slice().reverse().map(r=>'<tr><td class="r">'+r.date+'</td>'+
        keys.map(k=>'<td class="r">'+(r[k]||0)+'</td>').join('')+'</tr>').join('')+
      '</tbody></table>');
  }

  const rows=cm.rows||[];
  setHtml('cm-rows', rows.length? rows.map(r=>`<tr>
      <td style="font-size:12px">${r.rule||''}</td>
      <td class="r" style="font-size:12px">${r.signal_date||''}</td>
      <td class="r" style="font-size:12px">${r.entry_date||''}</td>
      <td style="font-size:12px;color:#94a3b8">${r.acct||''}</td>
      <td style="font-size:12px;color:#94a3b8">${r.strategy||''}</td>
      <td style="font-weight:500">${r.code||''} ${r.name||''}</td>
      <td class="r">${r.score!=null?r.score:'-'}</td>
      <td class="r">${fmt(r.entry_price)}</td>
      <td class="r" style="font-size:12px">${r.exit_date||''}</td>
      <td class="r ${clr(r.net_pct)}" style="font-weight:600">${r.net_pct!=null?pct(r.net_pct):'-'}</td>
      <td style="font-size:12px;color:${String(r.why)==='보유중'?'#8b949e':'#c9d1d9'}">${r.why||''}</td>
    </tr>`).join('') : '<tr><td colspan="11" style="color:#8b949e;text-align:center">내역 없음</td></tr>');
}

function fillStrength(st){
  if(!st){ return; }
  const s=st.summary||{};
  setHtml('st-summary', st.total ? `
    <div class="stat"><div class="v">${fmt(s.total)}</div><div class="l">총 기록 신호</div></div>
    <div class="stat"><div class="v" style="color:#38bdf8">${safe(s.avg_score_ic,'-')}</div><div class="l">평균 강도(IC)</div></div>
    <div class="stat"><div class="v">${safe(s.scored_pct,'0')}%</div><div class="l">채점 성공률</div></div>
    <div class="stat"><div class="v">${s.strategies||0}</div><div class="l">전략 수</div></div>
    <div class="stat"><div class="v">${s.accounts||0}</div><div class="l">계좌 수</div></div>
    <div class="stat"><div class="v" style="font-size:16px">${s.last_date||'-'}</div><div class="l">최근 기록일</div></div>
  ` : '<div style="color:#9ca3af;font-size:13px">강도 기록 없음 &mdash; 다음 신호생성(18:30) 때 첫 데이터가 누적됩니다.</div>');
  const bs=st.by_strategy||[];
  setHtml('st-bystrat', bs.length?bs.map(r=>
    `<tr><td style="font-weight:500">${r.strategy||''}</td><td class="r">${fmt(r.n)}</td>
     <td class="r" style="font-weight:600;color:#e2e8f0">${safe(r.avg_ic,'-')}</td><td class="r">${safe(r.avg_tv,'-')}</td></tr>`
  ).join(''):'<tr><td colspan="4" style="color:#9ca3af;text-align:center">데이터 없음</td></tr>');

  const vt=st.virtual;
  setHtml('st-virtual', (vt&&vt.buckets&&vt.buckets.length)?
    vt.buckets.map(b=>`
      <div class="stat" style="min-width:160px">
        <div class="v ${clr(b.avg_net)}">${pct(b.avg_net)}</div>
        <div class="l" style="margin-top:8px">강도 ${b.label}<br><span style="color:#64748b;font-size:11px;text-transform:none">${fmt(b.n)}건 평균 · 승률 ${b.win}%</span></div>
      </div>`).join('')
    +`<div class="stat"><div class="v" style="font-size:16px">${fmt(vt.done_total)}건</div><div class="l">완료 매매(조인 표본)</div></div>`
    :'<div style="color:#9ca3af;font-size:13px">아직 실현 표본 없음 — 신호가 보유기간을 마치고 paper_audit(일 09:00)이 돌면 누적됩니다.</div>');
  
  const va=st.virtual_ai;
  setHtml('st-virtual-ai', (va&&va.buckets&&va.buckets.length)?
    va.buckets.map(b=>`
      <div class="stat" style="min-width:180px">
        <div class="v ${clr(b.avg_net)}">${pct(b.avg_net)}</div>
        <div class="l" style="margin-top:8px">AI확률 ${b.label}<br><span style="color:#64748b;font-size:11px;text-transform:none">${fmt(b.n)}건 · 승률 ${b.win}%</span></div>
      </div>`).join('')
    +`<div class="stat"><div class="v" style="font-size:16px">${fmt(va.done_total)}건</div><div class="l">완료 매매(조인 표본)</div></div>`
    :`<div style="color:#9ca3af;font-size:13px;line-height:1.6">${va?`AI확률 기록 ${fmt(va.logged_total)}건 누적 중 — 완료 매매 ${fmt(va.done_total)}건(9건 이상부터 분위 표시).<br>ai_prob 는 06-30부터 기록되어 첫 만기(rsi 5일)가 ~07-07 도래.`:'AI확률 기록 없음'}</div>`);
  
  window._stRows = st.rows||[];
  window._stSort = window._stSort || {key:null, asc:false};
  renderStRows();
}

function fillAiPaper(ap){
  const el=document.getElementById('ap-stats'); if(!el) return;
  if(!ap||!ap.portfolios){ el.innerHTML='<div style="color:#9ca3af;font-size:13px">첫 실행 대기 — 18:31 신호 생성 후 생성됩니다 (수동: 제어판 없이 python ai_paper_trader.py)</div>'; return; }
  const ts=document.getElementById('ap-ts'); if(ts) ts.textContent='('+(ap.updated||'')+' 갱신)';
  const P=ap.portfolios;
  el.innerHTML=['ai','strength'].map(k=>{
    const p=P[k]; if(!p) return '';
    const nm=k==='ai'?'🤖 AI(ai_prob·필터없음)':'💪 강도(score_ic≥5.7 — 라이브 룰)';
    return `<div class="stat" style="min-width:260px">
      <div class="v ${clr(p.ret_pct)}" style="display:flex;align-items:baseline;gap:8px">${fmt(p.equity)}원 <span style="font-size:14px;font-weight:600">(${pct(p.ret_pct)})</span></div>
      <div class="l" style="margin-top:8px">${nm}<br><span style="color:#64748b;font-size:11px;text-transform:none">보유 ${p.n_pos} · 완료 ${p.n_trades}건${p.win_pct!=null?' · 승률 '+p.win_pct+'%':''}${p.avg_net!=null?' · 평균 '+pct(p.avg_net):''}</span></div>
    </div>`;
  }).join('');
  const rows=[];
  ['ai','strength'].forEach(k=>{
    (P[k]&&P[k].positions||[]).slice(0,10).forEach(r=>rows.push({k,...r}));
  });
  setHtml('ap-pos', rows.length?rows.map(r=>`<tr>
    <td style="font-weight:600;color:${r.k==='ai'?'#a78bfa':'#f87171'}">${r.k==='ai'?'🤖 AI':'💪 강도'}</td>
    <td style="font-weight:500">${r.code||''} ${r.name||''}</td>
    <td style="color:#94a3b8;font-size:12px">${r.strategy||''}</td>
    <td class="r" style="font-size:12px;color:#cbd5e1">${r.entry_date||''}</td>
    <td class="r" style="font-weight:600">${r.score!=null?r.score:''}</td>
    <td class="r ${clr(r.pnl_pct)}" style="font-weight:600">${pct(r.pnl_pct)}</td></tr>`).join('')
    :'<tr><td colspan="6" style="color:#9ca3af;text-align:center">보유 없음</td></tr>');

  const bsHtml=['ai','strength'].map(k=>{
    const p=P[k]; const bs=(p&&p.by_strategy)||[];
    if(!bs.length) return '';
    const label=k==='ai'?'🤖 AI':'💪 강도';
    // [2026-10-09 §44] 중앙값·최대기여일·그날 제외 평균 — 평균이 하루 이벤트에 좌우되는지 보이게.
    // 하루가 손익(절대값)의 50% 이상이고 2건 이상이면 ⚠. 구버전 JSON(필드 없음)은 '-' 표시.
    const tr=bs.map(s=>{
      const conc=s.top_day_share!=null&&s.top_day_share>=50&&s.n>=2;
      const td=s.top_day?`${s.top_day.slice(4,6)}/${s.top_day.slice(6,8)}`:'';
      const topCell=s.top_day?`<span style="${conc?'color:#fbbf24;font-weight:700':'color:#94a3b8'}">${conc?'⚠ ':''}${td} · ${s.top_day_n}건 · ${s.top_day_share}%</span>`:'-';
      return `<tr>
      <td style="color:#cbd5e1;font-weight:500">${s.strategy||''}</td>
      <td class="r">${s.n}</td>
      <td class="r" style="font-weight:600">${s.win_pct!=null?s.win_pct+'%':'-'}</td>
      <td class="r ${clr(s.avg_net)}" style="font-weight:600">${pct(s.avg_net)}</td>
      <td class="r ${s.med_net!=null?clr(s.med_net):''}">${s.med_net!=null?pct(s.med_net):'-'}</td>
      <td class="r" style="font-size:12px">${topCell}</td>
      <td class="r ${s.avg_ex_top!=null?clr(s.avg_ex_top):''}">${s.avg_ex_top!=null?pct(s.avg_ex_top):'-'}</td>
      <td class="r ${clr(s.pnl)}" style="font-weight:600">${fmt(s.pnl)}원</td></tr>`;}).join('');
    return `<div style="margin-bottom:16px;background:#1f2937;padding:16px;border-radius:10px;border:1px solid #374151">
      <div style="font-size:13px;font-weight:700;margin-bottom:12px;color:${k==='ai'?'#a78bfa':'#f87171'}">${label}</div>
      <table><thead><tr><th>전략</th><th class="r">완료</th><th class="r">승률</th><th class="r">평균%</th><th class="r">중앙값%</th><th class="r" title="손익(절대값) 기여가 가장 큰 진입일 · 그날 건수 · 전체 손익 중 비중">최대기여일</th><th class="r">그날 제외 평균%</th><th class="r">실현손익</th></tr></thead>
      <tbody>${tr}</tbody></table>
      <div style="font-size:11px;color:#64748b;margin-top:8px">⚠ = 하루 진입분이 손익의 절반 이상 — 평균보다 중앙값·그날 제외 평균을 보세요 (§44)</div></div>`;
  }).join('');
  setHtml('ap-bystrat', bsHtml||'완료 거래 없음');

  const trHtml=['ai','strength'].map(k=>{
    const p=P[k]; const tds=(p&&p.trades)||[];
    const label=k==='ai'?'🤖 AI':'💪 강도';
    if(!tds.length) return `<details style="margin-bottom:8px;background:#1f2937;padding:12px 16px;border-radius:8px"><summary style="cursor:pointer;color:#94a3b8">${label} — 완료 0건</summary></details>`;
    const body=tds.map(t=>`<tr>
      <td style="font-weight:500">${t.code||''} ${t.name||''}</td>
      <td style="color:#94a3b8;font-size:12px">${t.strategy||''}</td>
      <td class="r" style="font-size:12px;color:#cbd5e1">${t.entry_date||''}</td>
      <td class="r" style="font-size:12px;color:#cbd5e1">${t.exit_date||''}</td>
      <td class="r" style="font-weight:600">${t.score!=null?t.score:''}</td>
      <td class="r ${clr(t.net_pct)}" style="font-weight:600">${pct(t.net_pct)}</td>
      <td class="r ${clr(t.pnl)}" style="font-weight:600">${fmt(t.pnl)}원</td></tr>`).join('');
    return `<details style="margin-bottom:8px;background:#1f2937;padding:12px 16px;border-radius:8px"><summary style="cursor:pointer;color:#e2e8f0;font-weight:600">${label} — 완료 ${tds.length}건 <span style="font-weight:normal;color:#64748b;font-size:12px">(클릭)</span></summary>
      <table style="margin-top:12px"><thead><tr><th>종목</th><th>전략</th><th class="r">진입</th><th class="r">청산</th><th class="r">점수</th><th class="r">순수익%</th><th class="r">손익</th></tr></thead>
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
  document.querySelectorAll('#st-head th.sortable').forEach(th=>{
    th.textContent=th.textContent.replace(/ [▲▼]$/,'');
    const m=th.getAttribute('onclick')||'';
    if(s.key && m.includes(`'${s.key}'`)) th.textContent+=(s.asc?' ▲':' ▼');
  });
  setHtml('st-rows', rows.length?rows.map(r=>{
    const msv=String(r.market_strong);
    const ms=(msv==='True'||msv==='true')?'<span style="color:#10b981;background:rgba(16,185,129,0.1);padding:2px 6px;border-radius:4px;font-weight:600">강세</span>'
            :(msv==='False'||msv==='false')?'<span style="color:#94a3b8;background:rgba(148,163,184,0.1);padding:2px 6px;border-radius:4px">약세</span>':'';
    const ic=Number(r.score_ic);
    const icCls=(!isNaN(ic)&&ic>=6)?'pos':'';
    return `<tr>
      <td class="r" style="font-size:12px;color:#cbd5e1">${String(r.signal_date||'')}</td>
      <td style="font-size:12px;color:#cbd5e1">${r.account||''}</td>
      <td style="color:#94a3b8;font-size:12px">${r.strategy||''}</td>
      <td style="font-weight:500">${r.code||''} ${r.name||''}</td>
      <td class="r">${fmt(r.entry_close)}</td>
      <td class="r ${icCls}" style="font-weight:700;font-size:14px">${safe(r.score_ic,'-')}</td>
      <td class="r" style="font-weight:600">${(r.ai_prob!=null&&r.ai_prob!=='')?(Number(r.ai_prob)*100).toFixed(1)+'%':'-'}</td>
      <td class="r">${safe(r.score_tv,'-')}</td>
      <td class="r">${safe(r.slot_rank,'-')}</td>
      <td>${ms}</td></tr>`;
  }).join(''):'<tr><td colspan="10" style="color:#9ca3af;text-align:center">강도 기록 없음</td></tr>');
}

function sortSt(key){
  const s=window._stSort||{key:null,asc:false};
  if(s.key===key) s.asc=!s.asc;
  else { s.key=key; s.asc=false; }
  window._stSort=s;
  renderStRows();
}

function fillBacktest(bt, sc){
  if(!bt){setHtml('bt-stats','<div class="err-box">백테스트 데이터 없음</div>');return;}
  if(bt.error){setHtml('bt-stats',`<div class="err-box">${bt.error}</div>`);return;}
  setTxt('bt-src', bt.source||'');
  const s=bt.stats;
  if(s&&s.total){
    setHtml('bt-stats',`
      <div class="stat"><div class="v">${fmt(s.total)}</div><div class="l">거래</div></div>
      <div class="stat"><div class="v">${safe(s.win_rate)} %</div><div class="l">승률</div></div>
      <div class="stat"><div class="v ${clr(s.avg_return)}">${pct(s.avg_return)}</div><div class="l">평균%</div></div>
      <div class="stat"><div class="v neg">${safe(s.mdd)} %</div><div class="l">MDD</div></div>
      <div class="stat"><div class="v">${safe(s.sharpe)}</div><div class="l">샤프</div></div>`);
  } else {
    setHtml('bt-stats','<div style="color:#9ca3af;font-size:13px">통계 없음 (net_pct 컬럼 필요)</div>');
  }
  const trades=bt.trades||[];
  setHtml('bt-rows', trades.length?trades.slice(0,500).map(r=>{
    if(r.mode!==undefined){
      const da=Number(r.delta_avg||0);
      return `<tr>
        <td style="font-weight:500">${r.strategy||r.stock_code||''}</td>
        <td style="font-size:11px;color:#94a3b8">${r.mode||''}</td>
        <td class="r" style="font-size:12px;color:#cbd5e1">${String(r.entry_date||'').slice(0,10)}</td>
        <td class="r" style="font-size:12px;color:#cbd5e1">${String(r.exit_date||'').slice(0,10)}</td>
        <td class="r ${clr(r.net_pct)}" style="font-weight:600">${pct(r.net_pct)}</td>
        <td class="r ${clr(r.gross_pct)}" style="font-weight:600">${pct(r.gross_pct)}</td></tr>`;
    }
    return `<tr>
      <td style="font-weight:500">${r.stock_code||r.code||''}</td>
      <td style="font-size:12px;color:#94a3b8">${r.strategy||''}</td>
      <td class="r" style="font-size:12px;color:#cbd5e1">${String(r.entry_date||r.entry_time||'').slice(0,10)}</td>
      <td class="r" style="font-size:12px;color:#cbd5e1">${String(r.exit_date||'').slice(0,10)}</td>
      <td class="r ${clr(r.net_pct)}" style="font-weight:600">${pct(r.net_pct)}</td>
      <td class="r ${clr(r.gross_pct)}" style="font-weight:600">${pct(r.gross_pct)}</td></tr>`;
  }).join(''):'<tr><td colspan="6" style="color:#9ca3af;text-align:center">거래 내역 없음</td></tr>');
  const scRows=((sc&&sc.rows)||[]).slice(0,200);
  setHtml('sc-rows', scRows.length?scRows.map(r=>{
    const da=Number(r.delta_avg||0);
    return `<tr>
      <td style="font-weight:500">${r.strategy||''}</td>
      <td style="font-size:11px;color:#94a3b8">${r.mode||''}</td>
      <td class="r">${r.n||r.trades||''}</td>
      <td class="r" style="font-weight:600">${Number(r.win_pct||0).toFixed(1)}%</td>
      <td class="r ${clr(r.avg_net)}" style="font-weight:600">${Number(r.avg_net||0).toFixed(2)}%</td>
      <td class="r">${Number(r.profit_factor||0).toFixed(2)}</td>
      <td class="r">${Number(r.sharpe||0).toFixed(2)}</td>
      <td class="r">${Number(r.avg_hold||0).toFixed(1)}d</td>
      <td class="r ${clr(da)}" style="font-weight:700">${da>=0?'+':''}${da.toFixed(2)}%p</td></tr>`;
  }).join(''):'<tr><td colspan="9" style="color:#9ca3af;text-align:center">strategy_engine.py 먼저 실행하세요</td></tr>');
}

// ── [2026-10-18] 기간 백테스트 테스트기 ─────────────────────────────────────────
let _pbtMeta=null, _pbtTimer=null, _pbtLastRes=null;
// ── 지표 설명(마우스를 올리면 뜸) — '|' 는 줄바꿈 (2026-10-18) ──
const _PBT_TIPS={
  strategy:'전략 이름을 누르면 어떤 조건으로 사고파는지 쉬운 말로 설명이 뜹니다|★ = 지금 모의매매에서 쓰는 전략|이름 옆 ℹ 참고 · ⚠ 경고(데이터·표본 부족) · ⛔ 오류 — 마우스를 올리면 이유와 결과를 읽는 법이 뜸',
  n:'기간 안에 신호가 난 매매 건수(10칸이 다 차서 실제로 못 산 신호도 포함)|(미완료 N) = 종료일 뒤에 팔리는 매매',
  win:'끝난 매매 중 이익으로 끝난 비율|높아도 손실 한 번이 크면 전체는 마이너스일 수 있음',
  avg:'신호 전부를 샀다고 칠 때 한 건 평균 수익률(비용 포함)|10칸 제약 없이 본 ’신호 자체의 실력’',
  slot:'10칸에 실제로 담긴 매매만의 평균 수익률|자본곡선은 이 값을 따른다 — 신호 평균과 차이가 크면 ’고르는 순서’가 결과를 좌우한 것',
  tot:'기간 처음 대비 끝의 자산 증감률|예: 1,000만 원 → 1,300만 원이면 +30%',
  cagr:'CAGR = 연평균 수익률|기간 전체 수익을 ’매년 같은 비율로 불었다면 한 해 몇 %?’로 바꾼 값|예: 3년에 +33% → 연 약 +10%|기간 길이가 달라도 서로 비교할 수 있어 표를 이 값 순으로 정렬|은행 이자(연 3% 안팎)보다 낮으면 굳이 위험을 질 이유가 약함',
  mdd:'MDD = 최대 낙폭|기간 중 가장 높았던 잔고에서 가장 많이 떨어졌던 비율|예: 1,000만 원까지 갔다가 600만 원까지 떨어졌으면 −40%|’최악일 때 얼마나 아팠나’ — 0에 가까울수록 좋음|−50%면 원금 회복에 +100%가 필요',
  sharpe:'샤프 = 수익의 ’질’(흔들림 대비 수익)|하루하루 수익의 평균 ÷ 하루하루 흔들림(표준편차)을 1년 기준으로 환산(무위험수익 0 가정)|같은 수익이면 덜 출렁인 쪽이 높다|0 이하: 위험만 지고 못 벌었음 · 0.5 전후: 보통 · 1 이상: 좋음|운으로도 높게 나올 수 있어 아래 ’운 검정’과 함께 볼 것',
  vs:'이 전략 CAGR − 시장 CAGR(%p)|시장 = 코스닥 유니버스 전 종목을 똑같이 나눠 산 경우|+면 그냥 시장 전체를 산 것보다 나았다는 뜻',
  year:'그 해 1년 동안의 자산 증감률|해마다 부호가 바뀌면 특정 해의 운에 기댄 전략일 가능성'
};
function _pbtTipInit(){
  if(document.getElementById('pbt-tipbox')) return;
  const box=document.createElement('div'); box.id='pbt-tipbox';
  box.style.cssText='position:fixed;z-index:9999;display:none;max-width:340px;padding:8px 11px;background:#0b1220;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:12px;line-height:1.6;box-shadow:0 6px 18px rgba(0,0,0,.45);pointer-events:none';
  document.body.appendChild(box);
  document.addEventListener('mouseover',e=>{ const t=e.target.closest&&e.target.closest('.pbt-tip'); if(!t) return;
    const lines=String(t.getAttribute('data-tip')||'').split('|');
    box.innerHTML=lines.map((l,i)=>i===0?`<b>${_pbtEsc(l)}</b>`:`<div style="color:#cbd5e1">${_pbtEsc(l)}</div>`).join('');
    box.style.display='block'; const r=t.getBoundingClientRect();
    let x=Math.min(r.left, window.innerWidth-360), y=r.bottom+6; if(y+box.offsetHeight>window.innerHeight) y=r.top-box.offsetHeight-6;
    box.style.left=Math.max(6,x)+'px'; box.style.top=Math.max(6,y)+'px'; });
  document.addEventListener('mouseout',e=>{ const t=e.target.closest&&e.target.closest('.pbt-tip'); if(t) box.style.display='none'; });
}
// ── 전략 설명 팝업(전략 이름 / ⓘ 클릭) ──
function pbtExplain(name){
  const s=((_pbtMeta&&_pbtMeta.strategies)||[]).find(x=>x.name===name); if(!s) return;
  const d=s.desc||{}, li=a=>(a||[]).map(x=>`<li>${_pbtEsc(x)}</li>`).join('');
  const r=((_pbtLastRes&&_pbtLastRes.results)||[]).find(x=>x.strategy===name);
  const P=(_pbtLastRes&&_pbtLastRes.params)||{};
  const f=(v,k)=>v==null||isNaN(v)?'-':(v>0?'+':'')+Number(v).toFixed(k==null?1:k)+'%';
  const res=r&&r.cagr!=null?`<div style="margin-top:10px;padding:8px 10px;background:#0f172a;border-radius:6px;font-size:12px">
      <b>이번 백테스트</b> <span style="color:#64748b">${_pbtD(P.start)} ~ ${_pbtD(P.eff_end||P.end)}</span><br>
      매매 ${r.n_trades}건 · 승률 ${r.win==null?'-':r.win+'%'} · 신호 평균 ${f(r.avg_net,2)} · 슬롯 실매매 ${f(r.slot_avg,2)}<br>
      총수익 ${f(r.total_ret)} · <span class="pbt-tip" data-tip="${_pbtEsc(_PBT_TIPS.cagr)}">CAGR</span> <b>${f(r.cagr)}</b> ·
      <span class="pbt-tip" data-tip="${_pbtEsc(_PBT_TIPS.mdd)}">MDD</span> ${f(r.mdd)} ·
      <span class="pbt-tip" data-tip="${_pbtEsc(_PBT_TIPS.sharpe)}">샤프</span> ${r.sharpe==null?'-':Number(r.sharpe).toFixed(2)}<br>
      연도별: ${Object.entries(r.yearly||{}).map(([y,v])=>`${y} <span class="${clr(v)}">${f(v)}</span>`).join(' · ')}</div>`:'';
  let m=document.getElementById('pbt-modal');
  if(!m){ m=document.createElement('div'); m.id='pbt-modal';
    m.style.cssText='position:fixed;inset:0;z-index:9000;background:rgba(2,6,23,.65);display:flex;align-items:center;justify-content:center';
    m.addEventListener('click',e=>{ if(e.target===m) m.style.display='none'; });
    document.addEventListener('keydown',e=>{ if(e.key==='Escape'&&m) m.style.display='none'; });
    document.body.appendChild(m); }
  m.innerHTML=`<div style="width:min(620px,92vw);max-height:86vh;overflow:auto;background:#111827;border:1px solid #334155;border-radius:12px;padding:18px 20px;color:#e2e8f0;font-size:13px;line-height:1.7">
    <div style="display:flex;justify-content:space-between;align-items:start;gap:10px">
      <div><div style="font-size:16px;font-weight:700">${s.live?'<span style="color:#fbbf24">★ </span>':''}${_pbtEsc(d.title||name)}</div>
      <div style="color:#64748b;font-size:12px">${_pbtEsc(name)}${s.live?' · 모의매매 운용 중: '+_pbtEsc(s.live):''}</div></div>
      <button class="runbtn" onclick="document.getElementById('pbt-modal').style.display='none'">닫기 ✕</button></div>
    ${d.idea?`<div style="margin-top:10px;padding:8px 10px;background:rgba(37,99,235,.12);border-left:3px solid #2563eb;border-radius:4px">💡 ${_pbtEsc(d.idea)}</div>`:''}
    <div style="margin-top:10px"><b style="color:#10b981">살 때 — 아래를 모두 만족하는 날</b><ul style="margin:4px 0 0 18px">${li(d.buy)}</ul></div>
    <div style="margin-top:6px"><b style="color:#f87171">팔 때</b><ul style="margin:4px 0 0 18px">${li(d.sell)}</ul></div>
    <div style="margin-top:6px"><b style="color:#93c5fd">필요한 데이터</b> <span style="color:#cbd5e1">${_pbtEsc((d.data||[]).join(' · '))}</span>
      <span style="color:#64748b"> · 시작 전 과거 ${s.warmup}거래일 필요</span></div>
    ${(d.caution||[]).length?`<div style="margin-top:8px;padding:8px 10px;background:rgba(251,191,36,.08);border:1px solid #78350f;border-radius:6px;color:#fcd34d"><b>주의</b><ul style="margin:4px 0 0 18px">${li(d.caution)}</ul></div>`:''}
    ${res}
    <div style="margin-top:10px;color:#64748b;font-size:11px"><b>백테스트 공통 규칙</b> — ${_pbtEsc((_pbtMeta.common_rules||[]).join(' · '))}</div></div>`;
  m.style.display='flex';
}
const _pbtEsc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const _pbtD=s=>s?`${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}`:'';
function _pbtCount(){ const n=document.querySelectorAll('#pbt-list input:checked').length; setTxt('pbt-count', `${n}개 선택`+(n>15?' · 많으면 수 분~수십 분':'')); }
async function pbtInit(){
  _pbtTipInit();
  if(_pbtMeta) return;
  try{ _pbtMeta=await fetch('/api/pbt/meta').then(r=>r.json()); }
  catch(e){ setHtml('pbt-warn',`<span style="color:#ef4444">테스트기 로드 실패: ${_pbtEsc(e.message)}</span>`); return; }
  const d=_pbtMeta.data;
  if(d){
    const e=_pbtD(d[1]); const s=String(Number(d[1].slice(0,4))-3)+d[1].slice(4);
    document.getElementById('pbt-end').value=e; document.getElementById('pbt-start').value=_pbtD(s<d[0]?d[0]:s);
    document.getElementById('pbt-start').min=_pbtD(d[0]); document.getElementById('pbt-end').max=e;
    setTxt('pbt-range', `가격 데이터 ${_pbtD(d[0])} ~ ${e}`);
  }
  setHtml('pbt-list', (_pbtMeta.strategies||[]).map(s=>{
    const tip=`과거 ${s.warmup}거래일 필요`+(s.needs.length?` · 필요 데이터: ${s.needs.join(', ')}`:'')+(s.live?` · 라이브: ${s.live}`:'');
    return `<label title="${_pbtEsc(tip)}" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:${s.live?'#fbbf24':'#cbd5e1'}">
      <input type="checkbox" value="${_pbtEsc(s.name)}" onchange="_pbtCount()"> ${s.live?'★ ':''}${_pbtEsc(s.name)}
      <span onclick="event.preventDefault();event.stopPropagation();pbtExplain('${_pbtEsc(s.name)}')" style="color:#60a5fa;cursor:pointer" title="전략 설명">ⓘ</span></label>`;}).join(''));
  setTxt('pbt-live-btn', `★ 라이브 ${((_pbtMeta.presets||{}).live||[]).length}개`);
  pbtSel('live');
  const j=_pbtMeta.running||_pbtMeta.last_job; if(j) pbtPoll(j);
  pbtHistLoad(true);      // 지난 기록 건수만 먼저(목록은 펼칠 때)
}
function pbtSel(p){
  const set=new Set(p==='none'?[]:((_pbtMeta&&_pbtMeta.presets&&_pbtMeta.presets[p])||[]));
  if(p==='top10'&&!set.size){ setHtml('pbt-warn','<span style="color:#fbbf24">상위 10개는 전체 전략 백테스트([다시 계산]) 결과가 있어야 정해집니다</span>'); }
  document.querySelectorAll('#pbt-list input').forEach(i=>{ i.checked=set.has(i.value); }); _pbtCount();
}
function _pbtBody(){
  return {start:document.getElementById('pbt-start').value.replace(/-/g,''), end:document.getElementById('pbt-end').value.replace(/-/g,''),
          names:[...document.querySelectorAll('#pbt-list input:checked')].map(i=>i.value), luck:document.getElementById('pbt-luck').checked,
          strength:(document.getElementById('pbt-strength')||{}).value||'',
          stop:(document.getElementById('pbt-stop')||{}).value||'', take:(document.getElementById('pbt-take')||{}).value||'',
          exit_mode:(document.getElementById('pbt-exitmode')||{}).value||'close',
          strength_version:(document.getElementById('pbt-sver')||{}).value||'v1'};
}
async function _pbtPost(url){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Action':'1'},body:JSON.stringify(_pbtBody())});
  let d={}; try{ d=await r.json(); }catch(e){ d={ok:false,msg:'응답 해석 실패 ('+r.status+')'}; }
  return d;
}
// [2026-10-22] 경고 문장 → '그래서 결과를 어떻게 읽나' 한 줄(옛 기록에도 적용)
function _pbtWarnMeaning(m){
  m=String(m||'');
  if(/강도를 매기지 못한/.test(m)&&!/짧은 기간/.test(m)) return '강도 v2는 해마다 그 이전 매매로만 학습하므로 첫 해들은 강도를 못 매김 — 강도 줄은 기본 줄보다 짧은 기간(뒤쪽 해들)만 본 결과';
  if(/표본 부족/.test(m)) return '매매 수가 적어 우연에 크게 좌우됨 — 숫자를 그대로 믿지 말 것';
  if(/과거 \\d+거래일이 필요/.test(m)) return '기간 초반엔 계산에 필요한 과거 가격이 없어 신호가 안 나옴 — 초반 성과는 현금 상태';
  if(/데이터는 .* 부터입니다/.test(m)) return '그 전 기간은 신호가 없어 현금 — 기간 전체 CAGR이 실제보다 낮게 보일 수 있음';
  if(/멈춰 있습니다/.test(m)) return '최근 구간은 신호가 없어 현금 — 수집 버튼으로 채운 뒤 다시 계산 권장';
  if(/데이터가 없습니다/.test(m)) return '필요한 보조 데이터가 없어 이 전략 결과는 거의 의미 없음';
  return '';
}
function _pbtWarnHtml(ws, head){
  if(!ws||!ws.length) return head?`<div style="padding:8px 12px;border-radius:8px;background:rgba(16,185,129,.08);border:1px solid #065f46;color:#6ee7b7;font-size:13px">✓ ${head} — 데이터 부족 없음</div>`:'';
  const col={error:'#ef4444',warn:'#fbbf24',info:'#94a3b8'}, ic={error:'⛔',warn:'⚠',info:'ℹ'};
  const bad=ws.some(w=>w.level==='error'), seen=new Set();
  return `<div style="padding:8px 12px;border-radius:8px;background:${bad?'rgba(239,68,68,.08)':'rgba(251,191,36,.07)'};border:1px solid ${bad?'#7f1d1d':'#78350f'};font-size:13px;line-height:1.7">
    ${head?`<div style="font-weight:700;margin-bottom:2px">${head}</div>`:''}
    ${ws.map(w=>{
      // 같은 부족 구간(여러 전략이 같은 데이터를 씀)은 버튼을 한 번만 — 2026-10-18 수동 수집
      let btn=''; const c=w.collect;
      if(c){ const key=c.aux+c.start+c.end; if(!seen.has(key)){ seen.add(key);
        btn=` <button class="runbtn" style="padding:1px 8px;font-size:11px" onclick="pbtColFromWarn('${_pbtEsc(c.aux)}','${_pbtEsc(c.start)}','${_pbtEsc(c.end)}')">&#x1F4E5; 이 구간 수집</button>`; } }
      return `<div style="color:${col[w.level]||'#cbd5e1'}">${ic[w.level]||''} ${w.strategy?`<b>${_pbtEsc(w.strategy)}</b>: `:''}${_pbtEsc(w.msg)}${btn}</div>`;}).join('')}</div>`;
}
// ── 지난 계산 기록 (2026-10-19) — 기록마다 접었다 펴는 요약 + [표로 보기] [이 조건 불러오기] ──
let _pbtHist=[];
async function pbtHistLoad(silent){
  try{ const d=await fetch('/api/pbt/jobs').then(r=>r.json()); _pbtHist=(d&&d.jobs)||[]; }
  catch(e){ if(!silent) setHtml('pbt-hist',`<span style="color:#ef4444">기록 로드 실패: ${_pbtEsc(e.message)}</span>`); return; }
  setTxt('pbt-histsum', _pbtHist.length?`— ${_pbtHist.length}건 (최근 200건 보관)`:'— 아직 없음');
  if(silent) return;
  const f=(v,k)=>v==null||isNaN(v)?'-':(v>0?'+':'')+Number(v).toFixed(k==null?1:k)+'%';
  const st={done:'완료',running:'계산 중',error:'실패'};
  setHtml('pbt-hist', _pbtHist.length?_pbtHist.map(j=>{
    const t=j.top&&j.top[0];
    const head=`<b style="color:#e2e8f0">${_pbtEsc((j.started||'').slice(0,16))}</b>
      <span style="color:#94a3b8"> · ${_pbtD(j.start)} ~ ${_pbtD(j.end)} · ${j.names.length}전략${j.strength.length?' · 강도'+(j.strength_version==='v2'?'v2 ':' ')+j.strength.join('/'):''}${j.stop!=null?' · 손절 '+j.stop+'%':''}${j.take!=null?' · 익절 +'+j.take+'%':''}${j.luck?' · 운검정':''}</span>
      <span style="color:${j.status==='done'?'#10b981':j.status==='error'?'#ef4444':'#38bdf8'}"> · ${st[j.status]||_pbtEsc(j.status)}</span>
      ${t?`<span style="color:#cbd5e1"> · 1위 ${_pbtEsc(t.strategy)} <span class="${clr(t.cagr)}">${f(t.cagr)}</span></span>`:''}`;
    const rows=(j.top||[]).map((r,k)=>`<tr><td>${k+1}</td><td>${_pbtEsc(r.strategy)}</td><td class="r">${r.n_trades??'-'}</td>
      <td class="r ${clr(r.total_ret)}">${f(r.total_ret)}</td><td class="r ${clr(r.cagr)}" style="font-weight:700">${f(r.cagr)}</td>
      <td class="r neg">${f(r.mdd)}</td><td class="r">${r.sharpe==null?'-':Number(r.sharpe).toFixed(2)}</td></tr>`).join('');
    const L=j.luck_sum;
    return `<details class="pbt-hjob" style="border-top:1px solid #1e293b;padding:5px 0">
      <summary style="cursor:pointer;font-size:12px;line-height:1.7">${head}</summary>
      <div style="padding:6px 0 4px 14px;font-size:12px">
        ${j.error?`<div style="color:#ef4444">${_pbtEsc(j.error)}</div>`:''}
        ${rows?`<table style="font-size:12px;width:auto"><thead><tr><th>#</th><th>전략 (기본 줄 상위 5)</th><th class="r">매매수</th><th class="r">총수익</th>
          <th class="r"><span class="pbt-tip" data-tip="${_pbtEsc(_PBT_TIPS.cagr)}">CAGR</span></th><th class="r"><span class="pbt-tip" data-tip="${_pbtEsc(_PBT_TIPS.mdd)}">MDD</span></th>
          <th class="r"><span class="pbt-tip" data-tip="${_pbtEsc(_PBT_TIPS.sharpe)}">샤프</span></th></tr></thead><tbody>${rows}</tbody></table>`:'<span style="color:#64748b">결과 없음</span>'}
        <div style="color:#94a3b8;margin-top:4px">시장 CAGR ${f(j.market_cagr)} · 결과 ${j.n_results}줄 · 경고 ${j.n_warn}건${j.elapsed_sec!=null?' · '+j.elapsed_sec+'초':''}${L?` · 운 검정: SPA(현금) p=${L.spa_cash??'-'} · PBO ${L.pbo??'-'} · DSR ${L.dsr??'-'}`:''}</div>
        <div style="color:#64748b;margin-top:2px">전략: ${_pbtEsc(j.names.slice(0,12).join(', '))}${j.names.length>12?` 외 ${j.names.length-12}개`:''}</div>
        <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
          <button class="runbtn" onclick="pbtHistOpen('${_pbtEsc(j.job)}')">📊 전체 결과 표로 보기</button>
          <button class="runbtn" onclick="pbtHistReuse('${_pbtEsc(j.job)}')">↺ 이 조건 불러오기</button>
          <span style="color:#475569;align-self:center">${_pbtEsc(j.job)}</span></div>
      </div></details>`; }).join('') : '<span style="color:#64748b;font-size:12px">아직 계산한 기록이 없습니다</span>');
}
function pbtHistOpen(job){
  clearTimeout(_pbtTimer); pbtPoll(job);
  const el=document.getElementById('pbt-prog'); if(el) el.scrollIntoView({behavior:'smooth',block:'start'});
}
function pbtHistReuse(job){
  const j=_pbtHist.find(x=>x.job===job); if(!j) return;
  const s=document.getElementById('pbt-start'), e=document.getElementById('pbt-end');
  if(s&&j.start) s.value=_pbtD(j.start);
  if(e&&j.end) e.value=_pbtD(j.end);
  const want=new Set(j.names), have=new Set();
  document.querySelectorAll('#pbt-list input').forEach(i=>{ i.checked=want.has(i.value); if(i.checked) have.add(i.value); });
  const miss=j.names.filter(n=>!have.has(n)).length;
  const st=document.getElementById('pbt-strength'); if(st) st.value=(j.strength||[]).join(', ');
  const sp=document.getElementById('pbt-stop'); if(sp) sp.value=j.stop==null?'':j.stop;
  const tk=document.getElementById('pbt-take'); if(tk) tk.value=j.take==null?'':j.take;
  const em=document.getElementById('pbt-exitmode'); if(em) em.value=j.exit_mode||'close';
  const sv=document.getElementById('pbt-sver'); if(sv) sv.value=j.strength_version||'v1';
  const lk=document.getElementById('pbt-luck'); if(lk) lk.checked=!!j.luck;
  _pbtCount();
  setHtml('pbt-warn', `<span style="color:#38bdf8;font-size:13px">↺ ${_pbtEsc(job)} 의 조건을 불러왔습니다${miss?` (지금은 없는 전략 ${miss}개 제외)`:''} — [데이터 점검]·[실행]으로 다시 계산</span>`);
  const sec=document.getElementById('pbt-sec'); if(sec) sec.scrollIntoView({behavior:'smooth',block:'start'});
}
// ── 부족한 데이터 수동 수집 (2026-10-18) ──
let _pbtCol=null, _pbtColTimer=null;
async function pbtColLoad(){
  clearTimeout(_pbtColTimer);
  try{ _pbtCol=await fetch('/api/pbt/collect_status').then(r=>r.json()); }
  catch(e){ setHtml('pbt-col',`<span style="color:#ef4444">수집 현황 로드 실패: ${_pbtEsc(e.message)}</span>`); return; }
  pbtColRender();
  if(_pbtCol.running) _pbtColTimer=setTimeout(pbtColLoad,3000);
}
function pbtColRender(){
  const C=_pbtCol||{}, cov=C.coverage||{}, inp='background:#111827;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:2px 4px';
  const lab={foreign:'외국인',inst:'기관',individual:'개인',credit:'신용',short:'공매도'};
  const stTxt=s=>{ if(!s) return '<span style="color:#64748b">-</span>';
    const t=(s.tail||[]).slice(-1)[0]||'';
    if(s.status==='running') return `<span style="color:#38bdf8">수집 중 ${s.elapsed_sec!=null?Math.round(s.elapsed_sec/60)+'분':''}</span> <span style="color:#64748b">${_pbtEsc(t)}</span>`;
    if(s.status==='done') return `<span style="color:#10b981">✓ 완료 ${_pbtEsc(s.finished||'')}</span> <span style="color:#64748b">(${_pbtD(s.start)}~${_pbtD(s.end)})</span>`;
    return `<span style="color:#ef4444">${s.status==='stale'?'중단됨':'실패'} ${_pbtEsc(s.error||('rc='+s.rc))}</span> <span style="color:#64748b">${_pbtEsc(t)}</span>`; };
  const rows=(C.kinds||[]).map(k=>{
    const now=k.aux.map(a=>{ const c=cov[a]; return `${lab[a]||a} ${c?_pbtD(c[0])+' ~ '+_pbtD(c[1]):'<span style="color:#ef4444">없음</span>'}`; }).join('<br>');
    const busy=!!C.running;
    return `<tr><td style="font-weight:600;white-space:nowrap">${_pbtEsc(k.label)}<div style="color:#64748b;font-weight:400;font-size:11px">${_pbtEsc(k.src)}</div></td>
      <td style="white-space:nowrap;font-size:12px">${cov&&Object.keys(cov).length?now:'<span style="color:#64748b">[데이터 점검]을 한 번 누르면 계산됩니다</span>'}</td>
      <td style="white-space:nowrap"><input type="date" id="pbtc-s-${k.kind}" style="${inp}">${k.range?` ~ <input type="date" id="pbtc-e-${k.kind}" style="${inp}">`:''}</td>
      <td><button class="runbtn" ${busy?'disabled style="opacity:.5"':''} onclick="pbtCollect('${k.kind}')">수집</button></td>
      <td style="font-size:12px;max-width:360px">${stTxt(k.state)}<div style="color:#64748b;font-size:11px">${_pbtEsc(k.eta)}</div></td></tr>`; }).join('');
  // 부족분 한 번에 (collect_all.bat 과 같은 작업) — 단계별 완료/미완료
  const A=C.all, ic={done:'✅',partial:'⚠',failed:'❌',skipped:'⏭',running:'⏳',pending:'·'};
  const stepTxt=s=>{ const m=s.missing_n; let b='';
    if(s.status==='done') b='빠진 날 0'; else if(m!=null&&s.status!=='pending'&&s.status!=='running') b=`${m}/${s.n_days}거래일 미완료${s.ranges?': '+s.ranges:''}`;
    return `<div>${ic[s.status]||''} <b>${_pbtEsc(s.label)}</b> <span style="color:#64748b">${s.start&&s.kind!=='flow'?_pbtD(s.start)+'~'+_pbtD(s.end):''}</span> ${_pbtEsc(b)}${s.note?` <span style="color:#94a3b8">· ${_pbtEsc(s.note)}</span>`:''}</div>`; };
  const allBox=`<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
    <button class="runbtn" style="background:#2563eb;color:#fff;${C.running?'opacity:.5':''}" ${C.running?'disabled':''} onclick="pbtCollect('all')">&#x1F4E5; 부족분 한 번에 수집</button>
    <span style="font-size:12px;color:#94a3b8">${_pbtEsc(C.plan_note||'')} · 끝나면 텔레그램으로 완료/미완료 보고 · PC 에서 <code>collect_all.bat</code> 더블클릭도 같은 작업</span></div>`+
    (A?`<div style="font-size:12px;line-height:1.7;padding:6px 10px;background:#111827;border-radius:6px;margin-bottom:8px">
      <div style="color:#cbd5e1">${A.status==='running'?`⏳ 진행 중 — ${_pbtEsc(A.current||'')} ${A.elapsed_sec!=null?Math.round(A.elapsed_sec/60)+'분':''}`:A.status==='stale'?'<span style="color:#ef4444">중단됨</span>':_pbtEsc(A.summary||'')} <span style="color:#64748b">${_pbtEsc(A.started||'')}</span></div>
      ${(A.steps||[]).map(stepTxt).join('')}
      ${A.status==='running'&&(A.tail||[]).length?`<div style="color:#64748b">${_pbtEsc(A.tail.slice(-1)[0])}</div>`:''}</div>`:'');
  // 입력칸 값 유지(3초 폴링으로 다시 그려도 지우지 않음)
  const keep={}; document.querySelectorAll('#pbt-col input').forEach(i=>keep[i.id]=i.value);
  setHtml('pbt-col', allBox+`<table style="width:100%;font-size:13px"><thead><tr><th>데이터</th><th>지금 있는 구간</th><th>수집할 기간</th><th></th><th>상태 · 소요</th></tr></thead><tbody>${rows}</tbody></table>
    <div style="font-size:11px;color:#64748b;margin-top:6px">한 번에 한 종류만 수집합니다(KRX 차단·키움 키 충돌 방지). 끝나면 텔레그램으로 알리고, 데이터 구간을 다시 계산합니다 — 그다음 [데이터 점검]을 다시 누르세요. 이미 있는 날은 건너뜁니다.</div>`);
  Object.entries(keep).forEach(([id,v])=>{ const e=document.getElementById(id); if(e&&v) e.value=v; });
  setTxt('pbt-colsum', C.running?`— 수집 중: ${C.running}`:'');
}
async function pbtColFromWarn(aux,s,e){
  const box=document.getElementById('pbt-colbox'); box.open=true;
  if(!_pbtCol) await pbtColLoad();
  const kind=(_pbtCol&&_pbtCol.aux_to_kind||{})[aux]; if(!kind) return;
  const si=document.getElementById('pbtc-s-'+kind), ei=document.getElementById('pbtc-e-'+kind);
  if(si) si.value=_pbtD(s); if(ei) ei.value=_pbtD(e);
  box.scrollIntoView({behavior:'smooth',block:'center'});
  pbtCollect(kind);
}
async function pbtCollect(kind){
  if(kind==='all'){
    if(!confirm('부족한 데이터를 한 번에 수집할까요?\\n가격·외국인·기관 → 개인 순매수 → 공매도 → 신용잔고 순서, 빈 날만 받습니다.\\n신용은 수 시간 걸릴 수 있고, 장중(08:30~16:00)이면 신용만 건너뜁니다.\\n끝나면 텔레그램으로 완료/미완료를 보냅니다.')) return;
    const r=await fetch('/api/pbt/collect',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Action':'1'},body:JSON.stringify({kind:'all'})});
    let d={}; try{ d=await r.json(); }catch(x){ d={ok:false,msg:'응답 해석 실패 ('+r.status+')'}; }
    if(!d.ok) alert(d.msg||'수집 시작 실패');
    return pbtColLoad();
  }
  const k=((_pbtCol&&_pbtCol.kinds)||[]).find(x=>x.kind===kind); if(!k) return;
  // flow(가격·외국인·기관)는 기간을 안 쓰므로 비워도 오늘로 채운다
  const s=(document.getElementById('pbtc-s-'+kind)||{}).value||(kind==='flow'?new Date().toISOString().slice(0,10):''), ei=document.getElementById('pbtc-e-'+kind);
  const e=ei?(ei.value||''):s;
  if(!s||(k.range&&!e)){ alert('수집할 기간을 입력하세요'); return; }
  if(k.range&&s>e){ alert('시작일이 종료일보다 늦습니다'); return; }
  if(!confirm(`${k.label} 수집을 시작할까요?\n기간: ${s}${k.range?' ~ '+e:' 이후'}\n${k.eta}`)) return;
  const r=await fetch('/api/pbt/collect',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Action':'1'},
    body:JSON.stringify({kind, start:s.replace(/-/g,''), end:(e||s).replace(/-/g,'')})});
  let d={}; try{ d=await r.json(); }catch(x){ d={ok:false,msg:'응답 해석 실패 ('+r.status+')'}; }
  if(!d.ok) alert(d.msg||'수집 시작 실패');
  pbtColLoad();
}
async function pbtCheck(){
  setHtml('pbt-warn','<span style="color:#38bdf8;font-size:13px">데이터 점검 중… (처음엔 10~20초)</span>');
  const d=await _pbtPost('/api/pbt/check');
  if(d.ok===false&&!d.warnings){ setHtml('pbt-warn',`<span style="color:#ef4444">${_pbtEsc(d.msg||'점검 실패')}</span>`); return; }
  setHtml('pbt-warn', _pbtWarnHtml(d.warnings, `데이터 점검 — 거래일 ${d.n_days}일`+(d.ok?'':' (실행 불가)')));
}
async function pbtRun(){
  const b=_pbtBody(); if(!b.names.length){ setHtml('pbt-warn','<span style="color:#ef4444">전략을 하나 이상 고르세요</span>'); return; }
  setHtml('pbt-prog','<span style="color:#38bdf8">실행 요청 중…</span>');
  const d=await _pbtPost('/api/pbt/run');
  if(!d.ok){ setHtml('pbt-prog',`<span style="color:#ef4444">${_pbtEsc(d.msg||'실행 실패')}</span>`); if(d.job) pbtPoll(d.job); return; }
  setHtml('pbt-res',''); setHtml('pbt-luckbox',''); pbtPoll(d.job);
}
async function pbtPoll(job){
  clearTimeout(_pbtTimer);
  let d; try{ d=await fetch('/api/pbt/status?job='+encodeURIComponent(job)).then(r=>r.json()); }catch(e){ _pbtTimer=setTimeout(()=>pbtPoll(job),5000); return; }
  pbtRender(d);
  if(['running','starting'].includes(d.status)) _pbtTimer=setTimeout(()=>pbtPoll(job),3000);
  else if(d.status==='done'||d.status==='error'){ const hb=document.getElementById('pbt-histbox'); pbtHistLoad(!(hb&&hb.open)); }
}
function pbtRender(d){
  const p=d.params||{}, pr=d.progress||[0,0];
  const pct=pr[1]?Math.round(pr[0]/pr[1]*100):0;
  const stat={running:'계산 중',starting:'시작 중',done:'완료',error:'실패',missing:'결과 없음'}[d.status]||d.status;
  setHtml('pbt-prog', `<div style="display:flex;gap:10px;align-items:center">
    <b style="color:${d.status==='done'?'#10b981':d.status==='error'?'#ef4444':'#38bdf8'}">${stat}</b>
    <span>${p.start?_pbtD(p.start)+' ~ '+_pbtD(p.eff_end||p.end):''} · ${pr[0]}/${pr[1]}전략${d.current?' · '+_pbtEsc(d.current):''}${d.elapsed_sec!=null?' · '+d.elapsed_sec+'초':''}</span>
    <div style="flex:1;max-width:260px;height:6px;background:#1e293b;border-radius:3px"><div style="width:${pct}%;height:6px;background:#2563eb;border-radius:3px"></div></div>
    <span style="color:#64748b">${_pbtEsc(d.job||'')}</span></div>`+(d.error?`<div style="color:#ef4444;margin-top:4px">${_pbtEsc(d.error)}</div>`:''));
  if(d.warnings&&d.warnings.length) setHtml('pbt-warn', _pbtWarnHtml(d.warnings,'데이터·표본 경고 (텔레그램에도 전송됨)'));
  let rs=(d.results||[]).slice().sort((a,b)=>(b.cagr==null?-1e9:b.cagr)-(a.cagr==null?-1e9:a.cagr));
  if((p.strength||[]).length){   // 강도 비교: 기본 줄 CAGR 순 + 그 아래에 같은 전략의 강도 줄(낮은 임계부터)
    const base=rs.filter(r=>r.strength_min==null), sub={};
    rs.filter(r=>r.strength_min!=null).forEach(r=>{ (sub[r.base]=sub[r.base]||[]).push(r); });
    rs=base.flatMap(b=>[b,...(sub[b.strategy]||[]).sort((x,y)=>x.strength_min-y.strength_min)]);
  }
  if(!rs.length&&!d.market){ setHtml('pbt-res',''); return; }
  const years=[...new Set([...(d.market?Object.keys(d.market.yearly||{}):[]),...rs.flatMap(r=>Object.keys(r.yearly||{}))])].sort();
  const f=(v,k)=>v==null||isNaN(v)?'-':(v>0?'+':'')+Number(v).toFixed(k==null?2:k)+'%';
  const c=v=>v==null?'':clr(v);
  // [2026-10-22] 줄마다 경고 표시 — 수준별 아이콘(⛔ 오류/⚠ 경고/ℹ 참고) + 마우스 올리면 이유·뜻(전략 설명 팝업에 가리지 않게 pbt-tip 사용).
  //   강도 관련 경고는 강도 줄(└ 강도 ≥x)에, 나머지는 전략 줄에 붙인다.
  const wmap={}; (d.warnings||[]).forEach(w=>{ if(!w.strategy) return;
    const strength=w.row==='strength'||/강도 줄/.test(w.msg||'');
    const k=strength?'S:'+w.strategy:w.strategy; (wmap[k]=wmap[k]||[]).push(w); });
  const _wMark=ws=>{ if(!ws||!ws.length) return '';
    const lv=ws.some(w=>w.level==='error')?'error':ws.some(w=>w.level==='warn')?'warn':'info';
    const ic={error:'⛔',warn:'⚠',info:'ℹ'}[lv], col={error:'#ef4444',warn:'#fbbf24',info:'#60a5fa'}[lv];
    const head={error:'⛔ 오류 — 이 줄 결과를 믿기 어려움',warn:'⚠ 경고 — 데이터·표본이 모자라 결과가 덜 믿을 만함',info:'ℹ 참고 — 결과를 읽을 때 알아둘 점'}[lv];
    const lines=ws.map(w=>'· '+w.msg+(_pbtWarnMeaning(w.msg)?' → '+_pbtWarnMeaning(w.msg):''));
    return ` <span class="pbt-tip" data-tip="${_pbtEsc([head,...lines].join('|').replace(/"/g,"'"))}" style="color:${col};cursor:help">${ic}</span>`; };
  // ★ 는 '지금' 운용 중인 전략 기준(작업 파일의 live 는 실행 당시 값이라 교체 후엔 틀릴 수 있다)
  const _live=n=>{ const x=((_pbtMeta&&_pbtMeta.strategies)||[]).find(z=>z.name===n); return x?x.live:''; };
  const row=(r,isM)=>`<tr${r.strength_min!=null?' style="background:#0b1220;font-size:12px"':(!isM&&_live(r.strategy)?' style="background:rgba(251,191,36,0.06)"':'')}${isM?' style="background:#111827;font-style:italic"':''}>
    <td style="font-weight:500">${r.strength_min!=null?`<span style="color:#64748b;padding-left:14px">└ 강도 ≥${r.strength_min}</span> <span style="color:#64748b;font-size:11px" title="이 전략 신호 중 강도 기준을 넘은 비율">(통과 ${r.pass_pct??'-'}%)</span>${_wMark([...(wmap['S:'+r.base]||[]),...(wmap[r.strategy]||[])])}`:
      `${!isM&&_live(r.strategy)?'<span style="color:#fbbf24">★ </span>':''}${isM?_pbtEsc(r.name):`<a href="javascript:void(0)" onclick="pbtExplain('${_pbtEsc(r.strategy)}')" style="color:#93c5fd;text-decoration:underline dotted" title="눌러서 전략 설명 보기">${_pbtEsc(r.strategy)}</a>`}`}${r.strength_min==null?_wMark(wmap[r.strategy]):''}${r.error?` <span style="color:#ef4444" title="${_pbtEsc(r.error)}">오류</span>`:''}</td>
    <td class="r" style="color:#94a3b8">${isM?'-':(r.n_trades??'-')}${!isM&&r.n_open?`<span style="font-size:10px" title="기간 안에 신호가 났지만 종료일 뒤에 청산되는 매매 — 신호 평균·승률에서 제외, 자본곡선에선 실제로 보유 중인 것만 종료일 종가로 평가"> (미완료 ${r.n_open})</span>`:''}</td>
    <td class="r">${isM||r.win==null?'-':r.win.toFixed(1)+'%'}</td>
    <td class="r ${c(r.avg_net)}">${isM?'-':f(r.avg_net)}</td>
    <td class="r ${c(r.slot_avg)}" style="font-weight:600">${isM?'-':f(r.slot_avg)}</td>
    <td class="r ${c(r.total_ret)}">${f(r.total_ret,1)}</td>
    <td class="r ${c(r.cagr)}" style="font-weight:800">${f(r.cagr,1)}</td>
    <td class="r neg">${f(r.mdd,1)}</td>
    <td class="r">${r.sharpe==null?'-':Number(r.sharpe).toFixed(2)}</td>
    <td class="r ${c(r.vs_mkt)}">${isM?'기준':f(r.vs_mkt,1)+'p'}</td>
    ${years.map(y=>`<td class="r ${c((r.yearly||{})[y])}">${f((r.yearly||{})[y],1)}</td>`).join('')}</tr>`;
  const th=(k,lab)=>`<th class="r"><span class="pbt-tip" data-tip="${_pbtEsc(_PBT_TIPS[k])}">${lab}</span></th>`;
  _pbtLastRes=d;
  setHtml('pbt-res', `<table><thead><tr><th><span class="pbt-tip" data-tip="${_pbtEsc(_PBT_TIPS.strategy)}">전략</span></th>${th('n','매매수')}${th('win','승률')}${th('avg','신호 평균')}${th('slot','슬롯 실매매')}
    ${th('tot','총수익')}${th('cagr','CAGR')}${th('mdd','MDD')}${th('sharpe','샤프')}${th('vs','시장 대비')}${years.map(y=>`<th class="r"><span class="pbt-tip" data-tip="${_pbtEsc(_PBT_TIPS.year)}">${y}</span></th>`).join('')}</tr></thead>
    <tbody>${d.market?row(d.market,true):''}${rs.map(r=>row(r,false)).join('')}</tbody></table>
    <div style="font-size:11px;color:#64748b;margin-top:6px">전략마다 10슬롯 단독 · 다음날 시가 진입·보유일 만기${(p.stop!=null||p.take!=null)?'':'·손절/익절 없음'}·비용 왕복 ≈0.33%(연도별 거래세) · 상한가 시초가 미체결 · 상폐·정지는 마지막 거래가 청산 · 분할·병합 낀 신호 제외 · 일별 평가(MTM) · 시장 = 코스닥 유니버스 동일가중 · 연도별은 기간 안 각 해의 자본 증감${(p.strength||[]).length?` · <b>강도 줄(${_pbtEsc(p.strength_version||'v1')})</b> = 강도 기준 넘은 신호만, 강도 높은 순으로 담음(기본 줄은 강도 무관·전략 점수순)`:''}${(p.stop!=null||p.take!=null)?` · <b>청산</b>: ${p.stop!=null?'손절 '+p.stop+'%':''}${p.stop!=null&&p.take!=null?' / ':''}${p.take!=null?'익절 +'+p.take+'%':''} (${p.exit_mode==='intraday'?'장중 도달 → 그 가격':'종가 판정 → 다음날 시가'})`:''}</div>
    ${d.strength_info&&Object.keys(d.strength_info).length?`<div style="font-size:11px;color:#64748b;margin-top:3px">강도 ${_pbtEsc(p.strength_version||'v1')} 가중치(워크포워드) — ${
      p.strength_version==='v2'
        ? Object.entries(d.strength_info).map(([n,yy])=>`<b>${_pbtEsc(n)}</b>: `+Object.entries(yy||{}).map(([y,v])=>v.skipped||!v.n_ic?`${y} 채점 안 함`:`${y} 지표 ${v.n_ic}개`).join(', ')).join(' · ')
        : Object.entries(d.strength_info).map(([y,v])=>v.skipped?`${y}: 학습 ${v.n_train}건 부족 → 채점 안 함`:`${y}: ${y}년 이전 매매 ${Number(v.n_train).toLocaleString()}건으로 학습(${_pbtEsc((v.top||[]).join(', '))})`).join(' · ')}</div>`:''}`);
  const L=d.luck;
  if(L&&!L.error&&L.pbo!=null){
    const pass=(L.spa_cash!=null&&L.spa_cash<0.05)||(L.dsr!=null&&L.dsr>=0.95);
    setHtml('pbt-luckbox', `<div style="padding:10px 14px;border-radius:8px;background:#0f172a;border:1px solid #1e293b;font-size:13px;line-height:1.8">
      <b>운 검정</b> <span style="color:#64748b">(${L.n}전략 · ${L.T}거래일)</span> —
      <b style="color:${pass?'#10b981':'#fbbf24'}">${pass?'1등이 운이 아닐 근거 있음':'1등을 운과 구분할 수 없음'}</b><br>
      SPA p값: 현금 대비 <b>${L.spa_cash??'-'}</b> · 시장 대비 <b>${L.spa_market??'-'}</b> (0.05 미만이면 '이기는 전략이 있다') ·
      StepM 우월: ${(L.stepm_cash||[]).length?_pbtEsc(L.stepm_cash.join(', ')):'없음'}<br>
      PBO <b>${L.pbo}</b> (앞 절반 1등이 뒤 절반 하위로 떨어지는 비율, 0.5=동전던지기) · 앞 1등의 뒤 샤프&lt;0 확률 ${L.is_best_oos_neg}<br>
      DSR(${_pbtEsc(L.best)}, 연샤프 ${L.best_sharpe}) <b>${L.dsr}</b> — 고른 전략 수만큼 '운만으로' 나올 최대 연샤프 ${L.luck_max_sharpe} (0.95 이상이어야 유의)<br>
      <span style="color:#64748b;font-size:12px">※ 다른 결과를 보고 골라낸 전략들이면 실제 시도 수는 더 많다 — 그만큼 DSR·SPA 는 낙관적이다(55전략 기준 결과는 메모 §52)</span>
      ${L.spa_error?`<br><span style="color:#ef4444">${_pbtEsc(L.spa_error)}</span>`:''}</div>`);
  } else if(L&&L.error){ setHtml('pbt-luckbox',`<span style="color:#ef4444">운 검정 실패: ${_pbtEsc(L.error)}</span>`); }
  else setHtml('pbt-luckbox','');
}

// [2026-10-12] 전체 전략 과거 백테스트(strategy_engine --full) — CAGR 내림차순, ★ 라이브
function fillStrategyFull(sf){
  const rows=(sf&&sf.rows)||[];
  const n=v=>(v===null||v===undefined||v==='')?null:Number(v);
  const f=(v,d)=>{const x=n(v);return x===null||isNaN(x)?'-':(x>0?'+':'')+x.toFixed(d===undefined?2:d)+'%';};
  const c=v=>{const x=n(v);return x===null||isNaN(x)?'':clr(x);};
  const done=rows.filter(r=>n(r.n_trades)!==null).length;
  setHtml('sfull-meta', sf&&sf.updated?`— ${sf.updated} 계산 · 기간 ${sf.period||'-'} · 최근2년 기준 ${sf.oos_cutoff||'-'}~ · ${done}/${rows.length}전략`+
    (done<rows.length?' <span style="color:#fbbf24">(미계산 '+(rows.length-done)+'개 — 다시 계산 필요)</span>':''):'');
  setHtml('sfull-rows', rows.length?rows.map(r=>{
    if(n(r.n_trades)===null) return `<tr><td style="color:#94a3b8">${r.live?'★ ':''}${r.strategy}</td><td colspan="11" style="color:#64748b;font-size:12px">${r.error||'미계산'}</td></tr>`;
    const live=r.live?`<span title="${r.live}" style="color:#fbbf24">★ </span>`:'';
    const gap=n(r.slot_avg_net)!==null&&n(r.avg_net)!==null&&(n(r.avg_net)-n(r.slot_avg_net))>=1;
    return `<tr${r.live?' style="background:rgba(251,191,36,0.06)"':''}>
      <td style="font-weight:500">${live}${r.strategy}${r.live?`<div style="font-size:10px;color:#94a3b8">${r.live}</div>`:''}</td>
      <td class="r" style="color:#94a3b8">${Number(r.n_trades).toLocaleString()}</td>
      <td class="r">${n(r.win_rate)!==null?Number(r.win_rate).toFixed(1)+'%':'-'}</td>
      <td class="r ${c(r.avg_net)}">${f(r.avg_net)}</td>
      <td class="r ${c(r.slot_avg_net)}" style="font-weight:600${gap?';text-decoration:underline dotted':''}" title="${gap?'신호 평균보다 1%p 이상 낮음 — 슬롯이 사는 매매가 더 나쁘다':''}">${f(r.slot_avg_net)}</td>
      <td class="r ${c(r.real_cagr_pct)}" style="font-weight:800">${f(r.real_cagr_pct,1)}</td>
      <td class="r neg">${f(r.real_mdd_pct,1)}</td>
      <td class="r">${n(r.real_sharpe)!==null?Number(r.real_sharpe).toFixed(2):'-'}</td>
      <td class="r ${c(r.is_avg_net)}">${f(r.is_avg_net)}</td>
      <td class="r ${c(r.oos_avg_net)}" style="font-weight:600">${f(r.oos_avg_net)}</td>
      <td class="r ${c(r.oos_cagr_pct)}">${f(r.oos_cagr_pct,1)}</td>
      <td class="r" style="color:#94a3b8">${n(r.avg_hold)!==null?Number(r.avg_hold).toFixed(0)+'d':'-'}</td></tr>`;
  }).join(''):'<tr><td colspan="12" style="color:#9ca3af;text-align:center">[다시 계산] 버튼 또는 python strategy_engine.py --full</td></tr>');
}

function fillLab(lab){
  const rows=((lab&&lab.rows)||[]).slice(0,200);
  setHtml('lab-rows', rows.length?rows.map(r=>
    `<tr>
      <td style="font-weight:500">${r.strategy||''}</td>
      <td class="r" style="color:#94a3b8">${r.bt_n||0}</td>
      <td class="r ${clr(r.bt_avg_net)}" style="font-weight:600">${Number(r.bt_avg_net||0).toFixed(2)}%</td>
      <td class="r">${r.fwd_n||0}</td>
      <td class="r ${clr(r.fwd_avg_net)}" style="font-weight:600">${r.fwd_n?Number(r.fwd_avg_net||0).toFixed(2)+'%':'-'}</td>
      <td class="r ${clr(r.gap_avg_net)}" style="font-weight:800;font-size:14px">${r.gap_avg_net!=null?((r.gap_avg_net>=0?'+':'')+Number(r.gap_avg_net).toFixed(2)+'%p'):'-'}</td>
      <td class="r ${clr(r.slot_total_ret)}" style="font-weight:600">${Number(r.slot_total_ret||0).toFixed(1)}%</td>
      <td class="r neg" style="font-weight:600">${Number(r.slot_mdd||0).toFixed(1)}%</td></tr>`
  ).join(''):'<tr><td colspan="8" style="color:#9ca3af;text-align:center">strategy_lab.py 실행 대기 (BT는 즉시, FWD/갭은 누적)</td></tr>');
}

function fillAIRegistry(reg){
  const rows=(reg&&reg.rows)||[];
  setHtml('ai-reg-rows', rows.length?rows.map(r=>{
    const grp=String(r.groups||'').replace(/\\|/g,', ');
    const sp=r.spread_pp;
    const spc=(sp!==''&&sp!=null)?(Number(sp)>=3?'pos':(Number(sp)<0?'neg':'')):'';
    return `<tr>
      <td class="r" style="font-size:12px;color:#cbd5e1">${r.run_date||''}</td>
      <td style="font-size:12px;color:#94a3b8">${r.engine||''}</td>
      <td style="font-size:12px;color:#cbd5e1">${grp||'-'}</td>
      <td class="r" style="font-weight:500">${r.n_features||''}</td>
      <td class="r" style="font-weight:600;color:#38bdf8">${(r.test_auc!==''&&r.test_auc!=null)?Number(r.test_auc).toFixed(3):'-'}</td>
      <td class="r ${spc}" style="font-weight:700">${(sp!==''&&sp!=null)?((Number(sp)>=0?'+':'')+Number(sp).toFixed(2)):'-'}</td>
      <td class="r" style="font-weight:600">${(r.cv_best!==''&&r.cv_best!=null)?Number(r.cv_best).toFixed(2):'-'}</td>
      <td class="r" style="font-size:12px;color:#94a3b8">${fmt(r.train_n)}/${fmt(r.test_n)}</td></tr>`;
  }).join(''):'<tr><td colspan="8" style="color:#9ca3af;text-align:center">아직 기록 없음 &mdash; 다음 AI 학습 때부터 쌓입니다</td></tr>');
}

function fillAI(ai){
  const errBox=document.getElementById('ai-err-box');
  if(!ai){errBox.style.display='flex';errBox.textContent='AI 데이터 없음';return;}
  if(ai.error){errBox.style.display='flex';errBox.textContent='API 오류: '+ai.error;return;}
  errBox.style.display='none';
  const m=ai.model||{};
  setTxt('ai-date', safe(m.last_trained,'-').slice(0,16));
  setTxt('ai-age', m.age_days!=null?m.age_days+'일':'-');
  setTxt('ai-auc', safe(m.auc,'-'));
  setTxt('ai-fc', safe(m.feature_count,'-'));
  const ic=ai.ic||{};
  if(ic.weights&&ic.weights.length){
    setHtml('ai-ic', ic.weights.map(w=>{
      const bar=Math.round(Math.abs(w.ic)*100);
      const barHtml=`<div style="display:inline-block;width:${bar}px;height:8px;background:${w.ic>=0?'#10b981':'#ef4444'};border-radius:4px;box-shadow:0 1px 2px rgba(0,0,0,0.2)"></div>`;
      return `<tr><td style="font-family:'JetBrains Mono',monospace;font-size:13px;color:#93c5fd">${w.feat}</td>
        <td style="color:#cbd5e1;font-size:12px">${w.label||''}</td>
        <td class="r ${clr(w.ic)}" style="font-weight:600;font-size:14px">${w.ic>=0?'+':''}${w.ic}</td>
        <td>${barHtml}</td></tr>`;
    }).join(''));
  } else {
    setHtml('ai-ic',`<tr><td colspan="4" style="color:#ef4444">${ic.error||'IC 가중치 없음 — scipy 설치 필요: pip install scipy'}</td></tr>`);
  }
  const db=ai.db||{};
  setTxt('ai-db-info', db.size_mb?`stock.db: ${db.size_mb} MB`:(db.error||'DB 없음'));
  const dbTables=['korea_stocks','usa_stocks','supply_demand','macro_indicators','credit_balance','news'];
  setHtml('ai-db-rows', dbTables.map(t=>{
    const info=db[t]||{};
    return `<tr><td style="font-weight:500">${t}</td><td class="r">${fmt(info.count)}</td><td class="r" style="font-size:12px;color:#94a3b8">${safe(info.latest,'-')}</td></tr>`;
  }).join(''));
  const news=ai.news||{};
  const sent=news.sentiment||{};
  setTxt('ai-news-pos', safe(sent.positive,'0'));
  setTxt('ai-news-neg', safe(sent.negative,'0'));
  setTxt('ai-news-neu', safe(sent.neutral,'0'));
  setTxt('ai-news-tot', safe(news.week_total,'0'));
  setHtml('ai-news-rows', (news.recent||[]).map(r=>{
    const sc=r.sentiment||'';
    const cls=sc==='positive'?'pos':sc==='negative'?'neg':'neu';
    const bgCls=sc==='positive'?'rgba(16,185,129,0.1)':sc==='negative'?'rgba(239,68,68,0.1)':'rgba(148,163,184,0.1)';
    return `<tr><td style="font-size:12px;font-weight:600;color:#93c5fd">${r.ticker||''}</td>
      <td style="font-size:13px;color:#e2e8f0;line-height:1.4">${r.title||''}</td>
      <td class="${cls}" style="font-size:12px;font-weight:600;"><span style="background:${bgCls};padding:2px 8px;border-radius:4px">${sc}</span></td>
      <td class="r" style="font-size:12px;color:#94a3b8">${r.date||''}</td>
      <td style="font-size:12px;color:#94a3b8">${r.source||''}</td></tr>`;
  }).join(''));
}

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

// 주의: 브라우저 confirm() 모달은 '추가 대화상자 표시 차단'(반복 시 크롬/엣지가 제안)에
// 걸리면 아무것도 안 띄우고 즉시 false 를 반환 → 버튼이 죽은 것처럼 보인다
// (2026-07-17 실사례: 수동키 무반응 신고 — 서버 로그엔 요청 자체가 없었음).
// 모달 없이 버튼을 3초간 '한번 더 클릭' 상태로 무장시키는 방식으로 교체.
const _armTimers={};
function runTask(task, label, reloadMs){
  const btn=(typeof event!=='undefined'&&event)?event.target:null;
  if(btn && !btn.dataset.armed){
    btn.dataset.armed='1'; btn.dataset.orig=btn.textContent;
    btn.textContent='한번 더 클릭=실행'; btn.style.background='#f59e0b'; btn.style.color='#0b0f19';
    _armTimers[task]=setTimeout(()=>{ delete btn.dataset.armed;
      btn.textContent=btn.dataset.orig; btn.style.background=''; btn.style.color=''; },3000);
    return;
  }
  if(btn){ clearTimeout(_armTimers[task]); delete btn.dataset.armed;
    btn.textContent=btn.dataset.orig||btn.textContent; btn.style.background=''; btn.style.color=''; }
  const el=document.getElementById('run-status');
  if(el) el.innerHTML=`<span style="color:#38bdf8">${label} 시작 요청 중...</span>`;
  fetch('/api/run/'+task,{method:'POST',headers:{'X-Dashboard-Action':'1'}}).then(r=>r.json()).then(d=>{
    if(el) el.innerHTML=`<span style="${d.ok?'color:#10b981':'color:#ef4444'}">[${label}] ${d.msg||(d.ok?'시작됨':'실패')}</span> <span style="color:#64748b;font-size:11px">(${new Date().toLocaleTimeString()})</span>`;
    if(d.ok){ setTimeout(()=>{ refreshLogFiles('run_'+task+'.log').then(()=>loadLogFile()); }, 2000); }
    if(reloadMs && d.ok){ if(el) el.innerHTML+=` <span style="color:#94a3b8">— ${(reloadMs/1000)}초 후 갱신</span>`; setTimeout(()=>load(), reloadMs); }
  }).catch(e=>{ if(el) el.innerHTML=`<span style="color:#ef4444">[${label}] 오류: ${e.message}</span>`; });
}

function fillLogs(logs, files){
  const schEl=document.getElementById('log-sch');
  const liveEl=document.getElementById('log-live');
  if(schEl) schEl.textContent=(logs&&logs.sch)||'(수집 로그 없음)';
  if(liveEl) liveEl.textContent=(logs&&logs.live)||'(페이퍼 로그 없음)';
  setHtml('dl-files', (files||[]).map(f=>{
    const ok=f.exists===true||f.exists===undefined;
    const cls=ok?'file-ok':'file-err';
    return `<tr>
      <td style="font-family:'JetBrains Mono',monospace;font-size:13px;color:#93c5fd">${f.name||''}</td>
      <td class="r" style="color:#94a3b8;font-weight:500">${f.size_kb||f.size_mb||'?'}${f.size_mb?'MB':'KB'}</td>
      <td style="font-size:12px;color:#cbd5e1">${f.mtime||''}</td>
      <td class="${cls}" style="font-size:14px;font-weight:800">${ok?'✓':'✗'}</td></tr>`;
  }).join('')||'<tr><td colspan="4" style="color:#9ca3af;text-align:center">파일 목록 없음</td></tr>');
}

async function refreshIntraday(){
  try{
    const d=await fetch('/api/intraday/refresh',{method:'POST',headers:{'X-Dashboard-Action':'1'}}).then(r=>r.json());
    fillIntraday(d);
  }catch(e){
    const eb=document.getElementById('intra-err');
    if(eb){eb.style.display='flex';eb.textContent='갱신 실패: '+e.message;}
  }
}

function fillIntraday(d){
  const errEl=document.getElementById('intra-err');
  if(!d||d.error){
    if(errEl){errEl.style.display='flex';errEl.textContent=(d&&d.error)||'장중 데이터 없음 (intraday_monitor.py 실행 필요)';}
    return;
  }
  if(errEl)errEl.style.display='none';
  setTxt('intra-ts', d.updated_at||'');
  const data=d.data||{};
  const statsRows=[
    {l:'KOSPI', v:data.kospi_price, c:data.kospi_chg_pct},
    {l:'KOSDAQ', v:data.kosdaq_price, c:data.kosdaq_chg_pct},
    {l:'USD/KRW', v:data.usdkrw, c:data.usdkrw_chg},
  ];
  setHtml('intra-stats', statsRows.map(r=>
    `<div class="stat"><div class="v ${clr(r.c)}">${fmt(r.v)}</div><div class="l">${r.l} <span style="font-size:11px;color:${clr(r.c)==='pos'?'#10b981':'#ef4444'};text-transform:none">(${r.c!=null?(Number(r.c)>=0?'+':'')+Number(r.c).toFixed(2)+'%':'-'})</span></div></div>`
  ).join(''));
  const idxFields=[
    ['KOSPI', data.kospi_price, data.kospi_chg_pct],
    ['KOSDAQ', data.kosdaq_price, data.kosdaq_chg_pct],
    ['USD/KRW', data.usdkrw, data.usdkrw_chg],
    ['외국인 순매수', data.for_net_buy, null],
    ['기관 순매수', data.inst_net_buy, null],
  ];
  setHtml('intra-idx', idxFields.map(([l,v,c])=>
    `<tr><td style="font-weight:600;color:#e2e8f0">${l}</td><td class="r" style="font-size:14px">${fmt(v)}</td>
     <td class="r ${c!=null?clr(c):'neu'}" style="font-weight:700">${c!=null?(Number(c)>=0?'+':'')+Number(c).toFixed(2)+'%':'-'}</td></tr>`
  ).join(''));
}

let _lastData=null;
async function load(){
  const ts=document.getElementById('ts');
  if(ts)ts.textContent='갱신 중...';
  let d=null;
  try{ d=await fetch('/api/all').then(r=>r.json()); }
  catch(e){ if(ts)ts.textContent='로드 실패: '+e.message; return; }
  _lastData=d;
  if(ts)ts.textContent=d.ts||new Date().toLocaleString();
  const fb=document.getElementById('freshness-banner');
  if(fb){
    const hs=(d.health||[]);
    if(hs.length){
      const hasErr=hs.some(h=>h.sev==='err');
      fb.innerHTML='<div class="'+(hasErr?'fresh-err':'fresh-warn')+'" style="display:flex;align-items:center;gap:8px;background:'+(hasErr?'rgba(239,68,68,0.1)':'rgba(245,158,11,0.1)')+';padding:8px 20px;border-bottom:1px solid '+(hasErr?'rgba(239,68,68,0.3)':'rgba(245,158,11,0.3)')+'"><span style="font-size:16px">⚠</span> 점검 필요: '
        +hs.map(h=>h.msg).join(' &nbsp;|&nbsp; ')+'</div>';
    } else {
      fb.innerHTML='<div class="fresh-ok" style="display:flex;align-items:center;gap:8px;background:rgba(16,185,129,0.1);padding:8px 20px;border-bottom:1px solid rgba(16,185,129,0.3)"><span style="font-size:16px">✓</span> 시스템 정상 (테이블·신호 이상 없음)</div>';
    }
  }
  const _r=(fn,...a)=>{try{fn(...a);}catch(e){console.error('[dashboard] render 오류 ('+fn.name+'):',e);}};
  _r(fillOverview, d.cheonok, d.ai, d.candidates);
  _r(fillCheonok, d.cheonok);
  _r(fillMock, d.mock, d.kis);
  _r(fillStrength, d.strength);
  _r(fillCutMonitor, d.cutmon);
  _r(fillAiPaper, d.ai_paper);
  _r(fillRunButtons, d);
  _r(fillBacktest, d.bt, d.sc);
  _r(fillLab, d.lab);
  _r(fillStrategyFull, d.sfull);
  _r(fillAI, d.ai);
  _r(fillAIRegistry, d.ai_registry);
  _r(fillLogs, d.logs, d.files);
  fetch('/api/intraday').then(r=>r.json()).then(fillIntraday).catch(()=>{});
  fetch('/api/preview').then(r=>r.json()).then(fillPreview).catch(()=>{});
  fetch('/api/marketgrid').then(r=>r.json()).then(fillMarketGrid).catch(()=>{});
}

function _pctS(v){v=Number(v||0);return '<span class="'+(v>=0?'pos':'neg')+'" style="font-weight:600">'+(v>=0?'+':'')+v.toFixed(2)+'%</span>';}
function _amtS(v){v=Number(v||0);return '<span class="'+(v>=0?'pos':'neg')+'" style="font-weight:600">'+(v>=0?'+':'')+v.toLocaleString()+'</span>';}
const _GRID=[
  ['volume','거래량', r=>_pctS(r.change_pct)],
  ['change','상승률', r=>_pctS(r.change_pct)],
  ['netbuy','기관+외인 순매수(천만)', r=>_amtS(r.net)],
  ['netsell','기관+외인 순매도(천만)', r=>_amtS(r.net)],
  ['new_high','신고가', r=>_pctS(r.change_pct)],
  ['sector','업종등락', r=>_pctS(r.change_pct)],
];
function _gridCard(title, rows, valf){
  const inner=(rows&&rows.length)
    ? rows.slice(0,20).map((r,i)=>`<div style="display:flex;justify-content:space-between;gap:6px;font-size:12px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
        <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#cbd5e1"><span style="color:#64748b;margin-right:4px">${i+1}.</span>${r.name||r.code||''}</span>
        <span style="white-space:nowrap">${valf(r)}</span></div>`).join('')
    : '<div style="color:#64748b;font-size:12px;padding:4px 0">-</div>';
  return `<div style="flex:1;min-width:180px;background:#1f2937;border:1px solid #374151;border-radius:10px;padding:12px;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
    <div style="font-size:12px;color:#9ca3af;margin-bottom:8px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">${title}</div>
    <div style="max-height:120px;overflow-y:auto;padding-right:4px">${inner}</div></div>`;
}
function fillMarketGrid(d){
  const el=document.getElementById('market-grid'); if(!el) return;
  const ts=document.getElementById('grid-ts'); if(ts) ts.textContent=d&&d.updated?'('+d.updated+')':'';
  const mk=(d&&d.markets)||{};
  if(!Object.keys(mk).length){ el.innerHTML='<div style="color:#9ca3af;font-size:13px">장중 갱신 대기 (market_grid.py)</div>'; return; }
  let html='';
  for(const [key,label] of [['kospi','코스피'],['kosdaq','코스닥']]){
    const m=mk[key]||{};
    html+=`<div style="font-size:14px;color:#38bdf8;font-weight:700;margin:16px 0 8px;display:flex;align-items:center;gap:8px">${label}${m.error?' <span style="color:#ef4444;font-size:12px;font-weight:normal">'+m.error+'</span>':''}</div>`;
    html+='<div style="display:flex;flex-wrap:wrap;gap:12px">'+
      _GRID.map(([k,t,vf])=>_gridCard(t, m[k], vf)).join('')+'</div>';
  }
  el.innerHTML=html;
}

const _STRAT_KR={high_500d_filt:'500일신고가',high_52w_filt:'52주신고가',rsi_reversal:'RSI반전',rsi_vol:'RSI+거래량'};
function fillPreview(d){
  const tb=document.getElementById('prev-rows'); if(!tb) return;
  const ts=document.getElementById('prev-ts');
  if(ts) ts.textContent=d&&d.updated?'('+d.updated+' 기준)':'';
  const items=(d&&d.items)||[];
  if(!items.length){
    tb.innerHTML='<tr><td colspan="3" style="color:#9ca3af;text-align:center">잠정 후보 없음 (장중 갱신)</td></tr>';
    return;
  }
  tb.innerHTML=items.slice(0,40).map(x=>
    `<tr><td style="font-size:12px;color:#9ca3af;font-weight:500">${_STRAT_KR[x.strategy]||x.strategy}</td>
     <td style="font-weight:500">${x.name||''} <span style="color:#64748b;font-size:12px;font-weight:normal;margin-left:4px">${x.code||''}</span></td>
     <td class="r" style="font-weight:600;color:#e2e8f0">${fmt(x.price)}</td></tr>`
  ).join('');
}

// ── 표 정렬(헤더 더블클릭) ─────────────────────────────────────────────────
// [2026-08-09 신설] 천억이 활성신호 / 모의계좌 매매내역·보유포지션 표를 정렬 가능하게.
// 강도매매 표(sortSt)는 컬럼마다 onclick 을 배선하는 방식이라 표가 늘 때마다 손이 가서,
// 여기서는 '헤더 위치(index)로 DOM 행을 정렬'하는 범용 방식을 쓴다 — 새 표가 생겨도
// 아래 목록에 id 만 추가하면 된다.
// 대시보드는 5분마다 자동 재렌더(innerHTML 교체)되므로 MutationObserver 로 정렬을
// 다시 적용한다. 재적용이 또 관찰을 부르는 무한루프는 _tblSorting 플래그로 차단.
// 정렬·필터 대상 표(각 탭 주요 표). 새 표는 여기에 id 만 추가하면 된다.
const SORTABLE_TBODIES = [
  'ov-cand',                        // 개요: 오늘의 후보
  'ch-signals',                     // 천억이: 활성 신호
  'mock-pos','mock-orders','mock-stratperf',   // 모의계좌(키움)
  'kis-pos','kis-orders','kis-stratperf',      // 모의계좌(KIS)
  'st-rows','st-bystrat','ap-pos',  // 강도매매
  'sc-rows','bt-rows','lab-rows',   // 전략/백테스트/전략랩
  'dl-files',                       // 데이터+로그: 파일 목록
];
const _tblSort = {};      // tbodyId -> {idx, asc}
const _tblFilter = {};    // tbodyId -> 소문자 검색어
let _tblSorting = false;

function _cellKey(td){
  const t=(td?td.textContent:'').trim();
  if(!t||t==='-') return {n:null,s:''};
  // "1,234" "+5.20%" "-12,345원" "6.11" 같은 표기를 숫자로 인식(빈칸·기호 제거)
  const c=t.replace(/[,\\s원주%]/g,'').replace(/^\\+/,'');
  const n=Number(c);
  return (c!==''&&!isNaN(n))?{n:n,s:t}:{n:null,s:t};
}

function _applyTblSort(id){
  const st=_tblSort[id]; if(!st) return;
  const tb=document.getElementById(id); if(!tb) return;
  const rows=Array.from(tb.children).filter(r=>r.tagName==='TR'&&r.children.length>st.idx);
  if(rows.length<2) return;   // '데이터 없음' 안내행(colspan)은 자동 제외됨
  const orig=rows.slice();
  rows.sort((a,b)=>{
    const A=_cellKey(a.children[st.idx]), B=_cellKey(b.children[st.idx]);
    let c;
    if(A.n!==null&&B.n!==null) c=A.n-B.n;                 // 숫자는 수치 비교
    else if(A.n!==null) c=-1;                              // 숫자를 빈값보다 앞에
    else if(B.n!==null) c=1;
    else c=A.s.localeCompare(B.s,'ko');                    // 한글 사전순
    return st.asc?c:-c;
  });
  // 이미 정렬된 상태면 DOM 을 건드리지 않는다 — MutationObserver 콜백은 '비동기'라
  // 아래 _tblSorting 플래그만으로는 재귀를 막지 못한다(플래그가 false 로 돌아간 뒤
  // 콜백이 실행 → 다시 정렬 → 또 콜백 … 무한루프로 탭이 멈춤. 2026-08-09 실측).
  // 순서 변화가 없으면 변이도 없으므로 이 조건이 루프의 종료점이 된다.
  if(rows.every((r,i)=>r===orig[i])) return;
  _tblSorting=true;
  rows.forEach(r=>tb.appendChild(r));
  _tblSorting=false;
}

function _markTblHeaders(tbl,id){
  const st=_tblSort[id];
  tbl.querySelectorAll('thead th').forEach((th,i)=>{
    if(th.dataset.orig===undefined) th.dataset.orig=th.textContent;
    th.textContent=th.dataset.orig+((st&&st.idx===i)?(st.asc?' ▲':' ▼'):'');
  });
}

// ── 필터: 표 위 검색창으로 행 숨기기(정렬과 독립적으로 동작) ──
function _applyTblFilter(id){
  const q=_tblFilter[id]||'';
  const tb=document.getElementById(id); if(!tb) return;
  let shown=0, total=0;
  Array.from(tb.children).forEach(r=>{
    if(r.tagName!=='TR') return;
    if(r.children.length<=1) return;          // '데이터 없음' 안내행(colspan)은 대상 아님
    total++;
    const hit=!q||r.textContent.toLowerCase().includes(q);
    r.style.display=hit?'':'none';            // 숨김만 — 정렬 로직은 그대로 유지됨
    if(hit) shown++;
  });
  const cnt=document.getElementById('cnt-'+id);
  if(cnt){
    if(!q){ cnt.textContent=total?`${total}행`:''; cnt.className='tblfilter-cnt'; }
    else { cnt.textContent=`${shown} / ${total}행 일치`;
           cnt.className='tblfilter-cnt'+(shown?' hit':''); }
  }
}

function initTableSort(){
  SORTABLE_TBODIES.forEach(id=>{
    const tb=document.getElementById(id); if(!tb) return;
    const tbl=tb.closest('table'); if(!tbl) return;

    // ① 헤더 더블클릭 정렬
    tbl.querySelectorAll('thead th').forEach((th,i)=>{
      th.style.cursor='pointer';
      th.title='더블클릭: 오름차순 정렬 (한 번 더: 내림차순)';
      th.addEventListener('dblclick',()=>{
        const cur=_tblSort[id];
        _tblSort[id]=(cur&&cur.idx===i)?{idx:i,asc:!cur.asc}:{idx:i,asc:true};
        _markTblHeaders(tbl,id);
        _applyTblSort(id);
      });
    });

    // ② 표 위에 필터 입력창 자동 삽입(HTML 수정 없이 모든 대상 표에 동일 적용)
    if(!document.getElementById('flt-'+id)){
      const wrap=document.createElement('div');
      wrap.className='tblfilter-wrap';
      const inp=document.createElement('input');
      inp.type='search'; inp.className='tblfilter'; inp.id='flt-'+id;
      inp.placeholder='필터 — 종목·전략·사유 등 아무 글자';
      inp.autocomplete='off';
      const cnt=document.createElement('span');
      cnt.className='tblfilter-cnt'; cnt.id='cnt-'+id;
      wrap.appendChild(inp); wrap.appendChild(cnt);
      tbl.parentNode.insertBefore(wrap, tbl);
      inp.addEventListener('input',()=>{
        _tblFilter[id]=inp.value.trim().toLowerCase();
        _applyTblFilter(id);
      });
    }

    // ③ 자동 재렌더(5분) 후 정렬·필터 재적용
    new MutationObserver(()=>{
      if(_tblSorting) return;
      if(_tblSort[id]) _applyTblSort(id);
      _applyTblFilter(id);     // 필터는 표시 상태라 항상 재적용(행 수 표시 갱신 포함)
    }).observe(tb,{childList:true});
  });
}

// ══ 2026-10-21 모의매매 전략 설정 — 모의계좌 탭·강도매매 탭(같은 편집기) ══
let _lc=null, _lcDraft=null;
const _LC_BOX=['lc-mock'];   // [2026-10-22] 강도매매 탭은 AI·강도 가상매매 설정(vc-strength)으로 바뀜
const _lcEsc=v=>String(v==null?'':v).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function lcLoad(){
  try{ _lc=await fetch('/api/live_config').then(r=>r.json()); }
  catch(e){ _LC_BOX.forEach(id=>setHtml(id,`<span style="color:#ef4444">설정 로드 실패: ${_lcEsc(e.message)}</span>`)); return; }
  _lcDraft=JSON.parse(JSON.stringify(_lc.config.accounts));
  lcRender();
}
function _lcEng(name){ return ((_lc&&_lc.engines)||[]).find(e=>e.engine===name)||{}; }
// [2026-10-23] 전략 고르기 목록은 알파벳순(대소문자 무시) — 엔진 정의 순서는 계열이 섞여 찾기 어려움
function _lcSorted(){ return ((_lc&&_lc.engines)||[]).slice().sort((x,y)=>x.engine.localeCompare(y.engine,'en',{sensitivity:'base',numeric:true})); }
function lcRender(){
  if(!_lc) return;
  const L=_lc, D=_lcDraft;
  const inp='background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:3px 5px';
  const opts=sel=>_lcSorted().map(e=>`<option value="${_lcEsc(e.engine)}"${e.engine===sel?' selected':''}>${_lcEsc(e.engine)} — ${_lcEsc(e.title)}</option>`).join('');
  const card=a=>{
    const A=D[a], tot=A.strategies.reduce((t,s)=>t+(Number(s.slots)||0),0);
    const rows=A.strategies.map((s,i)=>{ const e=_lcEng(s.engine);
      return `<tr>
        <td style="white-space:nowrap">${i+1}
          <button class="runbtn" style="padding:0 6px" title="위로(먼저 슬롯을 채움)" onclick="lcMove('${a}',${i},-1)">▲</button>
          <button class="runbtn" style="padding:0 6px" title="아래로" onclick="lcMove('${a}',${i},1)">▼</button></td>
        <td><select style="${inp};max-width:330px" onchange="lcSet('${a}',${i},'engine',this.value)">${opts(s.engine)}</select>
          ${e.note?`<div style="color:#fbbf24;font-size:11px;max-width:340px">⚠ ${_lcEsc(e.note)}</div>`:''}
          ${A.strength_version==='v2'?((L.v2_models||{})[s.engine]?`<div style="color:#64748b;font-size:11px">v2 모델: ${_lcEsc(L.v2_models[s.engine].trained)} · 학습 ${L.v2_models[s.engine].n}건 · 지표 ${L.v2_models[s.engine].n_ic}개</div>`
            :'<div style="color:#fbbf24;font-size:11px">v2 모델 없음 — 저장하면 학습(1~3분), 그 전엔 v1 점수로 대체</div>'):''}</td>
        <td>${A.slot_mode==='pool'?'<span style="color:#64748b;font-size:11px" title="칸 없음 모드 — 전략별 칸 수는 쓰지 않음">공유</span>':`<input type="number" min="1" max="10" step="1" value="${s.slots}" style="${inp};width:52px" onchange="lcSet('${a}',${i},'slots',this.value)">`}</td>
        <td style="text-align:center"><input type="checkbox" ${s.exempt?'checked':''} onchange="lcSet('${a}',${i},'exempt',this.checked)" title="체크하면 강도 기준과 상관없이 삼(후보가 많으면 강도 높은 순)"></td>
        <td>${s.exempt?'<span style="color:#64748b;font-size:11px">면제</span>':`<input type="number" min="0" max="10" step="0.1" placeholder="기본 ${A.min_strength}" value="${s.min_strength==null?'':s.min_strength}" style="${inp};width:70px${s.min_strength!=null?';border-color:#38bdf8':''}" title="이 전략만의 강도 기준 — 비우면 계좌 기본값(${A.min_strength})" onchange="lcSet('${a}',${i},'min_strength',this.value)">`}</td>
        <td><input type="number" step="1" min="-50" max="-1" placeholder="없음" value="${s.stop==null?'':s.stop}" style="${inp};width:64px" onchange="lcSet('${a}',${i},'stop',this.value)"></td>
        <td><input type="number" step="1" min="1" max="300" placeholder="없음" value="${s.take==null?'':s.take}" style="${inp};width:64px" onchange="lcSet('${a}',${i},'take',this.value)"></td>
        <td style="color:#94a3b8">${e.holding??s.holding}일</td>
        <td><button class="runbtn" style="padding:0 7px" title="빼기" onclick="lcDel('${a}',${i})">✕</button></td></tr>`; }).join('');
    const stopSem=a==='kiwoom'?'15:21 평가손익 판정 → 마감 동시호가 매도':'전일 종가 판정 → 다음날 09:01 시가 매도';
    const takeSem=a==='kiwoom'?'매일 아침 장중 지정가(닿으면 그 가격)':'전일 종가 판정 → 다음날 09:01 시가 매도';
    return `<div style="flex:1;min-width:560px;background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:10px 12px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
        <b style="font-size:14px">${_lcEsc(L.config.accounts[a].label)}</b>
        <label style="font-size:13px;color:#cbd5e1" title="전략별 칸을 비운 전략에 쓰는 기본값">기본 강도 기준 ≥
          <input type="number" min="0" max="10" step="0.1" value="${A.min_strength}" style="${inp};width:62px" onchange="lcSetAcc('${a}','min_strength',this.value)"></label>
        <select style="${inp};font-size:12px" title="v1: 라이브 6전략 공통 가중치(종전) / v2: 전략마다 자기 과거 매매로 학습(방향이 해마다 유지된 지표만·시장 지표 제외, §62). v2 권장 기준 5.5" onchange="lcSetAcc('${a}','strength_version',this.value)">
          <option value="v1"${(A.strength_version||'v1')==='v1'?' selected':''}>강도 v1(공통)</option>
          <option value="v2"${A.strength_version==='v2'?' selected':''}>강도 v2(전략별)</option></select>
        <select style="${inp};font-size:12px" title="전략별 칸: 전략마다 칸 수를 나눠 줌(위 순서대로 채움) / 칸 없음: 고른 전략 중 조건을 넘은 신호를 전체 10종목까지, 같은 날 후보는 전략 안 순위(그 전략 과거 강도 중 상위 몇 %) 높은 순 — 백테스트(§74)에선 신호가 많은 전략이 칸을 먼저 차지해 대체로 불리했음" onchange="lcSetAcc('${a}','slot_mode',this.value)">
          <option value="slots"${(A.slot_mode||'slots')==='slots'?' selected':''}>전략별 칸</option>
          <option value="pool"${A.slot_mode==='pool'?' selected':''}>칸 없음(전체 10종목)</option></select>
        ${A.slot_mode==='pool'?`<span style="font-size:12px;color:#38bdf8">칸 없음 · 전체 ${L.slots_total}종목</span>`:`<span style="font-size:12px;color:${tot===L.slots_total?'#10b981':'#ef4444'}">슬롯 합 ${tot}/${L.slots_total}</span>`}</div>
      <table style="font-size:12px;margin-top:6px;width:100%"><thead><tr><th>순서</th><th>전략</th><th>슬롯</th>
        <th title="강도 기준과 상관없이 삼">강도 면제</th><th title="전략마다 다른 강도 기준 — 비우면 위의 계좌 기본값">강도 기준</th><th title="${_lcEsc(stopSem)}">손절 %</th><th title="${_lcEsc(takeSem)}">익절 %</th><th>보유</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>
      <div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap">
        ${A.strategies.length<L.max_strategies?`<button class="runbtn" onclick="lcAdd('${a}')">+ 전략 추가</button>`:`<span style="font-size:11px;color:#64748b">전략은 최대 ${L.max_strategies}개</span>`}
        <span style="font-size:11px;color:#64748b">손절: ${_lcEsc(stopSem)} · 익절: ${_lcEsc(takeSem)} · 보유일은 전략 정의대로</span></div></div>`;
  };
  const hist=(L.history||[]).slice(0,5).map(h=>`<div>· <b>${_lcEsc(h.time)}</b> ${_lcEsc((h.changes||[]).join(' / ')||'변경 없음')}</div>`).join('');
  const html=`<div style="font-size:12px;color:#94a3b8;margin-bottom:8px">
      지금 설정: <b style="color:#e2e8f0">${_lcEsc(L.config.updated||'기본값(현행)')}</b>${L.config.fallback?` <span style="color:#ef4444">⚠ ${_lcEsc(L.config.fallback)}</span>`:''}
      · 적용: ${L.regen_after_save?'저장하면 오늘 신호를 새 설정으로 다시 만듦 → 다음 거래일 09:00 매수부터':'오늘 18:30 신호부터 → 다음 거래일 09:00 매수부터'}
      ${L.regen_running?' · <span style="color:#38bdf8">신호 다시 만드는 중…</span>':''}</div>
    ${L.locked?`<div style="padding:6px 10px;border-radius:6px;background:rgba(239,68,68,.08);border:1px solid #7f1d1d;color:#fca5a5;font-size:12px;margin-bottom:8px">🔒 ${_lcEsc(L.lock_msg)}</div>`:''}
    <div style="display:flex;gap:10px;flex-wrap:wrap">${card('kiwoom')}${card('kis')}</div>
    <div id="lc-ov" style="margin-top:8px">${_ovHtml.lc}</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
      <button class="runbtn" style="background:#2563eb;color:#fff;${L.locked?'opacity:.5':''}" ${L.locked?'disabled':''} onclick="lcSave()">💾 변경 확인 후 저장</button>
      <button class="runbtn" onclick="lcLoad()">↺ 현재 설정 다시 불러오기</button>
      <span id="lc-msg" style="font-size:12px;color:#94a3b8"></span></div>
    <details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px;color:#94a3b8">최근 변경 기록</summary>
      <div style="font-size:12px;color:#cbd5e1;margin-top:4px;line-height:1.7">${hist||'<span style="color:#64748b">아직 없음</span>'}</div></details>
    <div style="font-size:11px;color:#64748b;margin-top:6px">빠진 전략으로 이미 산 종목은 원래 보유일·손절·익절대로 만기까지 갑니다 · 거래일 08:30~15:40 은 잠금 · 바꿀 때 두 번 확인합니다 · 백테스트 탭 '기간 백테스트'로 먼저 확인해 보세요</div>`;
  _LC_BOX.forEach(id=>setHtml(id,html.replace('id="lc-msg"',`id="lc-msg-${id}"`)));
  ovRefresh();
}
function _lcMsg(h){ _LC_BOX.forEach(id=>setHtml('lc-msg-'+id,h)); }
function lcSet(a,i,k,v){
  const s=_lcDraft[a].strategies[i];
  if(k==='slots') s.slots=parseInt(v||'0',10)||0;
  else if(k==='exempt'){ s.exempt=!!v; if(s.exempt) s.min_strength=null; }   // 면제면 전략별 기준은 의미 없음
  else if(k==='stop'||k==='take'||k==='min_strength') s[k]=(v===''||v==null)?null:Number(v);
  else if(k==='engine'){ s.engine=v; const e=_lcEng(v); s.holding=e.holding; }
  lcRender();
}
function lcSetAcc(a,k,v){ _lcDraft[a][k]=(k==='min_strength')?Number(v):v; lcRender(); }
function lcMove(a,i,d){ const S=_lcDraft[a].strategies, j=i+d; if(j<0||j>=S.length) return; [S[i],S[j]]=[S[j],S[i]]; lcRender(); }
function lcDel(a,i){ const S=_lcDraft[a].strategies; if(S.length<=1){ alert('전략은 최소 1개'); return; } S.splice(i,1); lcRender(); }
function lcAdd(a){
  const S=_lcDraft[a].strategies, used=new Set(S.map(s=>s.engine));
  const e=_lcSorted().find(x=>!used.has(x.engine)); if(!e) return;
  S.push({engine:e.engine,slots:1,exempt:false,stop:null,take:null,min_strength:null,holding:e.holding}); lcRender();
}
async function lcSave(){
  const post=async(url,extra)=>{ const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Action':'1'},
      body:JSON.stringify(Object.assign({accounts:_lcDraft},extra||{}))}); let d={}; try{ d=await r.json(); }catch(e){ d={ok:false,msg:'응답 해석 실패 ('+r.status+')'}; } return d; };
  const pv=await post('/api/live_config/preview');
  if(!pv.ok){ alert('저장할 수 없습니다:\\n- '+(pv.errors||[pv.msg]).join('\\n- ')); return; }
  if(pv.locked){ alert(pv.lock_msg); return; }
  if(!(pv.changes||[]).length){ alert('바뀐 내용이 없습니다'); return; }
  // ① 첫 번째 확인 — 무엇이 바뀌는지
  const _ovTxt=(pv.overlap||[]).length?'\\n\\n⚠ 겹침 경고:\\n- '+pv.overlap.join('\\n- '):'';
  if(!confirm('[1/2] 모의매매 전략을 이렇게 바꿉니다:\\n\\n- '+pv.changes.join('\\n- ')+_ovTxt+'\\n\\n적용 시점: '+pv.effective+'\\n\\n계속할까요?')) return;
  // ② 두 번째 확인 — 정말로
  if(!confirm('[2/2] 정말로 적용할까요?\\n\\n다음 거래일 09:00 매수부터 이 방식으로 매매합니다.\\n되돌리려면 다시 바꿔야 합니다.')) return;
  const d=await post('/api/live_config',{confirm:2});
  _lcMsg(d.ok?`<span style="color:#10b981">✓ ${_lcEsc(d.msg)}</span>`:`<span style="color:#ef4444">${_lcEsc(d.msg||'저장 실패')}</span>`);
  if(d.ok){ await lcLoad(); _lcMsg(`<span style="color:#10b981">✓ ${_lcEsc(d.msg)}</span>`); }
}

// ══ 2026-10-22 AI·강도 가상매매 전략 설정(§68) — 강도매매 탭. 모의계좌 설정과 같은 모양, 다른 파일(실계좌 무관) ══
let _vc=null, _vcDraft=null;
async function vcLoad(){
  try{ _vc=await fetch('/api/virtual_config').then(r=>r.json()); }
  catch(e){ setHtml('vc-strength',`<span style="color:#ef4444">설정 로드 실패: ${_lcEsc(e.message)}</span>`); return; }
  _vcDraft=JSON.parse(JSON.stringify(_vc.draft)); vcRender();
}
function _vcEng(n){ return ((_vc&&_vc.engines)||[]).find(e=>e.engine===n)||{}; }
function _vcSorted(){ return ((_vc&&_vc.engines)||[]).slice().sort((x,y)=>x.engine.localeCompare(y.engine,'en',{sensitivity:'base',numeric:true})); }
function _vcLive(eng){ const ln=(_vc.aliases||{})[eng]||eng; return (_vc.live_names||[]).includes(ln); }
function _vcD(s){ return s&&s.length===8?s.slice(0,4)+'-'+s.slice(4,6)+'-'+s.slice(6):(s||''); }
function vcRender(){
  if(!_vc) return;
  const V=_vc, D=_vcDraft;
  const inp='background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:3px 5px';
  const opts=sel=>_vcSorted().map(e=>`<option value="${_lcEsc(e.engine)}"${e.engine===sel?' selected':''}>${_lcEsc(e.engine)} — ${_lcEsc(e.title)}</option>`).join('');
  const card=p=>{
    const P=D[p], tot=P.strategies.reduce((t,s)=>t+(Number(s.slots)||0),0), isAI=p==='ai';
    const rows=P.strategies.map((s,i)=>{ const e=_vcEng(s.engine), live=_vcLive(s.engine);
      return `<tr>
        <td style="white-space:nowrap">${i+1}
          <button class="runbtn" style="padding:0 6px" onclick="vcMove('${p}',${i},-1)">▲</button>
          <button class="runbtn" style="padding:0 6px" onclick="vcMove('${p}',${i},1)">▼</button></td>
        <td><select style="${inp};max-width:330px" onchange="vcSet('${p}',${i},'engine',this.value)">${opts(s.engine)}</select>
          <div style="font-size:11px;color:${live?'#64748b':'#38bdf8'}">${live?'모의계좌 신호를 그대로 씀':'가상 전용 신호를 매일 18:31 새로 만듦(주문 없음)'}</div>
          ${e.note?`<div style="color:#fbbf24;font-size:11px;max-width:340px">⚠ ${_lcEsc(e.note)}</div>`:''}
          ${(!isAI&&P.strength_version==='v2')?((V.v2_models||{})[s.engine]?`<div style="color:#64748b;font-size:11px">v2 모델: ${_lcEsc(V.v2_models[s.engine].trained)} · 학습 ${V.v2_models[s.engine].n}건</div>`
            :'<div style="color:#fbbf24;font-size:11px">v2 모델 없음 — 저장하면 학습(1~3분), 그 전엔 v1 점수로 대체</div>'):''}</td>
        <td>${P.slot_mode==='pool'?'<span style="color:#64748b;font-size:11px" title="칸 없음 모드 — 전략별 칸 수는 쓰지 않음">공유</span>':`<input type="number" min="1" max="10" step="1" value="${s.slots}" style="${inp};width:52px" onchange="vcSet('${p}',${i},'slots',this.value)">`}</td>
        <td style="text-align:center"><input type="checkbox" ${s.exempt?'checked':''} onchange="vcSet('${p}',${i},'exempt',this.checked)" title="체크하면 기준과 상관없이 후보(후보가 많으면 순위 높은 순)"></td>
        <td>${s.exempt?'<span style="color:#64748b;font-size:11px">면제</span>':`<input type="number" min="0" max="${isAI?1:10}" step="${isAI?0.01:0.1}" placeholder="${P.min_score==null?'기본 없음':'기본 '+P.min_score}" value="${s.min_score==null?'':s.min_score}" style="${inp};width:70px${s.min_score!=null?';border-color:#38bdf8':''}" title="이 전략만의 기준 — 비우면 위의 기본 기준" onchange="vcSet('${p}',${i},'min_score',this.value)">`}</td>
        <td><input type="number" step="1" min="-50" max="-1" placeholder="없음" value="${s.stop==null?'':s.stop}" style="${inp};width:64px" onchange="vcSet('${p}',${i},'stop',this.value)"></td>
        <td><input type="number" step="1" min="1" max="300" placeholder="없음" value="${s.take==null?'':s.take}" style="${inp};width:64px" onchange="vcSet('${p}',${i},'take',this.value)"></td>
        <td style="color:#94a3b8">${e.holding??s.holding}일</td>
        <td><button class="runbtn" style="padding:0 7px" onclick="vcDel('${p}',${i})">✕</button></td></tr>`; }).join('');
    const thr=isAI
      ? `<label style="font-size:13px;color:#cbd5e1" title="AI 확률(meta 모델 big-win 확률, 0~1) 이 값 이상만 삼. 비우면 기준 없이 확률 높은 순">기본 AI 확률 기준 ≥
           <input type="number" min="0" max="1" step="0.01" placeholder="없음" value="${P.min_score==null?'':P.min_score}" style="${inp};width:68px" onchange="vcSetP('${p}','min_score',this.value)"></label>`
      : `<label style="font-size:13px;color:#cbd5e1" title="강도 점수(0~10) 이 값 이상만 삼. 비우면 기준 없이 강도 높은 순">기본 강도 기준 ≥
           <input type="number" min="0" max="10" step="0.1" placeholder="없음" value="${P.min_score==null?'':P.min_score}" style="${inp};width:62px" onchange="vcSetP('${p}','min_score',this.value)"></label>
         <select style="${inp};font-size:12px" onchange="vcSetP('${p}','strength_version',this.value)">
           <option value="v1"${(P.strength_version||'v1')==='v1'?' selected':''}>강도 v1(공통)</option>
           <option value="v2"${P.strength_version==='v2'?' selected':''}>강도 v2(전략별)</option></select>`;
    return `<div style="flex:1;min-width:560px;background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:10px 12px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
        <b style="font-size:14px">${isAI?'🤖':'💪'} ${_lcEsc(V.labels[p])} <span style="font-size:11px;color:#64748b;font-weight:400">${isAI?'AI 확률 높은 순':'강도 높은 순'}</span></b>
        ${thr}
        <select style="${inp};font-size:12px" title="전략별 칸 / 칸 없음(조건 넘은 신호를 전체 10종목까지, 같은 날 후보는 ${isAI?'AI 확률':'전략 안 순위(강도 백분위)'} 높은 순)" onchange="vcSetP('${p}','slot_mode',this.value)">
          <option value="slots"${(P.slot_mode||'slots')==='slots'?' selected':''}>전략별 칸</option>
          <option value="pool"${P.slot_mode==='pool'?' selected':''}>칸 없음(전체 10종목)</option></select>
        ${P.slot_mode==='pool'?`<span style="font-size:12px;color:#38bdf8">칸 없음 · 전체 ${V.slots_total}종목</span>`:`<span style="font-size:12px;color:${tot===V.slots_total?'#10b981':'#ef4444'}">슬롯 합 ${tot}/${V.slots_total}</span>`}</div>
      <table style="font-size:12px;margin-top:6px;width:100%"><thead><tr><th>순서</th><th>전략</th><th>슬롯</th>
        <th title="기준과 상관없이 후보">${isAI?'AI 기준 면제':'강도 면제'}</th><th title="전략마다 다른 기준 — 비우면 위의 기본 기준">${isAI?'AI 기준':'강도 기준'}</th><th title="매일 종가로 판정 → 그 종가에 청산">손절 %</th><th title="매일 종가로 판정 → 그 종가에 청산">익절 %</th><th>보유</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>
      <div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap">
        ${P.strategies.length<V.max_strategies?`<button class="runbtn" onclick="vcAdd('${p}')">+ 전략 추가</button>`:`<span style="font-size:11px;color:#64748b">전략은 최대 ${V.max_strategies}개</span>`}
        <span style="font-size:11px;color:#64748b">순서는 표시용 — 같은 날 후보는 ${isAI?'AI 확률':'강도'} 높은 순으로 담고, 전략마다 슬롯 수까지</span></div></div>`;
  };
  const now=V.active, pend=V.pending;
  const nowTxt=!now?`종전 규칙 — ${_lcEsc(V.legacy_now)}`:now.legacy?`종전 규칙 — ${_lcEsc(now.desc)}`:`${_vcD(now.effective)} 신호부터 적용 중인 설정(저장 ${_lcEsc(now.saved||'')})`;
  const hist=(V.history||[]).map(h=>`<div>· <b>${_vcD(h.effective)==='0000-00-00'?'처음':_vcD(h.effective)+' 신호부터'}</b> ${_lcEsc(h.desc)}</div>`).join('');
  setHtml('vc-strength', `<div style="font-size:12px;color:#94a3b8;margin-bottom:8px">
      지금: <b style="color:#e2e8f0">${nowTxt}</b>${pend?` · <span style="color:#38bdf8">예약: ${_vcD(pend.effective)} 신호부터 새 설정</span>`:''}
      ${V.fallback?` <span style="color:#ef4444">⚠ ${_lcEsc(V.fallback)}</span>`:''}
      · 저장하면 <b>${_vcD(V.effective)} 신호부터</b> 적용(그 전 기록·이미 산 종목은 그대로)</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">${card('ai')}${card('strength')}</div>
    <div id="vc-ov" style="margin-top:8px">${_ovHtml.vc}</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
      <button class="runbtn" style="background:#2563eb;color:#fff" onclick="vcSave()">💾 변경 확인 후 저장</button>
      <button class="runbtn" onclick="vcLoad()">↺ 현재 설정 다시 불러오기</button>
      <span id="vc-msg" style="font-size:12px;color:#94a3b8"></span></div>
    <details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px;color:#94a3b8">설정 이력(적용 신호일별)</summary>
      <div style="font-size:12px;color:#cbd5e1;margin-top:4px;line-height:1.7">${hist||'<span style="color:#64748b">아직 없음 — 지금은 종전 규칙</span>'}</div></details>
    <div style="font-size:11px;color:#64748b;margin-top:6px">가상매매는 주문이 없는 장부 시뮬입니다(신호일 종가 진입·왕복 0.33%) · 실제 모의계좌 설정은 🏦 모의계좌 탭 · 모의계좌에 없는 전략의 AI 확률은 그 전략으로 학습되지 않은 모델 값이라 참고용</div>`);
  ovRefresh();
}
// ══ 2026-10-23 전략 겹침 경고(§72) — 모의계좌·가상매매 편집 중인 값으로 바로 점검(서버 표: db/strategy_overlap.json) ══
let _ovT=null; const _ovHtml={lc:'',vc:''};
function ovRefresh(){ clearTimeout(_ovT); _ovT=setTimeout(_ovRun,250); }
async function _ovRun(){
  const places={};
  if(typeof _lcDraft!=='undefined'&&_lcDraft){ ['kiwoom','kis'].forEach(a=>{ if(_lcDraft[a]) places[a]=_lcDraft[a].strategies.map(s=>s.engine); }); }
  if(typeof _vcDraft!=='undefined'&&_vcDraft){ ['ai','strength'].forEach(p=>{ if(_vcDraft[p]) places[p]=_vcDraft[p].strategies.map(s=>s.engine); }); }
  let d; try{ d=await fetch('/api/strategy_overlap/check?places='+encodeURIComponent(JSON.stringify(places))).then(r=>r.json()); }catch(e){ return; }
  const box=keys=>{
    if(!d.available) return '<div style="font-size:12px;color:#64748b">겹침 표가 아직 없습니다 — 주간 AI 파이프라인이 만들거나 python strategy_overlap.py 로 만듭니다</div>';
    const ws=(d.warnings||[]).filter(w=>(w.places||[]).some(p=>keys.includes(p)));
    const basis=`기준: 최근 3년(${_vcD((d.window||[])[0])}~${_vcD((d.window||[])[1])}) 백테스트 신호 · 같은 날 같은 종목 50%↑ = 겹침 · 표 갱신 ${_lcEsc(d.generated||'')}`;
    if(!ws.length) return `<div style="padding:6px 10px;border-radius:6px;background:rgba(16,185,129,.07);border:1px solid #065f46;color:#6ee7b7;font-size:12px">✓ 겹치는 전략 없음 <span style="color:#64748b">· ${basis}</span></div>`;
    const nW=ws.filter(w=>w.level==='warn').length;
    return `<div style="padding:7px 10px;border-radius:6px;background:${nW?'rgba(251,191,36,.07)':'rgba(96,165,250,.06)'};border:1px solid ${nW?'#78350f':'#1e3a5f'};font-size:12px;line-height:1.7">
      <b style="color:${nW?'#fbbf24':'#93c5fd'}">${nW?'⚠':'ℹ'} 전략 겹침 점검 — 경고 ${nW}건 · 참고 ${ws.length-nW}건</b>
      ${ws.map(w=>`<div style="color:${w.level==='warn'?'#fcd34d':'#94a3b8'}">${w.level==='warn'?'⚠':'ℹ'} ${_lcEsc(w.msg)}</div>`).join('')}
      <div style="color:#64748b;font-size:11px">${basis} · 저장은 막지 않습니다(판단은 직접)</div></div>`;
  };
  _ovHtml.lc=box(['kiwoom','kis']); _ovHtml.vc=box(['ai','strength']);
  setHtml('lc-ov',_ovHtml.lc); setHtml('vc-ov',_ovHtml.vc);
}
function vcSet(p,i,k,v){
  const s=_vcDraft[p].strategies[i];
  if(k==='slots') s.slots=parseInt(v||'0',10)||0;
  else if(k==='exempt'){ s.exempt=!!v; if(s.exempt) s.min_score=null; }
  else if(k==='stop'||k==='take'||k==='min_score') s[k]=(v===''||v==null)?null:Number(v);
  else if(k==='engine'){ s.engine=v; const e=_vcEng(v); s.holding=e.holding; }
  vcRender();
}
function vcSetP(p,k,v){ _vcDraft[p][k]=(k==='min_score')?((v===''||v==null)?null:Number(v)):v; vcRender(); }
function vcMove(p,i,d){ const S=_vcDraft[p].strategies, j=i+d; if(j<0||j>=S.length) return; [S[i],S[j]]=[S[j],S[i]]; vcRender(); }
function vcDel(p,i){ const S=_vcDraft[p].strategies; if(S.length<=1){ alert('전략은 최소 1개'); return; } S.splice(i,1); vcRender(); }
function vcAdd(p){
  const S=_vcDraft[p].strategies, used=new Set(S.map(s=>s.engine));
  const e=_vcSorted().find(x=>!used.has(x.engine)); if(!e) return;
  S.push({engine:e.engine,slots:1,exempt:false,stop:null,take:null,min_score:null,holding:e.holding}); vcRender();
}
async function vcSave(){
  const post=async(url,extra)=>{ const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Action':'1'},
      body:JSON.stringify(Object.assign({portfolios:_vcDraft},extra||{}))}); let d={}; try{ d=await r.json(); }catch(e){ d={ok:false,msg:'응답 해석 실패 ('+r.status+')'}; } return d; };
  const msg=h=>setHtml('vc-msg',h);
  const pv=await post('/api/virtual_config/preview');
  if(!pv.ok){ alert('저장할 수 없습니다:\\n- '+(pv.errors||[pv.msg]).join('\\n- ')); return; }
  if(!(pv.changes||[]).length){ alert('바뀐 내용이 없습니다'); return; }
  const _ovTxt=(pv.overlap||[]).length?'\\n\\n⚠ 겹침:\\n- '+pv.overlap.join('\\n- '):'';
  if(!confirm('[1/2] AI·강도 가상매매 설정을 이렇게 바꿉니다:\\n\\n- '+pv.changes.join('\\n- ')+_ovTxt+'\\n\\n적용: '+pv.effective+'\\n\\n계속할까요?')) return;
  if(!confirm('[2/2] 정말로 적용할까요?\\n\\n실제 모의계좌(키움·KIS) 매매는 바뀌지 않습니다.')) return;
  const d=await post('/api/virtual_config',{confirm:2});
  if(d.ok){ await vcLoad(); }
  msg(d.ok?`<span style="color:#10b981">✓ ${_lcEsc(d.msg)}</span>`:`<span style="color:#ef4444">${_lcEsc(d.msg||'저장 실패')}</span>`);
}

// ══ 2026-10-19 ① 섹션 접기/펴기(모든 탭) ② 전략 이름 마우스오버 설명(모든 탭) ══
// 저장 키 = 탭 + (섹션 id 또는 탭 안 순번) — 제목 글자는 데이터 로딩 후 바뀌는 칸이 있어 키로 안 쓴다
function _secKey(sec){ const tab=sec.closest('.tab-content');
  return 'sec:'+(tab?tab.id:'top')+':'+(sec.id||('i'+(sec.dataset.secIdx||'')));}
function _secSet(sec,col){ sec.classList.toggle('sec-collapsed',col); try{ localStorage.setItem(_secKey(sec),col?'1':'0'); }catch(e){} }
function _secInit(){
  document.querySelectorAll('.tab-content').forEach(tab=>tab.querySelectorAll('.section').forEach((sec,i)=>{ sec.dataset.secIdx=i; }));
  document.querySelectorAll('.section').forEach(sec=>{
    if(sec.dataset.secInit) return; sec.dataset.secInit='1';
    const head=sec.firstElementChild; if(!head||!/^H[1-4]$/.test(head.tagName)) return;   // 제목(h2)이 맨 위인 섹션만
    head.classList.add('sec-head');
    const c=document.createElement('span'); c.className='sec-caret'; c.textContent='▾'; head.insertBefore(c,head.firstChild);
    head.title='눌러서 접기/펴기';
    head.addEventListener('click',e=>{ if(e.target.closest('button,input,select,a,textarea,label,option')) return;
      _secSet(sec,!sec.classList.contains('sec-collapsed')); });
    let saved=null; try{ saved=localStorage.getItem(_secKey(sec)); }catch(e){}
    if(saved==='1') sec.classList.add('sec-collapsed');
  });
  document.querySelectorAll('.tab-content').forEach(tab=>{
    if(tab.querySelector(':scope > .sec-bar')||!tab.querySelector('.section > .sec-head')) return;
    const bar=document.createElement('div'); bar.className='sec-bar';
    bar.innerHTML='<button type="button" data-a="1">▸ 모두 접기</button><button type="button" data-a="0">▾ 모두 펼치기</button>';
    bar.addEventListener('click',e=>{ const b=e.target.closest('button'); if(!b) return;
      tab.querySelectorAll('.section').forEach(sec=>{ if(sec.querySelector(':scope > .sec-head')) _secSet(sec,b.dataset.a==='1'); }); });
    tab.insertBefore(bar,tab.firstChild);
  });
}
let _STRAT={}, _STRAT_AL={};
const _STRAT_SKIP='input,select,textarea,option,#strat-tipbox,#pbt-tipbox,.pbt-tip';
function _stratFind(el){
  // 짧은 글자 요소에서 알려진 전략 이름(영문 토큰)을 찾는다 — 표 칸·라벨·배지 어디든
  for(let k=0, x=el; k<2 && x && x!==document.body; k++, x=x.parentElement){
    if(x.closest&&x.closest(_STRAT_SKIP)) return null;
    const t=(x.textContent||'').trim(); if(!t||t.length>90) continue;
    const t2=t.replace(/^★\s*/,'');
    if(_STRAT_AL[t2]) return [x,_STRAT_AL[t2]];
    if(typeof _STRAT_KR==='object'){ for(const [k,v] of Object.entries(_STRAT_KR)){ if(v===t2&&_STRAT[k]) return [x,k]; } }
    const toks=t.match(/[A-Za-z][A-Za-z0-9_]{3,}/g)||[];
    for(const tk of toks){ if(_STRAT[tk]) return [x,tk]; }
  }
  return null;
}
function _stratTipInit(){
  fetch('/api/strategy_info').then(r=>r.json()).then(d=>{ _STRAT=(d&&d.info)||{}; _STRAT_AL=(d&&d.aliases)||{}; }).catch(()=>{});
  const box=document.createElement('div'); box.id='strat-tipbox';
  box.style.cssText='position:fixed;z-index:9998;display:none;max-width:420px;padding:10px 12px;background:#0b1220;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:12px;line-height:1.6;box-shadow:0 8px 22px rgba(0,0,0,.5);pointer-events:none';
  document.body.appendChild(box);
  let cur=null;
  const esc=v=>String(v==null?'':v).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const li=a=>(a||[]).map(x=>'<li>'+esc(x)+'</li>').join('');
  document.addEventListener('mouseover',e=>{
    if(!Object.keys(_STRAT).length) return;
    const hit=_stratFind(e.target); if(!hit){ return; }
    const [el,name]=hit; if(cur===el&&box.style.display==='block') return;
    cur=el; el.classList.add('strat-hot');
    const d=_STRAT[name]||{}, L=d.live;
    box.innerHTML=`<div style="font-weight:700;font-size:13px">${esc(d.title||name)}</div>
      <div style="color:#64748b">${esc(name)}${d.engine&&d.engine!==name?' · 백테스트 이름 '+esc(d.engine):''}</div>
      ${d.retired?`<div style="color:#fbbf24;margin-top:4px">⏹ ${esc(d.retired)}</div>`:''}
      ${d.idea?`<div style="margin-top:6px;color:#cbd5e1">💡 ${esc(d.idea)}</div>`:''}
      ${L?`<div style="margin-top:6px;padding:5px 8px;background:rgba(251,191,36,.08);border-left:3px solid #fbbf24;border-radius:4px">
        <b>★ 지금 모의매매(${esc(L.account)}) · ${L.slots}슬롯</b><br><span style="color:#cbd5e1">사는 조건: ${esc(L.entry)}</span><br>
        <span style="color:#cbd5e1">파는 조건: ${esc(L.exit)}</span></div>`:
        `${(d.buy||[]).length?`<div style="margin-top:6px"><b style="color:#10b981">살 때</b><ul style="margin:2px 0 0 16px">${li(d.buy)}</ul></div>`:''}
         ${(d.sell||[]).length?`<div><b style="color:#f87171">팔 때</b><ul style="margin:2px 0 0 16px">${li(d.sell)}</ul></div>`:''}`}
      ${(d.caution||[]).length?`<div style="margin-top:4px;color:#fcd34d">⚠ ${esc(d.caution[0])}</div>`:''}`;
    box.style.display='block';
    const r=el.getBoundingClientRect();
    let x=Math.min(r.left, window.innerWidth-440), y=r.bottom+6;
    if(y+box.offsetHeight>window.innerHeight-6) y=Math.max(6, r.top-box.offsetHeight-6);
    box.style.left=Math.max(6,x)+'px'; box.style.top=y+'px';
  });
  document.addEventListener('mouseout',e=>{
    if(!cur) return;
    if(e.relatedTarget && cur.contains(e.relatedTarget)) return;
    if(!cur.contains(e.target)&&e.target!==cur) return;
    cur.classList.remove('strat-hot'); cur=null; box.style.display='none';
  });
}
window.addEventListener('load',()=>{ try{ _secInit(); }catch(e){} try{ _stratTipInit(); }catch(e){} try{ lcLoad(); }catch(e){} try{ vcLoad(); }catch(e){} });
window.addEventListener('load',()=>{
  load();
  initTableSort();
  refreshLogFiles();
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