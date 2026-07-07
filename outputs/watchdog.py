# -*- coding: utf-8 -*-
"""
watchdog.py — '돌았어야 할 시점에 안 돌면' 자동 감지 → 텔레그램 경보.

이 시스템의 #1 약점인 '조용한 실패'(돌아가는 것처럼 보이는데 안 돌아감)를 잡는다.
이번까지 실제로 터진 실패 모드를 그대로 점검 항목으로 만든 것:
  1) 스케줄러 데몬 죽음(상주 루프 누락) → ♥ 하트비트 끊김
  2) 데이터 수집 정지 → stock.db 테이블 정체
  3) 신호 생성 미실행(18:30) → live_signal/kis_signal 오늘 로그 없음
  4) 매매 작업 미실행(09:01) → kis_trader 오늘 로그 없음
  5) 잔고 스냅샷 정지(15:40) → kis/kiwoom 스냅샷 날짜 과거

동작:
  - 평일/시각을 인지해 '지금쯤 됐어야 할 것'만 점검(0건 정상인 날 오탐 방지: 로그 존재 여부로 판정).
  - 문제 발견 시 한 통으로 묶어 텔레그램. 같은 문제는 하루 1회만(스팸 방지).
  - `--daily` 로 하루 1회 실행하면 정상이어도 'OK N/N' 하트비트를 보냄
    → 그게 안 오면 워치독/PC/텔레그램이 죽은 것(침묵 자체가 경보).

실행: python watchdog.py            # 평상시(이상 있을 때만 알림)
      python watchdog.py --daily    # 하루 마감 점검(정상이어도 하트비트 전송)
"""

from __future__ import annotations
import os
import sys
import json
import glob
import sqlite3
from datetime import datetime, date

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOGS = os.path.normpath(os.path.join(_HERE, "..", "logs"))
_STATE = os.path.join(_HERE, "db", "watchdog_state.json")
_KIWOOM_DIR = os.path.join(_HERE, "db", "kiwoom")
_STOCK_DB_PATHS = [
    os.path.join(_HERE, "..", "Stock_AI_Project", "data", "stock.db"),
    "C:/fin/Stock_AI_Project/data/stock.db",
]

NOW = datetime.now()
TODAY = NOW.date()
TODAY_STR = TODAY.strftime("%Y%m%d")
IS_WEEKDAY = TODAY.weekday() < 5


# ── 유틸 ──────────────────────────────────────────────────────────────────────
def _after(hhmm: str) -> bool:
    """현재 시각이 hhmm(예 '18:35') 이후인가."""
    h, m = map(int, hhmm.split(":"))
    return NOW >= NOW.replace(hour=h, minute=m, second=0, microsecond=0)


def _ran_today(pattern: str) -> bool:
    """logs/ 에서 pattern 에 맞는 파일 중 파일명에 오늘 날짜가 든 게 있나(=오늘 실행됨)."""
    for f in glob.glob(os.path.join(_LOGS, pattern)):
        if TODAY_STR in os.path.basename(f):
            return True
    return False


def _open_db():
    for p in _STOCK_DB_PATHS:
        try:
            if os.path.exists(p):
                return sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
        except Exception:
            continue
    return None


def _max_date(con, table, col="date"):
    try:
        r = con.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
        return str(r[0])[:10] if r and r[0] else None
    except Exception:
        return None


def _age_days(datestr):
    if not datestr:
        return 9999
    try:
        fmt = "%Y-%m-%d" if "-" in datestr else "%Y%m%d"
        return (NOW - datetime.strptime(datestr[:10], fmt)).days
    except Exception:
        return 9999


def _snap_date(fname):
    try:
        d = json.loads(open(os.path.join(_KIWOOM_DIR, fname), encoding="utf-8").read())
        return str(d.get("date", ""))[:10].replace("-", "")
    except Exception:
        return ""


# ── 점검 항목 ─────────────────────────────────────────────────────────────────
def check_scheduler():
    """스케줄러 데몬 생존 — scheduler.log 마지막 ♥ 하트비트가 15분 이내인가."""
    path = os.path.join(_LOGS, "scheduler.log")
    if not os.path.exists(path):
        return ("scheduler", "스케줄러 데몬", False, "scheduler.log 없음 — 데몬 미가동?")
    last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-300:]
        for ln in lines:
            if "alive" in ln and ln.startswith("["):
                try:
                    ts = ln[1:ln.index("]")]
                    last = datetime.strptime(ts.strip()[:19], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
    except Exception as e:
        return ("scheduler", "스케줄러 데몬", False, f"로그 읽기 실패: {e}")
    # 보조 생존신호: scheduler.log 가 최근에 쓰이고 있으면 데몬 활동 중.
    # (scheduler.py 는 단일스레드라 daily_data 같은 긴 수집(06:30~09시 전후) 동안
    #  ♥ 잡이 실행되지 못하고, 뉴스 수집이 수백 줄을 쏟아내 ♥ 가 tail 300줄 밖으로
    #  밀림 → 07:45 점검이 '하트비트 없음' 오탐. 로그 mtime 으로 보완 — 2026-07-02)
    try:
        mtime_min = (NOW - datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 60.0
    except Exception:
        mtime_min = 1e9
    if last is None:
        if mtime_min <= 15:
            return ("scheduler", "스케줄러 데몬", True,
                    f"정상(수집 작업 진행 중 — 로그 활동 {max(0, mtime_min):.0f}분 전, ♥는 작업 후 재개)")
        if _scheduler_process_alive():
            return ("scheduler", "스케줄러 데몬", True,
                    "정상(로그 핸들로 생존 확인 — 긴 수집 자식작업 중, 출력은 완료 후 일괄 기록)")
        return ("scheduler", "스케줄러 데몬", False, "하트비트(♥) 기록 없음")
    age_min = (NOW - last).total_seconds() / 60.0
    if age_min > 15:
        if mtime_min <= 15:
            return ("scheduler", "스케줄러 데몬", True,
                    f"정상(수집 작업 진행 중 — 마지막 ♥ {age_min:.0f}분 전이나 로그 활동 {max(0, mtime_min):.0f}분 전)")
        if _scheduler_process_alive():
            return ("scheduler", "스케줄러 데몬", True,
                    f"정상(로그 핸들로 생존 확인 — 마지막 ♥ {age_min:.0f}분 전, 긴 수집 자식작업 중)")
        return ("scheduler", "스케줄러 데몬", False,
                f"응답 없음 — 마지막 ♥ {age_min:.0f}분 전(데몬 죽었을 수 있음). start_scheduler.bat 확인")
    return ("scheduler", "스케줄러 데몬", True, f"정상(♥ {max(0, age_min):.0f}분 전)")


def _scheduler_process_alive():
    """데몬 생존 3차 신호 — scheduler.log 를 배타모드로 열어보고 '잠김'이면 생존.

    배경: 토요일 갭백필처럼 단일 자식작업이 2시간+ 돌면 스케줄러가 자식 출력을 완료 후
    일괄 기록해 로그 mtime 까지 정체 → ♥/mtime 휴리스틱이 둘 다 오탐(2026-07-04 실제 발생).
    프로세스 커맨드라인 조회는 데몬이 관리자 권한(작업스케줄러 Highest)이라 일반 권한에선
    비어 나옴 → 대신 **데몬(start /min ... >> scheduler.log)이 로그 append 핸들을 상시
    보유**한다는 사실을 이용: 배타 열기가 공유위반으로 실패하면 = 데몬 생존.
    ※ 한계: (a) taillog 등 다른 프로세스가 로그를 잡고 있으면 거짓 생존(경보 억제 방향),
      (b) '떠 있으나 교착'은 못 구분 — 둘 다 다음날 데이터 신선도 점검이 결과물 정체로 잡음."""
    try:
        import subprocess
        log_path = os.path.join(_LOGS, "scheduler.log").replace("\\", "/")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"try{{$f=[IO.File]::Open('{log_path}','Open','ReadWrite','None');"
             f"$f.Close();'FREE'}}catch{{'LOCKED'}}"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace")
        return "LOCKED" in (r.stdout or "")
    except Exception:
        return False


def check_data():
    """데이터 신선도 — stock.db 6종. (주기별 임계: 일일 5~6, 뉴스 6, 신용 10)"""
    con = _open_db()
    if con is None:
        return [("db_open", "stock.db", False, "DB 열기 실패")]
    out = []
    specs = [("korea_stocks", "date", 5), ("supply_demand", "date", 5),
             ("usa_stocks", "date", 6), ("macro_indicators", "date", 6),
             ("credit_balance", "date", 10), ("news", "pubDate", 6)]
    for tbl, col, thr in specs:
        latest = _max_date(con, tbl, col)
        age = _age_days(latest)
        ok = age <= thr
        out.append((f"data_{tbl}", f"데이터 {tbl}", ok,
                    f"{age}일 정체(최신 {latest})" if not ok else f"최신 {latest}"))
    con.close()
    return out


def check_signals():
    """신호 생성 — 평일 18:35 이후, 오늘 live_signal/kis_signal 로그가 있나(=작업 실행됨)."""
    out = []
    if IS_WEEKDAY and _after("18:35"):
        out.append(("sig_kiwoom", "키움 신호생성(18:30)", _ran_today("live_signal_*.log"),
                    "오늘 live_signal 미실행"))
        out.append(("sig_kis", "KIS 신호생성(18:31)", _ran_today("kis_signal_*.log"),
                    "오늘 kis_signal 미실행"))
    return out


def check_trading():
    """매매 작업 — 평일 09:10 이후, 오늘 kis_trader 로그가 있나(=09:01 매매 실행됨)."""
    out = []
    if IS_WEEKDAY and _after("09:10"):
        out.append(("trade_kis", "KIS 매매(09:01)", _ran_today("kis_trader_*.log"),
                    "오늘 kis_trader 미실행"))
    return out


def check_snapshots():
    """잔고 스냅샷 — 평일 15:45 이후, KIS/키움 스냅샷 날짜가 오늘인가(=status 갱신됨)."""
    out = []
    if IS_WEEKDAY and _after("15:45"):
        out.append(("snap_kis", "KIS 잔고 스냅샷(15:40)", _snap_date("kis_snapshot.json") == TODAY_STR,
                    f"오늘 미갱신(최신 {_snap_date('kis_snapshot.json') or '없음'})"))
    return out


def run_all_checks():
    results = []
    results.append(check_scheduler())
    results += check_data()
    results += check_signals()
    results += check_trading()
    results += check_snapshots()
    return results


# ── 상태(중복 알림 방지) ──────────────────────────────────────────────────────
def _load_state():
    try:
        s = json.loads(open(_STATE, encoding="utf-8").read())
        if s.get("date") == TODAY.isoformat():
            return s
    except Exception:
        pass
    return {"date": TODAY.isoformat(), "alerted": []}


def _save_state(s):
    try:
        os.makedirs(os.path.dirname(_STATE), exist_ok=True)
        with open(_STATE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
    except Exception:
        pass


def main():
    daily = "--daily" in sys.argv
    results = run_all_checks()
    fails = [(k, lbl, det) for (k, lbl, ok, det) in results if not ok]
    n_total, n_ok = len(results), len(results) - len(fails)

    state = _load_state()
    new_fails = [(k, lbl, det) for (k, lbl, det) in fails if k not in state["alerted"]]

    try:
        import notifier
    except Exception as e:
        print(f"[watchdog] notifier 임포트 실패: {e}")
        notifier = None

    msg = None
    if daily:
        # 하루 마감 하트비트 — 정상이어도 보냄(침묵=경보 원리)
        if fails:
            body = "\n".join(f"• {lbl}: {det}" for _, lbl, det in fails)
            msg = f"⚠️ [감시] 마감 점검 — 이상 {len(fails)}건 (정상 {n_ok}/{n_total})\n{body}\n(미해결 항목 점검 요망)"
        else:
            msg = f"✅ [감시] 오늘 점검 정상 ({n_ok}/{n_total} 통과) — {TODAY.isoformat()}"
        # 마감 땐 모든 현재 이상을 alerted 처리
        state["alerted"] = sorted(set(state["alerted"]) | {k for k, _, _ in fails})
    elif new_fails:
        body = "\n".join(f"• {lbl}: {det}" for _, lbl, det in new_fails)
        msg = f"⚠️ [감시] 점검 이상 {len(new_fails)}건 ({NOW.strftime('%m/%d %H:%M')})\n{body}"
        state["alerted"] = sorted(set(state["alerted"]) | {k for k, _, _ in new_fails})

    # 콘솔 출력(수동 점검/로그용)
    print(f"[watchdog] {NOW.strftime('%Y-%m-%d %H:%M')}  정상 {n_ok}/{n_total}"
          + (f", 이상 {len(fails)}건" if fails else ""))
    for k, lbl, ok, det in results:
        print(f"  [{'OK' if ok else 'XX'}] {lbl}: {det}")

    if msg and notifier is not None:
        sent = notifier.safe_send(msg) if hasattr(notifier, "safe_send") else notifier.send(msg)
        print(f"[watchdog] 텔레그램 전송: {sent}")
    elif msg:
        print(f"[watchdog] (notifier 없음) 미전송 메시지:\n{msg}")
    else:
        print("[watchdog] 신규 이상 없음 — 알림 생략")

    _save_state(state)


if __name__ == "__main__":
    main()
