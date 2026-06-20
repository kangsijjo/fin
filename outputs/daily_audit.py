"""
daily_audit.py — 매일 자동화 자가점검. 2026-06-20 신설.

"스케줄러 살아있나 / 오늘 수집 됐나 / 데이터 신선한가 / 신호 떴나 / 에러 있나"를
한 장으로 요약해 logs/daily_audit_YYYYMMDD.log 에 남긴다.
→ 사용자는 이 로그만 보여주면 됨(체크리스트 안 봐도 상태 판단 가능).

자동: run_live_signal.bat 끝(평일 18:30, live_signal·strategy_lab 직후)에서 호출.
수동: python daily_audit.py
"""
import os
import re
import sys
import glob
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(os.path.dirname(BASE), "logs")          # C:\fin\logs
if not os.path.isdir(LOGS):
    LOGS = os.path.join(BASE, "logs")
NOW = datetime.now()
TODAY = NOW.strftime("%Y%m%d")

_DB_CANDIDATES = [
    os.environ.get("STOCK_DB_PATH", ""),
    os.path.join(BASE, "..", "Stock_AI_Project", "data", "stock.db"),
    r"C:/fin/Stock_AI_Project/data/stock.db",
]

# (테이블, 날짜컬럼, 최대허용일) — get_health 와 동일 기준
_FRESH = [
    ("korea_stocks", "date", 5),
    ("supply_demand", "date", 5),
    ("usa_stocks", "date", 6),
    ("macro_indicators", "date", 6),
    ("credit_balance", "date", 10),   # 주1회
    ("news", "pubDate", 6),
]

ok_n = 0
warn_n = 0
lines = []


def chk(ok, label, detail=""):
    global ok_n, warn_n
    if ok:
        ok_n += 1
    else:
        warn_n += 1
    lines.append(f"  [{'OK ' if ok else '!! '}] {label}{(' — ' + detail) if detail else ''}")


def _telegram_cfg():
    """봇 토큰/챗ID 로드 — 환경변수 우선, 없으면 outputs/.telegram.json. 둘 다 없으면 (None,None)."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    cid = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (tok and cid):
        cfg = os.path.join(BASE, ".telegram.json")
        if os.path.exists(cfg):
            try:
                d = json.load(open(cfg, encoding="utf-8"))
                tok = tok or d.get("token", "")
                cid = cid or d.get("chat_id", "")
            except Exception:
                pass
    return (tok or None), (cid or None)


def notify(text):
    """텔레그램으로 경고 푸시 (설정 없으면 조용히 스킵). (성공여부, 메시지) 반환."""
    tok, cid = _telegram_cfg()
    if not tok or not cid:
        return False, "텔레그램 미설정(.telegram.json 또는 환경변수 TELEGRAM_BOT_TOKEN/CHAT_ID)"
    try:
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": cid, "text": text[:4000]}).encode()
        urllib.request.urlopen(url, data=data, timeout=10)
        return True, "전송됨"
    except Exception as e:
        return False, str(e)[:80]


def _db_path():
    for p in _DB_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def _mtime_min(path):
    return (NOW - datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 60


def _age_days(yyyymmdd_or_dash):
    s = str(yyyymmdd_or_dash)[:10].replace("-", "")
    try:
        return (NOW - datetime.strptime(s[:8], "%Y%m%d")).days
    except Exception:
        return 9999


# 텔레그램 설정 테스트: python daily_audit.py --test-tg  (검사 스킵하고 테스트 메시지만 발송)
if len(sys.argv) > 1 and sys.argv[1] in ("--test-tg", "test"):
    print("텔레그램 전송 테스트:", notify("천억이 daily_audit — 텔레그램 연결 테스트 OK"))
    raise SystemExit(0)


# 1) Stock_AI 스케줄러 가동 (scheduler.log heartbeat — 5분마다 기록)
p = os.path.join(LOGS, "scheduler.log")
if os.path.exists(p):
    age = _mtime_min(p)
    tail = ""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            tail = f.read()[-2000:]
    except Exception:
        pass
    dead_hint = "venv not found" in tail
    chk(age < 30 and not dead_hint,
        "Stock_AI 스케줄러 가동",
        f"마지막 기록 {int(age)}분 전" + (" / venv 오류 감지" if dead_hint else ""))
else:
    chk(False, "Stock_AI 스케줄러 가동", "scheduler.log 없음")

# 2) 데이터 신선도 (stock.db)
dbp = _db_path()
if dbp:
    try:
        con = sqlite3.connect(dbp, timeout=10)
        cur = con.cursor()
        for tbl, col, maxd in _FRESH:
            try:
                r = cur.execute(f"SELECT MAX({col}) FROM {tbl}").fetchone()
                latest = r[0] if r else None
                if latest is None:
                    chk(False, f"{tbl}", "데이터 없음")
                    continue
                ad = _age_days(latest)
                chk(ad <= maxd, f"{tbl}", f"최신 {str(latest)[:10]} ({ad}일)")
            except Exception as e:
                chk(False, f"{tbl}", f"조회실패 {str(e)[:40]}")
        con.close()
    except Exception as e:
        chk(False, "stock.db 접근", str(e)[:50])
else:
    chk(False, "stock.db", "경로 없음")

# 3) 오늘 신호 (live_signal) + paper_signals 갱신
sig_logs = glob.glob(os.path.join(LOGS, f"live_signal_{TODAY}_*.log"))
if sig_logs:
    latest_log = max(sig_logs, key=os.path.getmtime)
    try:
        txt = open(latest_log, encoding="utf-8", errors="replace").read()
    except Exception:
        txt = ""
    exit0 = "ExitCode=0" in txt
    m = re.search(r"합계\s+총\s+(\d+)건", txt)
    cnt = m.group(1) if m else "?"
    chk(exit0, "오늘 live_signal 실행", f"신호 {cnt}건, " + ("정상종료" if exit0 else "비정상/미완료"))
else:
    chk(False, "오늘 live_signal 실행", "오늘 로그 없음 (18:30 전이거나 미실행)")

pcsv = os.path.join(BASE, "paper_signals.csv")
if os.path.exists(pcsv):
    chk(_mtime_min(pcsv) < 1440, "paper_signals.csv 갱신",
        f"최종수정 {datetime.fromtimestamp(os.path.getmtime(pcsv)).strftime('%m-%d %H:%M')}")
else:
    chk(False, "paper_signals.csv", "파일 없음")

# 4) 전략 랩 오늘 산출물
lab = glob.glob(os.path.join(BASE, "results", f"strategy_lab_{TODAY}.csv"))
chk(bool(lab), "오늘 strategy_lab 산출", "있음" if lab else "results/strategy_lab_오늘.csv 없음")

# 5) 오늘 로그 에러 스캔
err = 0
for lg in glob.glob(os.path.join(LOGS, f"*{TODAY}*.log")):
    try:
        t = open(lg, encoding="utf-8", errors="replace").read()
        err += t.count("Traceback (most recent call last)")
    except Exception:
        pass
chk(err == 0, "오늘 로그 에러(Traceback)", f"{err}건")

# ── 리포트 작성 ──
verdict = "정상" if warn_n == 0 else ("주의" if warn_n <= 2 else "점검필요")
header = f"===== 일일 자동화 자가점검 {NOW.strftime('%Y-%m-%d %H:%M')} — [{verdict}] {ok_n} OK / {warn_n} 경고 ====="
report = header + "\n" + "\n".join(lines) + "\n"

out = os.path.join(LOGS, f"daily_audit_{TODAY}.log")
try:
    os.makedirs(LOGS, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
except Exception:
    pass
print(report)

# 경고가 있으면 텔레그램으로 폰에 푸시 (설정돼 있을 때만 — 없으면 조용히 스킵).
if warn_n > 0:
    _warns = "\n".join(l for l in lines if "[!! ]" in l)
    _sent, _msg = notify(f"⚠ 천억이 자동화 점검 [{verdict}] ({warn_n}건 경고)\n{_warns}\n({NOW.strftime('%m-%d %H:%M')})")
    print(f"  [텔레그램] {'전송됨' if _sent else '미전송 — ' + _msg}")
