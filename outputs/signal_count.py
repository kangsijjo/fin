"""
signal_count.py — 증권사별 '일자별 신호 수' 집계. 2026-06-23 신설.

키움(안C) = paper_signals.csv  /  KIS(안D) = kis_paper_signals.csv
실행:
  python signal_count.py                # 2026-05-20 ~ 현재
  python signal_count.py --since 20260601
"""
import os, sys, argparse, csv
from collections import defaultdict

os.chdir(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument("--since", default="20260520", help="시작일 YYYYMMDD")
args = ap.parse_args()
SINCE = args.since.replace("-", "")


def daily(path, label):
    print(f"\n===== {label}  ({path}) =====")
    if not os.path.exists(path):
        print("  (신호파일 없음 — 아직 생성된 적 없음)")
        return
    by_date = defaultdict(lambda: defaultdict(int))   # date -> {strategy: n}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = str(r.get("signal_date", "")).replace("-", "").strip()
            if len(d) == 8 and d >= SINCE:
                by_date[d][r.get("strategy", "?")] += 1
    if not by_date:
        print(f"  {SINCE} 이후 신호 0건")
        return
    grand = 0
    print(f"  {'날짜':<10} {'합계':>4}   전략별")
    for d in sorted(by_date):
        sd = by_date[d]
        tot = sum(sd.values()); grand += tot
        detail = ", ".join(f"{k} {v}" for k, v in sorted(sd.items()))
        print(f"  {d:<10} {tot:>4}   {detail}")
    print(f"  {'─'*40}\n  {SINCE} 이후 총 {grand}건 / {len(by_date)}일에 신호 발생")


print(f"[신호 일자별 집계]  기준: {SINCE} 이후")
daily("paper_signals.csv", "키움 (안C: high_52w_filt/rsi_reversal/rsi_vol)")
daily("kis_paper_signals.csv", "KIS (안D: h52w_for3d_mkt/for_high20_mkt/gc_for3d)")
