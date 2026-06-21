"""
build_stop_counterfactual.py — '장중 손절 반사실(counterfactual)' 라벨 데이터셋 생성. 2026-06-21 신설.

목적: 실거래에선 장중 손절을 하지 않지만(백테스트상 평균적으로 손해),
      "그 손절선에서 장중 손절했다면 어땠을까"를 트레이드별로 계산해 라벨로 쌓는다.
      → AI 가 '진입 피처' 만으로 "이 트레이드는 장중 손절이 이득이었나"를 학습할 수 있게 한다.
      (평균 엣지는 음수이므로, 모델의 목표는 '이득인 부분집합'을 찾아내는 것. 라이브 적용 전
       반드시 OOS 검증 필요 — 이 스크립트는 데이터만 만든다.)

입력: trades_history_v3.csv (트레이드 + 진입피처) + macro_data/daily OHLC
출력: ai_data/stop_counterfactual_stop{N}.csv
      = 기존 피처 + [stop_pct_used, entry_price, stop_hit, stop_net, actual_net, stop_better, hold_to_stop]

실행:
  python build_stop_counterfactual.py                # 기본 -10%
  python build_stop_counterfactual.py --stop -15     # 손절선 변경
  여러 레벨로 돌려 concat 하면 stop_pct_used 를 피처로 쓰는 학습도 가능.
"""
import os, sys, argparse
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import pandas as pd
import numpy as np
from strategies.daily_loader import load_macro_daily

COST = 0.245   # 왕복 비용 % (trades_history_v3 의 net_pct 와 동일 기준)
HIST = "trades_history_v3.csv"

ap = argparse.ArgumentParser()
ap.add_argument("--stop", type=float, default=-10.0, help="장중 손절선 %% (예: -10)")
ap.add_argument("--out", default=None)
args = ap.parse_args()
STOP = args.stop / 100.0

print(f"▶ 장중손절 반사실 라벨 생성  (손절선 {args.stop:.0f}%)")
hist = pd.read_csv(HIST, dtype={"code": str})
hist["code"] = hist["code"].str.zfill(6)
hist["entry_date"] = hist["entry_date"].astype(str)
hist["exit_date"] = hist["exit_date"].astype(str)
print(f"  트레이드 {len(hist):,}건 / 종목 {hist['code'].nunique():,}")

# OHLC 경량 로드(필요 종목만)
need = set(hist["code"])
df = load_macro_daily()
df["code"] = df["code"].astype(str).str.zfill(6)
df = df[df["code"].isin(need)]
df["date"] = df["date"].astype(str)
all_dates = sorted(df["date"].unique().tolist())
didx = {d: i for i, d in enumerate(all_dates)}
O = {}; L = {}
for r in df[["code", "date", "open", "low"]].itertuples(index=False):
    k = (r.code, r.date); O[k] = float(r.open); L[k] = float(r.low)

stop_hit = []; stop_net = []; hold_to_stop = []; entry_px_col = []
for t in hist.itertuples(index=False):
    code = t.code; ed = t.entry_date; xd = t.exit_date
    ep = O.get((code, ed), 0.0)   # 진입가 = entry_date 시가
    entry_px_col.append(round(ep, 2) if ep else "")
    ei = didx.get(ed); xi = didx.get(xd)
    if not ep or ep <= 0 or ei is None or xi is None:
        stop_hit.append(False); stop_net.append(""); hold_to_stop.append(""); continue
    stop_px = ep * (1 + STOP)
    hit = False; hd = None; exitp = None
    for di in range(ei + 1, xi + 1):      # 진입 다음날부터 만기까지 장중 저가 체크
        k = (code, all_dates[di])
        lo = L.get(k)
        if lo is None:
            continue
        if lo <= stop_px:
            op = O.get(k, 0.0)
            exitp = min(stop_px, op) if op and op > 0 else stop_px   # 갭하락이면 시가 체결
            hit = True; hd = di - ei
            break
    if hit:
        net = (exitp / ep - 1) * 100 - COST
        stop_hit.append(True); stop_net.append(round(net, 3)); hold_to_stop.append(hd)
    else:
        # 손절 미발동 → 실제 결과와 동일(라벨 비교 시 stop_net=actual)
        stop_hit.append(False); stop_net.append(round(float(t.net_pct), 3)); hold_to_stop.append("")

out = hist.copy()
out["stop_pct_used"] = args.stop
out["entry_price"] = entry_px_col
out["stop_hit"] = stop_hit
out["stop_net"] = stop_net
out["actual_net"] = hist["net_pct"].round(3)
# stop_better: 장중손절본이 실제보다 나았나(발동된 건만 의미 있음; 미발동은 동률 False)
sn = pd.to_numeric(out["stop_net"], errors="coerce")
out["stop_better"] = (out["stop_hit"]) & (sn > out["actual_net"])

os.makedirs("ai_data", exist_ok=True)
outp = args.out or f"ai_data/stop_counterfactual_stop{int(args.stop)}.csv"
out.to_csv(outp, index=False, encoding="utf-8-sig")

# 요약
hit_n = int(out["stop_hit"].sum())
better_n = int(out["stop_better"].sum())
avg_actual = out.loc[out["stop_hit"], "actual_net"].mean() if hit_n else 0
avg_stop = pd.to_numeric(out.loc[out["stop_hit"], "stop_net"], errors="coerce").mean() if hit_n else 0
print(f"\n  ✓ 저장: {outp}  ({len(out):,}행)")
print(f"  장중손절 발동: {hit_n:,}건 ({hit_n/len(out)*100:.1f}%)")
print(f"  발동분 평균: 실제 {avg_actual:+.2f}% vs 장중손절 {avg_stop:+.2f}%  (Δ{avg_stop-avg_actual:+.2f})")
print(f"  장중손절이 이득이었던 트레이드(stop_better=True): {better_n:,}건 "
      f"({better_n/max(hit_n,1)*100:.1f}% of 발동분)  ← AI 가 학습할 양성 라벨")
