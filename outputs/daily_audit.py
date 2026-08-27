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
    # [2026-08-20 문구 수정] 이 숫자는 '전략이 뽑은 후보 수'이고, 하루요약의
    # "오늘 신호 N건"은 '중복 제외 후 CSV 에 새로 저장된 수'다. 같은 메시지 안에서
    # "신호 6건 정상종료" 와 "오늘 신호 0건" 이 나란히 찍혀 모순으로 읽혔다(08-14 실측).
    saved = "저장 0건(전부 중복)" if "모두 중복 신호" in txt else "신규 저장분은 하루요약 참조"
    chk(exit0, "오늘 live_signal 실행",
        f"후보 {cnt}건 / {saved}, " + ("정상종료" if exit0 else "비정상/미완료"))
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

# 5) AI 모델 신선도 (meta_model 만 점검 — 섹터모델은 라이브 미사용이라 학습 중단, 점검 제외)
_meta = os.path.join(BASE, "ai_data", "meta_model_v4.json")
if os.path.exists(_meta):
    _age = _mtime_min(_meta) / 1440.0
    chk(_age <= 10, "meta_model 학습", f"{int(_age)}일 전")
else:
    chk(False, "meta_model", "meta_model_v4.json 없음")



# ── 오늘 로그 에러 집계 헬퍼 (2026-08-26 신설) ────────────────────────────────
def _scan_today_errors(log_dir, today8, exclude_prefix=()):
    """오늘 로그의 '실제 크래시 사건 수'와 회복 여부를 센다.

    반환: (사건수, 회복된 사건수, [(파일명, 건수, 회복여부), ...])

    파이썬의 chained exception 은 사건 하나에
      Traceback ... / The above exception was the direct cause ... /
      During handling of the above exception ...
    처럼 Traceback 헤더를 여러 번 찍는다. 헤더 수를 그대로 세면 사건 수가
    3배로 부풀려져 알림이 실제보다 훨씬 심각해 보인다(08-26: 6건 -> 18건).
    체인 연결 문구 수를 빼서 최상위 사건만 남긴다.

    '회복'은 그 실행의 마지막 ExitCode 가 0 인 경우 — bat 재시도 루프가
    성공했다는 뜻이라 사람이 손댈 일이 없다(08-26 15:21 PM 실사례).
    """
    import glob as _g
    import os as _o
    import re as _re

    total = recovered = 0
    detail = []
    for lg in _g.glob(_o.path.join(log_dir, f"*{today8}*.log")):
        base = _o.path.basename(lg)
        if any(base.startswith(p) for p in exclude_prefix):
            continue          # 자기 리포트를 세지 않는다(리포트 본문에 단어가 들어감)
        try:
            t = open(lg, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        tb = t.count("Traceback (most recent call last)")
        if not tb:
            continue
        chained = (t.count("The above exception was the direct cause")
                   + t.count("During handling of the above exception"))
        n = max(1, tb - chained)
        ecs = _re.findall(r"ExitCode=(\d+)", t)
        ok = bool(ecs) and ecs[-1] == "0"
        total += n
        if ok:
            recovered += n
        detail.append((base, n, ok))
    detail.sort(key=lambda x: -x[1])
    return total, recovered, detail


# 6) 오늘 로그 에러 스캔 — 체인 제외 실제 사건 수, 재시도 회복분은 정상으로 본다
_err, _rec, _detail = _scan_today_errors(LOGS, TODAY, exclude_prefix=("daily_audit",))
_unresolved = _err - _rec
chk(_unresolved == 0, "오늘 로그 에러(크래시)",
    (f"{_err}건 전부 재시도로 회복" if _err and not _unresolved else
     f"{_err}건(미회복 {_unresolved}) — "
     + ", ".join(f"{b} {n}{'✓' if ok else ''}" for b, n, ok in _detail[:3])))

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
