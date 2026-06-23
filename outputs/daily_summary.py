"""
daily_summary.py — 장마감 후 '하루 요약'을 텔레그램으로 한 통 보낸다. 2026-06-20 / 확장 2026-06-23.

매매코드를 건드리지 않고 산출 파일만 읽어 요약:
  - 오늘 신호 (paper_signals.csv, signal_date == 오늘)
  - 오늘 체결 (db/kiwoom/kis_orders_*.csv, orders_*.csv — 매수/매도 건수+종목)
  - KIS 모의   (kis_snapshot.json — 총평가/예수금/평가손익/보유)
  - 키움 모의 (snapshot.json — 예수금/보유)
  - 손절 모니터 (kis_stop_monitor.json — 손절선 임박/도달)
  - 파이프라인 상태 (오늘 로그 Traceback 유무)

자동: run_live_signal.bat (평일 18:30, daily_audit 직후)에서 호출.  수동: python daily_summary.py
"""
import os
import csv
import json
import glob
from datetime import datetime

import notifier

BASE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(os.path.dirname(BASE), "logs")
if not os.path.isdir(LOGS):
    LOGS = os.path.join(BASE, "logs")
NOW = datetime.now()
TODAY8 = NOW.strftime("%Y%m%d")
parts = [f"📊 천억이 하루요약 {NOW.strftime('%m-%d %H:%M')}"]


def _read_json(path):
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return None


def _won(v):
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


# ── 오늘 신호 (paper_signals.csv) ──
try:
    p = os.path.join(BASE, "paper_signals.csv")
    by_strat, total = {}, 0
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if str(row.get("signal_date", "")).strip() == TODAY8:
                    total += 1
                    by_strat[row.get("strategy", "?")] = by_strat.get(row.get("strategy", "?"), 0) + 1
    parts.append(f"• 오늘 신호 {total}건" + (f" ({', '.join(f'{k} {v}' for k, v in by_strat.items())})" if total else ""))
except Exception as e:
    parts.append(f"• 신호 집계 실패: {str(e)[:40]}")


# ── 오늘 체결 (kis_orders_*, orders_*) ──
def _today_fills(prefix, label):
    f = os.path.join(BASE, "db", "kiwoom", f"{prefix}_{TODAY8}.csv")
    if not os.path.exists(f):
        return None
    buys, sells = [], []
    try:
        with open(f, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if str(r.get("ok", "")).strip().lower() in ("false", "0"):
                    continue
                (buys if str(r.get("side", "")).lower() == "buy" else sells).append(r.get("name", r.get("code", "")))
    except Exception:
        return None
    if not buys and not sells:
        return f"• {label} 체결: 없음"
    seg = []
    if buys:
        seg.append(f"🟢매수 {len(buys)}({', '.join(buys[:4])})")
    if sells:
        seg.append(f"🔴매도 {len(sells)}({', '.join(sells[:4])})")
    return f"• {label} 체결: " + " / ".join(seg)

for pre, lab in [("kis_orders", "KIS"), ("orders", "키움")]:
    line = _today_fills(pre, lab)
    if line:
        parts.append(line)


# ── KIS 모의 (총평가/예수금/평가손익/보유) ──
ksnap = _read_json(os.path.join(BASE, "db", "kiwoom", "kis_snapshot.json"))
if ksnap:
    pos = ksnap.get("positions", []) or []
    names = ", ".join(str(x.get("name", x.get("code", "?"))) for x in pos[:5])
    teval = ksnap.get("total_eval")
    head = f"• KIS: 총평가 {_won(teval)}원" if teval is not None else f"• KIS: 예수금 {_won(ksnap.get('deposit', '?'))}원"
    pnl = ksnap.get("eval_pnl")
    if pnl is not None:
        head += f" / 평가손익 {('+' if int(pnl) >= 0 else '')}{_won(pnl)}"
    head += f" / 보유 {len(pos)}종목" + (f" ({names})" if names else "")
    parts.append(head)
else:
    parts.append("• KIS: 스냅샷 없음")


# ── 키움 모의 (예수금/보유) ──
snap = _read_json(os.path.join(BASE, "db", "kiwoom", "snapshot.json"))
if snap:
    pos = snap.get("positions", []) or []
    parts.append(f"• 키움: 예수금 {_won(snap.get('deposit', '?'))}원 / 보유 {len(pos)}종목")
else:
    parts.append("• 키움: 스냅샷 없음")


# ── 손절 모니터 (임박/도달) ──
mon = _read_json(os.path.join(BASE, "db", "kiwoom", "kis_stop_monitor.json"))
if mon and mon.get("items"):
    fired = [m for m in mon["items"] if m.get("room_pp", 99) <= 0]
    imm = [m for m in mon["items"] if 0 < m.get("room_pp", 99) <= 3]
    if fired or imm:
        seg = []
        if fired:
            seg.append("도달 " + ", ".join(m.get("name", "") for m in fired[:4]))
        if imm:
            seg.append("임박 " + ", ".join(m.get("name", "") for m in imm[:4]))
        parts.append("• ⚠ 손절: " + " / ".join(seg))


# ── 파이프라인 상태 (오늘 로그 Traceback) ──
try:
    err = 0
    for lg in glob.glob(os.path.join(LOGS, f"*{TODAY8}*.log")):
        try:
            err += open(lg, encoding="utf-8", errors="replace").read().count("Traceback (most recent call last)")
        except Exception:
            pass
    parts.append("• 파이프라인: 정상" if err == 0 else f"• ⚠ 파이프라인 에러 {err}건 (로그 확인)")
except Exception:
    pass


msg = "\n".join(parts)
print(msg)
sent, info = notifier.send(msg)
print(f"[텔레그램] {'전송됨' if sent else '미전송 — ' + info}")
