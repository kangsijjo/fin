# -*- coding: utf-8 -*-
"""
xover_stop_sweep.py — 수급선 역전(+2% 익절) 전략에 손절을 붙였을 때의 손절 수준 스윕. (2026-09-08)

배경
  supply_xover_tp2 는 승률 86% 인데 평균 -0.50%: 익절 도달 86%(+1.86%) vs 미도달 만기 14%(-15.4%).
  손실이 전부 '미도달 꼬리'에서 나오는 구조라 손절이 부호를 바꿀 수 있는지 사용자가 물었다.
  (2026-06 stop_sweep 의 '손절 기각'은 양의 기대값 전략에서 회복할 종목을 잘라낸 경우 — 다른 질문.)

읽는 법 (과적합 방어 — 이게 이 스크립트의 존재 이유)
  · 11개 중 '최고점 하나'를 고르지 않는다. **곡선 전체**를 본다: 넓게 양수(고원)면 진짜,
    한두 점만 튀면 노이즈.
  · 고른 수준의 **표본외(OOS, 최근 2년)** 를 확인한다.
  · 손절 수준은 사전등록 범위 [없음, -5.0, -5.5, ..., -10.0] 로 고정. 범위를 넓히거나
    다시 돌리지 않는다.

방법
  신호는 한 번만 계산(SupplyCrossoverStrategy.signal_df) → 각 손절 수준별로
  _make_trades_with_stops(take_profit_pct=2.0, stop_loss_pct=s). 손절은 일중 저가 기준,
  갭하락으로 시가가 이미 손절가 아래면 시가 체결(현실적). 같은 날 손절·익절 동시면 손절 우선.

출력: 표 + results/xover_stop_sweep_YYYYMMDD.csv
사용: python xover_stop_sweep.py [--start 20210101] [--oos 20240908]
"""
import os
import sys
import argparse
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from strategies.daily_loader import load_macro_daily
from strategies.supply_reversal import SupplyCrossoverStrategy
from strategies._swing_base import _make_trades_with_stops
from strategy_engine import DEFAULT_COSTS
from capital_simulator import simulate_capital

LEVELS = [None] + [round(-5.0 - 0.5 * i, 1) for i in range(11)]   # 없음, -5.0 ... -10.0
HOLD = 20
# 익절 목표는 --tp 로 지정(기본 2.0). "none" 이면 익절 없이 손절+20일 만기만 —
# 사용자 2차 요청(2026-09-08): 익절의 왜곡을 빼고 '신호 + 손절'만의 효과를 본다.


def _summ(trades, oos_cutoff):
    if not trades:
        return None
    x = pd.DataFrame([t.__dict__ for t in trades])
    x["entry_date"] = x["entry_date"].astype(str)
    n = len(x)
    wins = x["net_pct"] > 0
    pf_num = x.loc[wins, "net_pct"].sum()
    pf_den = -x.loc[~wins, "net_pct"].sum()
    mix = x["exit_reason"].value_counts(normalize=True) * 100
    ins = x[x["entry_date"] < oos_cutoff]["net_pct"]
    oos = x[x["entry_date"] >= oos_cutoff]["net_pct"]
    sim = simulate_capital(trades) or {}
    return {
        "n_trades": n,
        "win_rate": round(wins.mean() * 100, 1),
        "avg_net": round(x["net_pct"].mean(), 3),
        "median_net": round(x["net_pct"].median(), 2),
        "pf": round(pf_num / pf_den, 2) if pf_den > 0 else float("inf"),
        "tp_pct": round(mix.get("take_profit", 0.0), 1),
        "stop_pct": round(mix.get("stop_loss", 0.0), 1),
        "hold_pct": round(mix.get("hold_exit", 0.0), 1),
        "avg_stop_net": round(x.loc[x.exit_reason == "stop_loss", "net_pct"].mean(), 2)
                        if (x.exit_reason == "stop_loss").any() else None,
        "is_avg": round(ins.mean(), 3) if len(ins) else None,
        "oos_avg": round(oos.mean(), 3) if len(oos) else None,
        "oos_n": int(len(oos)),
        "real_cagr": sim.get("cagr_pct"),
        "real_mdd": sim.get("real_mdd_pct"),
        "real_sharpe": sim.get("real_sharpe"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20210101")
    ap.add_argument("--oos", default="20240908", help="표본외 시작(YYYYMMDD)")
    ap.add_argument("--tp", default="2.0", help="익절 목표 %% (예 2.0) 또는 none=익절 없음")
    ap.add_argument("--market", default="kosdaq", choices=["kosdaq", "kospi"],
                    help="kosdaq=기본 macro_data/daily(1,822종목) / kospi=macro_data/daily_kospi(943종목)")
    a = ap.parse_args()
    TP = None if str(a.tp).lower() == "none" else float(a.tp)
    # 유니버스별 폴더 — KOSPI 는 별도 수집(backfill_kospi_daily.py). KOSDAQ 오염 방지.
    data_dir = None if a.market == "kosdaq" else "macro_data/daily_kospi"
    tag = ("notp" if TP is None else f"tp{TP:g}") + f"_{a.market}"

    print(f"[sweep] 데이터 로드 {a.start}~ ... (시장 {a.market.upper()}, 익절: {'없음' if TP is None else str(TP)+'%'}, 만기 {HOLD}일)")
    df = load_macro_daily(start_date=a.start, data_dir=data_dir).reset_index(drop=True)
    print(f"[sweep] {len(df):,}행 {df['code'].nunique():,}종목 {df['date'].min()}~{df['date'].max()}")

    strat = SupplyCrossoverStrategy(take_profit_pct=TP)
    sig = strat.signal_df(df)
    print(f"[sweep] 신호 {int(sig['signal'].sum()):,}건 — 손절 {len(LEVELS)}단계 스윕 시작")

    rows = []
    for lv in LEVELS:
        name = f"{tag}_nostop" if lv is None else f"{tag}_sl{lv}"
        t = _make_trades_with_stops(sig, holding_days=HOLD, strategy_name=name, costs=DEFAULT_COSTS,
                                    take_profit_pct=TP, stop_loss_pct=lv)
        s = _summ(t, a.oos)
        if s is None:
            continue
        s = {"stop": "없음" if lv is None else f"{lv:+.1f}%", **s}
        rows.append(s)
        print(f"  손절 {s['stop']:>6s} | n {s['n_trades']:6,} | 승률 {s['win_rate']:5.1f}% | 평균 {s['avg_net']:+.3f}% "
              f"| PF {s['pf']:.2f} | TP/SL/만기 {s['tp_pct']:.0f}/{s['stop_pct']:.0f}/{s['hold_pct']:.0f}% "
              f"| IS {s['is_avg']:+.3f} OOS {s['oos_avg']:+.3f} | CAGR {s['real_cagr']} MDD {s['real_mdd']}")

    res = pd.DataFrame(rows)
    os.makedirs("results", exist_ok=True)
    out = f"results/xover_stop_sweep_{tag}_{datetime.now():%Y%m%d}.csv"
    res.to_csv(out, index=False, encoding="utf-8-sig")

    pos = res[res["stop"] != "없음"]
    n_pos_is = int((pos["avg_net"] > 0).sum())
    n_pos_oos = int((pos["oos_avg"].fillna(-1) > 0).sum())
    print()
    print("=" * 78)
    print(f" 고원 판정: 손절 11단계 중 평균수익>0 {n_pos_is}개 / 표본외>0 {n_pos_oos}개")
    print("   → 넓게 양수면 진짜, 한두 점만 양수면 노이즈. 최고점 하나로 결론 내지 말 것.")
    print(f" 저장: {out}")
    print("=" * 78)


if __name__ == "__main__":
    main()
