# -*- coding: utf-8 -*-
"""
recover_stale.py — 유니버스 지연으로 아침 매수를 건너뛴 날의 자동 복구. (2026-10-30, 설명서 §81)

상황: 전날 15:50 수집·20:00 재수집이 모두 실패하면 08:50/09:00 에 '유니버스 N거래일 밀림 → 오늘 매수 건너뜀'(§80).
      종전엔 사용자가 수동으로 ①빠진 날 수집 ②신호 다시 만들기 ③매수 를 했다(10-30 11:29~12:26).
동작(작업 스케줄러 StockAI\\StaleRecover — 평일 13:00, 30분마다 14:00 까지 재시도):
  0) 오늘 아침 건너뛴 기록(db/stale_skip.json, 트레이더가 남김)이 없으면 아무것도 안 함. 이미 복구했어도 안 함.
  1) 유니버스가 아직 밀렸으면 update_macro_daily.py 로 빠진 날 수집(구멍만). 그래도 밀렸으면 다음 시도로(14:00 이 마지막).
  2) 신호 다시 만들기: live_signal.py(키움)·kis_live_signal.py(KIS) 를 동시에, 이어서 가상매매 신호·장부.
  3) 15:00 전이면 건너뛴 계좌만 매수(키움: buy → 익절 지정가, KIS: buy). 이미 보유/주문한 종목·꽉 찬 칸은 평소처럼 건너뜀.
  4) 텔레그램으로 결과. 기록에 done 표시(같은 날 다시 안 함).
주의(사용자 고지): 시가가 아니라 오후 시장가에 사므로 백테스트(다음날 시가)와 가격이 다르다. 15:00 이후엔 매수 생략
(15:05 손절 점검·15:21 만기 매도·마감과 겹치지 않게).
수동: python recover_stale.py [--dry-run] [--force]   (--force: 기록이 없어도 지금 유니버스가 밀렸으면 진행)
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LAST_TRY = "1400"           # 이 시각 이후 시작한 시도는 수집이 안 돼도 '오늘 포기'로 끝냄
BUY_DEADLINE = "1500"       # 이 시각 이후엔 매수하지 않음
ACCT = {"키움 안C": [["kiwoom_trader.py", "buy"], ["kiwoom_trader.py", "targets"]],
        "KIS 안D": [["kis_trader.py", "buy"]]}


def _py():
    p = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    return p if os.path.exists(p) else sys.executable


def _notify(msg):
    print(msg, flush=True)
    try:
        import notifier
        notifier.safe_send(msg)
    except Exception:
        pass


def _run(args, timeout):
    print(f"\n$ python {' '.join(args)}", flush=True)
    try:
        r = subprocess.run([_py(), "-u"] + args, cwd=HERE, timeout=timeout,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
        return r.returncode
    except subprocess.TimeoutExpired:
        print(f"[recover] 시간 초과({timeout}초): {args[0]}")
        return 124


def _gap():
    import market_calendar as mc
    return mc.universe_gap(mc.latest_universe_date(), datetime.now().strftime("%Y%m%d"))


def plan(now=None, force=False):
    """오늘 할 일 판정 → (할일 'skip'|'collect'|'signals', 이유, 기록)."""
    import market_calendar as mc
    now = now or datetime.now()
    today = now.strftime("%Y%m%d")
    if not mc.is_trading_day(now):
        return "skip", "휴장일", {}
    rec = mc.load_stale_skip()
    if rec.get("date") != today:
        if not force:
            return "skip", "오늘 아침 건너뛴 매수 없음", rec
        rec = {"date": today, "accounts": list(ACCT), "latest": mc.latest_universe_date()}
    if rec.get("done"):
        return "skip", f"이미 복구함({rec.get('done')})", rec
    n, _ = _gap()
    return ("collect" if n else "signals"), (f"유니버스 {n}거래일 밀림" if n else "유니버스 정상"), rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    import market_calendar as mc
    now = datetime.now()
    hm = now.strftime("%H%M")
    todo, why, rec = plan(now, a.force)
    print(f"=== 유니버스 지연 복구 {now:%Y-%m-%d %H:%M} — {todo}: {why} ===", flush=True)
    if todo == "skip" or a.dry_run:
        return 0
    accts = [x for x in rec.get("accounts", []) if x in ACCT]

    # 1) 빠진 날 수집
    if todo == "collect":
        _run(["update_macro_daily.py"], timeout=1800)
        n, missing = _gap()
        if n:
            last = hm >= LAST_TRY
            if last:
                rec["done"] = f"{now:%H:%M} 포기(수집 실패)"
                mc.save_stale_skip(rec)
            _notify(f"⚠ [유니버스 복구] 빠진 거래일 {', '.join(missing)} 수집 실패 — "
                    + ("오늘은 복구 매수 포기(네트워크·KRX 확인 요망)" if last else "30분 뒤 다시 시도"))
            return 2

    # 2) 신호 다시 만들기 — 두 계좌 동시(각 수십 분)
    t0 = datetime.now()
    procs = []
    for s in ("live_signal.py", "kis_live_signal.py"):
        print(f"\n$ python {s} (병렬)", flush=True)
        procs.append((s, subprocess.Popen([_py(), "-u", s], cwd=HERE,
                                          env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))))
    rcs = {}
    for s, p in procs:
        try:
            rcs[s] = p.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            p.kill()
            rcs[s] = 124
    _run(["virtual_signal.py"], timeout=1800)
    _run(["ai_paper_trader.py"], timeout=600)
    print(f"[recover] 신호 재생성 {(datetime.now() - t0).seconds // 60}분 — rc {rcs}", flush=True)

    # 3) 매수 — 15:00 전, 아침에 건너뛴 계좌만
    if datetime.now().strftime("%H%M") >= BUY_DEADLINE:
        rec["done"] = f"{datetime.now():%H:%M} 신호만(15:00 지나 매수 생략)"
        mc.save_stale_skip(rec)
        _notify("⚠ [유니버스 복구] 신호는 다시 만들었지만 15:00 이 지나 오늘 매수는 생략 — 내일 09:00 부터 정상")
        return 0
    res = []
    for acct in accts:
        rcs_a = [_run(cmd, timeout=900) for cmd in ACCT[acct]]
        res.append(f"{acct} {'✅' if all(r == 0 for r in rcs_a) else '❌ rc=' + str(rcs_a)}")
    rec["done"] = f"{datetime.now():%H:%M} 복구 매수"
    mc.save_stale_skip(rec)
    _notify("🔁 [유니버스 복구] 빠진 날 수집·신호 재생성 후 아침에 건너뛴 매수를 실행했습니다 — "
            + (" / ".join(res) or "대상 계좌 없음")
            + "\n(시가가 아닌 오후 시장가 매수 — 체결 내역은 계좌별 알림 참고)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
