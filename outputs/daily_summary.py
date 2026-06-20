"""
daily_summary.py — 장마감 후 '일일 요약'을 텔레그램으로 한 통 보낸다. 2026-06-20 신설.

매매코드를 건드리지 않고 산출 파일만 읽어 요약:
  - 오늘 신호 (paper_signals.csv, signal_date == 오늘)
  - 키움 모의 (db/kiwoom/snapshot.json — 예수금/보유)
  - KIS 모의   (db/kiwoom/kis_snapshot.json — 예수금/보유)

자동: run_live_signal.bat (평일 18:30, daily_audit 직후)에서 호출.
수동: python daily_summary.py
"""
import os
import json
import glob
from datetime import datetime

import notifier

BASE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.now()
TODAY8 = NOW.strftime("%Y%m%d")
parts = [f"📊 천억이 일일요약 {NOW.strftime('%m-%d %H:%M')}"]


def _read_json(path):
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return None


# ── 오늘 신호 (paper_signals.csv) ──
try:
    import csv
    p = os.path.join(BASE, "paper_signals.csv")
    by_strat = {}
    total = 0
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if str(row.get("signal_date", "")).strip() == TODAY8:
                    total += 1
                    s = row.get("strategy", "?")
                    by_strat[s] = by_strat.get(s, 0) + 1
    if total:
        detail = ", ".join(f"{k} {v}" for k, v in by_strat.items())
        parts.append(f"• 오늘 신호 {total}건 ({detail})")
    else:
        parts.append("• 오늘 신호 0건")
except Exception as e:
    parts.append(f"• 신호 집계 실패: {str(e)[:40]}")


# ── 키움 모의 (snapshot.json) ──
snap = _read_json(os.path.join(BASE, "db", "kiwoom", "snapshot.json"))
if snap:
    dep = snap.get("deposit", "?")
    pos = snap.get("positions", []) or []
    dep_s = f"{int(dep):,}" if isinstance(dep, (int, float)) else str(dep)
    parts.append(f"• 키움: 예수금 {dep_s}원 / 보유 {len(pos)}종목")
else:
    parts.append("• 키움: 스냅샷 없음")


# ── KIS 모의 (kis_snapshot.json — db/kiwoom 에 기록됨) ──
ksnap = _read_json(os.path.join(BASE, "db", "kiwoom", "kis_snapshot.json"))
if ksnap:
    dep = ksnap.get("deposit", "?")
    pos = ksnap.get("positions", []) or []
    dep_s = f"{int(dep):,}" if isinstance(dep, (int, float)) else str(dep)
    names = ", ".join(str(x.get("name", x.get("code", "?"))) for x in pos[:5])
    parts.append(f"• KIS: 예수금 {dep_s}원 / 보유 {len(pos)}종목" + (f" ({names})" if names else ""))
else:
    parts.append("• KIS: 스냅샷 없음")


msg = "\n".join(parts)
print(msg)
sent, info = notifier.send(msg)
print(f"[텔레그램] {'전송됨' if sent else '미전송 — ' + info}")
