# -*- coding: utf-8 -*-
"""
xover_exit_grid.py — 수급 크로스의 **청산 규칙 그리드** + 무작위 대조 (2026-09-13, 사용자 요청)

사용자 질문: "수익을 5%로 잡으면 승률이 어떻게 될까? 손절선을 8%로 하고"

기존에 확인된 것
  · TP +2% × SL 12수준(−5.0~−10.0) 24조합 전부 음수(2026-09-08, §30).
  · TP +2% 의 승률 86.4% 는 신호가 아니라 **익절 규칙의 산물** — 무작위 진입도 86.1~86.3%(2026-09-09, §31.1).
  · **TP +5% 는 아직 검정한 적이 없다** → 이 스크립트가 그 빈칸을 메운다.

설계
  · 그리드: TP ∈ {없음, 2, 5} × SL ∈ {없음, −8}. 사용자 조합(5/−8)을 포함하고 비교 기준을 같이 놓는다.
  · 각 조합마다 **같은 날·같은 개수·같은 자격조건의 무작위 진입**에 동일 청산을 적용(xover_variants.random_like 재사용).
    승률·평균이 무작위와 같다면 그 숫자는 신호가 아니라 규칙이 만든 것이다.
  · 보고: 승률, 평균 net%, 청산 유형 구성(익절/손절/만기)과 유형별 평균, 무작위 대비 알파, 10슬롯 자본곡선.
  · 청산 우선순위는 엔진 기본값(손절 일중저가 > 익절 일중고가 > 만기) — 같은 날 둘 다 닿으면 **손절 우선**(보수적).

사용: .venv/Scripts/python.exe xover_exit_grid.py [--market both] [--seeds 2]
"""
import os
import sys
import argparse

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from strategies.daily_loader import load_macro_daily
from strategies._swing_base import _make_trades_for_signals, _make_trades_with_stops
from strategy_engine import DEFAULT_COSTS
from capital_simulator import simulate_capital
from xover_variants import XoverVariant, random_like

HOLD = 20
OOS = "20240908"
DATA_DIRS = {"kosdaq": None, "kospi": "macro_data/daily_kospi"}
GRID = [(None, None), (2.0, None), (5.0, None), (None, -8.0), (2.0, -8.0), (5.0, -8.0)]


def run_exit(sig, tp, sl, name):
    if tp is None and sl is None:
        return _make_trades_for_signals(sig, holding_days=HOLD, strategy_name=name, costs=DEFAULT_COSTS)
    return _make_trades_with_stops(sig, holding_days=HOLD, strategy_name=name, costs=DEFAULT_COSTS,
                                   take_profit_pct=tp, tp_fill="high", stop_loss_pct=sl)


def summ(trades, with_cap=False):
    if not trades:
        return None
    x = pd.DataFrame([t.__dict__ for t in trades])
    x["entry_date"] = x["entry_date"].astype(str).str.replace("-", "").str[:8]
    o = x[x.entry_date >= OOS]["net_pct"]
    r = dict(n=len(x), win=(x.net_pct > 0).mean() * 100, avg=x.net_pct.mean(),
             med=x.net_pct.median(), oos=o.mean() if len(o) else np.nan)
    if "exit_reason" in x.columns:
        mix = x.exit_reason.value_counts(normalize=True) * 100
        for k, tag in (("take_profit", "tp"), ("stop_loss", "sl"), ("hold_exit", "ho")):
            r[tag + "_pct"] = mix.get(k, 0.0)
            v = x.loc[x.exit_reason == k, "net_pct"]
            r[tag + "_avg"] = v.mean() if len(v) else np.nan
    if with_cap:
        s = simulate_capital(trades) or {}
        r["cagr"], r["mdd"] = s.get("cagr_pct"), s.get("real_mdd_pct")
    return r


def label(tp, sl):
    a = "익절없음" if tp is None else f"익절+{tp:g}%"
    b = "손절없음" if sl is None else f"손절{sl:g}%"
    return f"{a}/{b}"


def run_market(mk, seeds):
    print(f"\n{'=' * 108}\n {mk.upper()} — 수급 크로스 청산 그리드 (비용 포함 net%, 다음날 시가 진입, 최대 {HOLD}일)\n{'=' * 108}")
    df = load_macro_daily(start_date="20210101", data_dir=DATA_DIRS[mk]).reset_index(drop=True)
    sig = XoverVariant("base").signal_df(df)
    print(f" 크로스 신호 {int(sig['signal'].sum()):,}건 | 종목 {df['code'].nunique():,}")
    rnd = [random_like(sig, 3000 + k) for k in range(max(1, seeds))]

    print(f"\n {'청산규칙':18s}{'매매수':>8s}{'승률':>7s}{'평균net':>9s}{'중앙':>7s}{'OOS':>8s}"
          f"{'익절%':>7s}{'손절%':>7s}{'만기%':>7s}{'만기평균':>9s}{'CAGR':>8s}{'MDD':>8s}")
    rows = {}
    for tp, sl in GRID:
        s = summ(run_exit(sig, tp, sl, f"x_{tp}_{sl}"), with_cap=True)
        if s is None:
            continue
        rows[(tp, sl)] = s
        print(f" {label(tp, sl):18s}{s['n']:8,}{s['win']:6.1f}%{s['avg']:+9.3f}{s['med']:+7.2f}{s['oos']:+8.3f}"
              f"{s.get('tp_pct', np.nan):7.1f}{s.get('sl_pct', np.nan):7.1f}{s.get('ho_pct', np.nan):7.1f}"
              f"{s.get('ho_avg', np.nan):+9.2f}{str(s.get('cagr')):>8s}{str(s.get('mdd')):>8s}")

    print(f"\n {'청산규칙':18s}{'크로스 승률':>12s}{'무작위 승률':>12s}{'차이':>8s} | "
          f"{'크로스 평균':>12s}{'무작위 평균':>12s}{'알파':>9s}")
    for tp, sl in GRID:
        if (tp, sl) not in rows:
            continue
        c = rows[(tp, sl)]
        rs = [summ(run_exit(r, tp, sl, "rnd")) for r in rnd]
        rs = [r for r in rs if r]
        if not rs:
            continue
        rw = float(np.mean([r["win"] for r in rs]))
        ra = float(np.mean([r["avg"] for r in rs]))
        print(f" {label(tp, sl):18s}{c['win']:11.1f}%{rw:11.1f}%{c['win'] - rw:+8.1f}p | "
              f"{c['avg']:+12.3f}{ra:+12.3f}{c['avg'] - ra:+9.3f}")

    # 사용자 조합 상세
    key = (5.0, -8.0)
    if key in rows:
        s = rows[key]
        print(f"\n [상세] 사용자 조합 익절+5% / 손절−8%")
        print(f"   매매 {s['n']:,}건 | 승률 {s['win']:.1f}% | 평균 {s['avg']:+.3f}% | 중앙 {s['med']:+.2f}%")
        print(f"   청산 구성: 익절 {s['tp_pct']:.1f}%(평균 {s['tp_avg']:+.2f}) / "
              f"손절 {s['sl_pct']:.1f}%(평균 {s['sl_avg']:+.2f}) / 만기 {s['ho_pct']:.1f}%(평균 {s['ho_avg']:+.2f})")
        print(f"   기대값 분해: " + " + ".join(
            f"{s[k + '_pct'] / 100:.3f}×{s[k + '_avg']:+.2f}" for k in ("tp", "sl", "ho") if not np.isnan(s[k + "_avg"]))
            + f" = {s['avg']:+.3f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="both", choices=["kosdaq", "kospi", "both"])
    ap.add_argument("--seeds", type=int, default=2)
    a = ap.parse_args()
    for mk in (["kosdaq", "kospi"] if a.market == "both" else [a.market]):
        run_market(mk, a.seeds)


if __name__ == "__main__":
    main()
