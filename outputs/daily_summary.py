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
                side = str(r.get("side", "")).lower()
                # [2026-07-11] tp_sell 은 익절 지정가 '발주' 행 — 체결이 아니므로 집계 제외.
                # (체결되면 _reconcile_tp_fills 가 side='sell' 합성행을 남겨 그걸로 집계.
                #  기존엔 미체결 발주가 매일 허위 매도로, 실체결 시엔 2건 이중 계상됐음)
                if side == "buy":
                    buys.append(r.get("name", r.get("code", "")))
                elif side == "sell":
                    sells.append(r.get("name", r.get("code", "")))
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


# ── 파이프라인 상태 (오늘 로그 크래시 사건 수) ──
try:
    err, rec, detail = _scan_today_errors(LOGS, TODAY8, exclude_prefix=("daily_audit",))
    if err == 0:
        parts.append("• 파이프라인: 정상")
    elif err == rec:
        # 전부 재시도로 회복 — 사람이 손댈 일이 없다. 경고 아이콘을 쓰지 않는다.
        parts.append(f"• 파이프라인: 크래시 {err}건 전부 재시도로 회복 (조치 불필요)")
    else:
        top = ", ".join(f"{b.split('_2026')[0]} {n}건{' (회복)' if ok else ''}"
                        for b, n, ok in detail[:3])
        parts.append(f"• ⚠ 파이프라인 크래시 {err}건"
                     + (f" (그중 {rec}건 재시도 회복)" if rec else "")
                     + f" — {top}")
except Exception:
    pass


msg = "\n".join(parts)
print(msg)
sent, info = notifier.send(msg)
print(f"[텔레그램] {'전송됨' if sent else '미전송 — ' + info}")
