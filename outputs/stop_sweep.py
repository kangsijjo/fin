# -*- coding: utf-8 -*-
"""
stop_sweep.py — 전략 × 손절선(-5~-20%) 백테스트 스윕 (정직한 표만, 자동 '최적' 선택 안 함).

목적(#5 리스크): "어느 손절선이 MDD(최악 낙폭)를 가장 싸게 깎아주나?"를 보기 위한
트레이드오프 표. 각 (전략, 손절선)마다:
  - 슬롯 기준(현실 운용, 10슬롯): 총수익% / 실MDD% / 샤프   (capital_simulator)
  - 무제한 기준: 평균순수익% / 승률% / 손절발동%
를 full(전체기간)·fwd(최근 OOS 구간) 두 범위로 계산. 손절 없음(baseline)도 함께.

손절 시뮬(build_stop_counterfactual 과 동일):
  진입가 = entry_date 시가. 보유 중(진입+1~만기) 저가가 손절가 이하로 내려가면
  exit = min(손절가, 그날 시가)  ← 갭하락이면 시가 체결(현실 반영, 결과 부풀림 방지).
  net = (exit/진입가 - 1)*100 - 비용. 미발동이면 실제 net 그대로.

★ 자동으로 '최적 손절선'을 고르지 않는다. 커브피팅(과거 노이즈 적합) 방지 —
  표를 보고 '평평한 구간(plateau)'과 full↔fwd 일관성을 사람이 판단해 결정.

사용:
  python stop_sweep.py
  python stop_sweep.py --lo -5 --hi -20 --step 1 --fwd-start 20260401
출력: stop_sweep_result.csv  (+ 콘솔 표)
"""
from __future__ import annotations
import os
import sys
import argparse

import numpy as np
import pandas as pd

from strategies.daily_loader import load_macro_daily
from capital_simulator import simulate_capital, build_price_lookup

HIST = "trades_history_v3.csv"
# [2026-07-12 수정] 0.245 → 0.33. trades_history_v3 는 default_costs()(=0.015*2+0.20+0.05*2
# =0.33%)로 백테스트된 net_pct 를 담는데(daily_loader.default_costs; 주석의 '0.43'은 오기,
# 실계산 0.33), 손절 재계산만 0.245 를 쓰면 한 표 안에 두 비용이 섞여 발동 트레이드가
# 미발동 대비 0.085%p 공짜로 유리해짐(발동률 높은 타이트 손절일수록 편향 확대).
COST = 0.33       # 왕복 비용 % (trades_history_v3 net_pct 기준과 정확히 동일)
SLOT_N = 10       # 현실 운용 슬롯 수 (strategy_lab 과 동일)


class _T:
    """capital_simulator 가 읽는 최소 트레이드 객체."""
    __slots__ = ("entry_date", "exit_date", "code", "entry_price", "net_pct", "gross_pct", "_stopped")

    def __init__(self, ed, xd, code, ep, net):
        self.entry_date = ed
        self.exit_date = xd
        self.code = code
        self.entry_price = ep
        self.net_pct = net
        self.gross_pct = net + COST


def _stopped_net(path, ep, stop_frac, orig_net):
    """보유 경로(path=[(date,open,low,close),...])에 stop_frac 적용.
    반환 (net%, exit_date 또는 None). 미발동이면 (orig_net, None).

    ⚠ [2026-07-12 의미론 정합] 라이브 KIS 손절은 'EOD 종가 ≤ 손절가' 판정 후 '익일 09시
    시가' 매도다(profit_sweep 도 EOD 종가 모델). 종전 이 함수는 '장중 저가 터치 → 손절가
    또는 시가 중 유리가 체결'이라 발동은 과대·체결가는 과소(이중 낙관)였다. 라이브와
    동일하게 '판정=그날 종가 ≤ 손절가, 체결=다음날 시가(없으면 그날 종가)'로 교체하고,
    조기 청산일을 함께 돌려줘 MTM 시뮬이 그날 실현하도록 한다."""
    stop_px = ep * (1 + stop_frac)
    for i, (dt, op, lo, cl) in enumerate(path):
        if cl is not None and cl <= stop_px:
            if i + 1 < len(path):
                nxt_dt, nxt_op = path[i + 1][0], path[i + 1][1]
                exitp = nxt_op if (nxt_op and nxt_op > 0) else cl
                return (exitp / ep - 1) * 100 - COST, nxt_dt
            return (cl / ep - 1) * 100 - COST, dt   # 마지막 날 = 만기일과 동일
    return orig_net, None


def _agg(trades_objs, fwd_start, price_map=None, trading_dates=None):
    """주어진 _T 리스트 → full/fwd 범위별 (슬롯·무제한) 지표.

    [2026-07-12] price_map/trading_dates 전달 → MTM 평가. 이 도구의 핵심 목적이
    'MDD 를 가장 싸게 줄이는 손절선' 판단인데, 비-MTM 이면 보유 중 평가손실이 MDD 에
    안 잡혀 슬롯MDD 가 체계적으로 얕게 나와 결론이 왜곡됐음(breaker_sweep 는 이미 MTM)."""
    out = {}
    for scope, sel in (("full", trades_objs),
                       ("fwd", [t for t in trades_objs if str(t.entry_date) >= fwd_start])):
        if not sel:
            out[scope] = None
            continue
        nets = np.array([t.net_pct for t in sel], dtype=float)
        stopped = sum(1 for t in sel if getattr(t, "_stopped", False))
        sim = simulate_capital(sel, max_concurrent=SLOT_N,
                               price_map=price_map, trading_dates=trading_dates)
        out[scope] = {
            "n": len(sel),
            "win": round((nets > 0).mean() * 100, 1),
            "avg": round(float(nets.mean()), 2),
            "stopped_pct": round(stopped / len(sel) * 100, 1),
            "slot_ret": (sim or {}).get("total_ret_pct"),
            "slot_mdd": (sim or {}).get("real_mdd_pct"),
            "slot_sharpe": (sim or {}).get("real_sharpe"),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lo", type=float, default=-5.0, help="손절선 시작(예 -5)")
    ap.add_argument("--hi", type=float, default=-20.0, help="손절선 끝(예 -20)")
    ap.add_argument("--step", type=float, default=1.0, help="간격(%)")
    ap.add_argument("--fwd-start", default="", help="forward 시작일 YYYYMMDD(미지정 시 최근 30%)")
    ap.add_argument("--out", default="stop_sweep_result.csv")
    args = ap.parse_args()

    if not os.path.exists(HIST):
        print(f"[error] {HIST} 없음 — make_trades_history_v3.py 먼저")
        sys.exit(1)

    hist = pd.read_csv(HIST, dtype={"code": str, "entry_date": str, "exit_date": str})
    hist["code"] = hist["code"].str.zfill(6)
    hist = hist[hist["exit_date"].notna() & (hist["exit_date"] != "")].copy()
    print(f"트레이드 {len(hist):,}건 / 전략 {hist['strategy'].nunique()} / 종목 {hist['code'].nunique():,}")

    # forward 기준일: 미지정 시 entry_date 70 분위(최근 30%)
    if args.fwd_start:
        fwd_start = args.fwd_start
    else:
        eds = sorted(hist["entry_date"].unique())
        fwd_start = eds[int(len(eds) * 0.70)] if eds else "99999999"
    print(f"forward 기준일: {fwd_start} (이상 = OOS 최근구간)\n")

    # OHLC 경량 로드 + 보유경로 precompute (전략무관, 손절선마다 재사용)
    df = load_macro_daily()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df[df["code"].isin(set(hist["code"]))]
    df["date"] = df["date"].astype(str)
    all_dates = sorted(df["date"].unique())
    didx = {d: i for i, d in enumerate(all_dates)}
    O = {}; L = {}
    for r in df[["code", "date", "open", "low"]].itertuples(index=False):
        k = (r.code, r.date); O[k] = float(r.open); L[k] = float(r.low)
    price_map, trading_dates = build_price_lookup(df)   # MTM 평가용(2026-07-12)

    pre = []   # 트레이드별 precompute
    skipped = 0
    for t in hist.itertuples(index=False):
        ep = O.get((t.code, t.entry_date), 0.0)
        ei = didx.get(t.entry_date); xi = didx.get(t.exit_date)
        if not ep or ep <= 0 or ei is None or xi is None:
            skipped += 1
            continue
        path = []
        for di in range(ei + 1, xi + 1):
            d = all_dates[di]; k = (t.code, d)
            path.append((d, O.get(k), L.get(k), price_map.get(k)))
        pre.append({"strat": t.strategy, "code": t.code, "ed": t.entry_date,
                    "xd": t.exit_date, "ep": ep, "net": float(t.net_pct), "path": path})
    if skipped:
        print(f"[note] 진입가/날짜 매칭 실패 {skipped:,}건 슬롯시뮬 제외(가격데이터 없음)\n")

    strategies = sorted({p["strat"] for p in pre})
    # 손절선 그리드 + 'none'(baseline)
    levels = []
    v = args.lo
    while v >= args.hi - 1e-9:
        levels.append(round(v, 1)); v -= args.step
    grid = [None] + levels   # None = 손절 없음

    rows = []
    for strat in strategies:
        ps = [p for p in pre if p["strat"] == strat]
        for stop in grid:
            objs = []
            for p in ps:
                if stop is None:
                    net = p["net"]; stopped = False; xd = p["xd"]
                else:
                    net, stop_xd = _stopped_net(p["path"], p["ep"], stop / 100.0, p["net"])
                    stopped = stop_xd is not None
                    xd = stop_xd if stopped else p["xd"]   # 손절 시 조기 청산일(MTM 실현)
                o = _T(p["ed"], xd, p["code"], p["ep"], net)
                o._stopped = stopped
                objs.append(o)
            res = _agg(objs, fwd_start, price_map, trading_dates)
            for scope in ("full", "fwd"):
                r = res[scope]
                if r is None:
                    continue
                rows.append({"scope": scope, "strategy": strat,
                             "stop": "none" if stop is None else stop, **r})

    res_df = pd.DataFrame(rows)
    res_df.to_csv(args.out, index=False, encoding="utf-8-sig")

    # ── 콘솔 표(전략별, scope별) ──
    for strat in strategies:
        for scope in ("full", "fwd"):
            sub = res_df[(res_df.strategy == strat) & (res_df.scope == scope)]
            if sub.empty:
                continue
            print(f"━━ {strat}  [{scope}]  (n={int(sub.iloc[0]['n'])}) "
                  f"━━━━━━━━━━━━━━━━━━━━━━━━")
            print(f"  {'손절':>6} {'슬롯수익%':>9} {'슬롯MDD%':>9} {'샤프':>6} "
                  f"{'평균%':>7} {'승률%':>6} {'손절발동%':>9}")
            for _, r in sub.iterrows():
                s = "없음" if r["stop"] == "none" else f"{r['stop']:g}"
                print(f"  {s:>6} {str(r['slot_ret']):>9} {str(r['slot_mdd']):>9} "
                      f"{str(r['slot_sharpe']):>6} {r['avg']:>7} {r['win']:>6} {r['stopped_pct']:>9}")
            print()

    print(f"[saved] {args.out}  (행 {len(res_df)})")
    print("※ 자동 '최적' 선택 안 함. 'MDD를 가장 싸게 줄이는' 손절선을 표에서 직접 판단.")
    print("  팁: full 과 fwd 에서 '같이' 좋은 구간 + 한 점이 아니라 평평한 구간을 보세요.")


if __name__ == "__main__":
    main()
