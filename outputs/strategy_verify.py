"""
strategy_verify.py — 상위 전략 상세 검증
==========================================
백테스트 1회 실행 후 post-hoc 방식으로:
  1. 슬리피지 민감도  : 추가 슬리피지 0 ~ 1.0 % 구간별 avg_net / 손익분기 슬리피지
  2. 연도별 성과       : 알파가 특정 연도에 집중되었는지 확인
  3. 손익비 분포       : 이익/손실 거래 분포 요약 (분위수)

사용법:
    python strategy_verify.py --start 20190101 --end 20251231
    python strategy_verify.py --start 20190101 --end 20251231 --out results/verify.xlsx
"""

import argparse
import os
import math
from datetime import datetime

import pandas as pd
import numpy as np

# ── 전략 임포트 ──────────────────────────────────────────────
from strategies.high_with_filters  import HighWithFiltersStrategy
from strategies.high_52w           import FiftyTwoWeekHighStrategy
from strategies.rsi_reversal       import RsiReversalStrategy
from strategies.foreign_high       import ForeignHighStrategy
from strategies.daily_loader       import load_macro_daily

# ── 검증 대상 전략 (상위 5개) ────────────────────────────────
BASE_COSTS = {
    "fee_pct":   0.015,
    "tax_pct":   0.18,
    "slip_pct":  0.05,
    "total_pct": 0.245,
}

VERIFY_STRATEGIES = [
    ("rsi_reversal",     RsiReversalStrategy(name="rsi_reversal")),
    ("high_52w_filt",    HighWithFiltersStrategy(lookback_days=252, holding_days=20,
                                                 name="high_52w_filt")),
    ("high_52w_raw",     FiftyTwoWeekHighStrategy(lookback_days=252, holding_days=20,
                                                  name="high_52w_raw")),
    ("high_500d_20",     HighWithFiltersStrategy(lookback_days=500, holding_days=20,
                                                 name="high_500d_20")),
    ("for_high20_mkt",   ForeignHighStrategy(n_days=3, high_period=20, holding_days=20,
                                              use_market_filter=True,
                                              name="for_high20_mkt")),
]

# 슬리피지 테스트 레벨 (추가 슬리피지 %, 편도 기준)
# 왕복 비용에 추가되므로 × 2 해서 net_pct 에서 차감
SLIP_LEVELS = [0.0, 0.05, 0.10, 0.20, 0.30, 0.50, 0.75, 1.00]


# ── 헬퍼 ────────────────────────────────────────────────────

def _base_stats(df):
    """거래 DataFrame → 기본 통계 dict."""
    n = len(df)
    if n == 0:
        return {}
    wins   = df[df["net_pct"] > 0]["net_pct"]
    losses = df[df["net_pct"] <= 0]["net_pct"]
    return {
        "n"    : n,
        "wr"   : round(len(wins) / n * 100, 1),
        "avg"  : round(df["net_pct"].mean(), 3),
        "med"  : round(df["net_pct"].median(), 3),
        "pf"   : round(wins.sum() / (-losses.sum()), 3) if losses.sum() < 0 else 999,
        "aw"   : round(wins.mean(),   2) if len(wins)   > 0 else 0,
        "al"   : round(losses.mean(), 2) if len(losses) > 0 else 0,
        "std"  : round(df["net_pct"].std(), 3),
    }


def slip_sensitivity(trades_df):
    """
    슬리피지 민감도 테이블.
    각 SLIP_LEVELS 에서 net_pct = 원본 - 2*추가슬리피지 (왕복) 로 재계산.
    break_even_slip: avg_net = 0 이 되는 편도 슬리피지 (선형 근사).
    """
    rows = []
    base_avg = trades_df["net_pct"].mean()

    for slip in SLIP_LEVELS:
        adj = trades_df["net_pct"] - 2 * slip   # 왕복 추가 비용
        n   = len(adj)
        wins   = adj[adj > 0]
        losses = adj[adj <= 0]
        pf = wins.sum() / (-losses.sum()) if losses.sum() < 0 else 999
        rows.append({
            "extra_slip_%": slip,
            "total_slip_%": round(BASE_COSTS["slip_pct"] + slip, 3),
            "n_trades"    : n,
            "win_rate_%"  : round(len(wins) / n * 100, 1),
            "avg_net_%"   : round(adj.mean(), 3),
            "profit_factor": round(pf, 3),
        })

    df_out = pd.DataFrame(rows)

    # 손익분기 슬리피지 (선형 보간: avg = base_avg - 2*slip = 0 → slip = base_avg/2)
    bep = round(base_avg / 2, 3) if base_avg > 0 else 0.0
    return df_out, bep


def yearly_breakdown(trades_df, start_y=None, end_y=None):
    """연도별 avg_net, win_rate, n_trades."""
    df = trades_df.copy()
    df["year"] = df["entry_date"].str[:4].astype(int)
    years = sorted(df["year"].unique())
    if start_y:
        years = [y for y in years if y >= start_y]
    if end_y:
        years = [y for y in years if y <= end_y]

    rows = []
    for yr in years:
        g = df[df["year"] == yr]
        s = _base_stats(g)
        rows.append({
            "year"        : yr,
            "n_trades"    : s.get("n", 0),
            "win_rate_%"  : s.get("wr", 0),
            "avg_net_%"   : s.get("avg", 0),
            "profit_factor": s.get("pf", 0),
            "avg_win_%"   : s.get("aw", 0),
            "avg_loss_%"  : s.get("al", 0),
        })
    return pd.DataFrame(rows)


def distribution_summary(trades_df):
    """수익률 분포 요약 (분위수 + 극단값)."""
    s = trades_df["net_pct"]
    qs = [1, 5, 10, 25, 50, 75, 90, 95, 99]
    row = {f"p{q}": round(float(np.percentile(s, q)), 2) for q in qs}
    row["mean"] = round(s.mean(), 3)
    row["std"]  = round(s.std(), 3)
    row["min"]  = round(s.min(), 2)
    row["max"]  = round(s.max(), 2)
    row["skew"] = round(float(s.skew()), 3)
    return pd.DataFrame([row])


# ── 메인 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end",   type=str, default=None)
    parser.add_argument("--out",   type=str, default=None)
    args = parser.parse_args()

    print(f"\n{'='*65}")
    print(f"  전략 상세 검증 (슬리피지 민감도 · 연도별 · 분포)")
    print(f"  기간: {args.start or '전체'} ~ {args.end or '전체'}")
    print(f"{'='*65}")

    print("\n[1/2] 일봉 데이터 로딩...")
    df_all = load_macro_daily(start_date=args.start, end_date=args.end)
    df_all = df_all.reset_index(drop=True)
    print(f"      → {len(df_all):,}행, {df_all['date'].min()} ~ {df_all['date'].max()}")

    os.makedirs("results", exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    out_base = args.out or f"results/verify_{today}"

    all_slip_rows   = []   # 슬리피지 민감도 전체
    all_yearly_rows = []   # 연도별 전체
    all_dist_rows   = []   # 분포 전체

    print("\n[2/2] 전략별 검증...")

    start_y = int(args.start[:4]) if args.start else None
    end_y   = int(args.end[:4])   if args.end   else None

    for name, strat in VERIFY_STRATEGIES:
        print(f"\n  ━━━ {name} ━━━")
        try:
            trades = strat.backtest(df_all.copy(), BASE_COSTS)
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback; traceback.print_exc()
            continue

        if not trades:
            print("  거래 0건 — 스킵")
            continue

        tdf = pd.DataFrame([t.__dict__ for t in trades])
        tdf["entry_date"] = tdf["entry_date"].astype(str)

        s = _base_stats(tdf)
        print(f"  총 {s['n']:,}건  승률 {s['wr']}%  avg_net {s['avg']}%  "
              f"avg_win {s['aw']}%  avg_loss {s['al']}%  PF {s['pf']}")

        # ── 1. 슬리피지 민감도 ──
        slip_df, bep = slip_sensitivity(tdf)
        slip_df.insert(0, "strategy", name)
        all_slip_rows.append(slip_df)
        print(f"\n  [슬리피지 민감도]  손익분기 편도 슬리피지 ≈ {bep:.3f}%")
        print("  extra_slip%  win_rate%  avg_net%  PF")
        for _, r in slip_df.iterrows():
            marker = " ← break-even" if abs(r["avg_net_%"]) < 0.05 else ""
            neg    = "❌" if r["avg_net_%"] < 0 else "  "
            print(f"  {neg} {r['extra_slip_%']:5.2f}%     "
                  f"{r['win_rate_%']:5.1f}%    "
                  f"{r['avg_net_%']:+7.3f}%  "
                  f"{r['profit_factor']:5.3f}{marker}")

        # ── 2. 연도별 성과 ──
        yr_df = yearly_breakdown(tdf, start_y, end_y)
        yr_df.insert(0, "strategy", name)
        all_yearly_rows.append(yr_df)

        print(f"\n  [연도별 성과]")
        print(f"  {'연도':>4}  {'건수':>5}  {'승률':>6}  {'avg_net':>8}  {'PF':>5}")
        for _, r in yr_df.iterrows():
            neg = "❌" if r["avg_net_%"] < 0 else "  "
            print(f"  {neg}{int(r['year']):4d}  {int(r['n_trades']):5d}  "
                  f"{r['win_rate_%']:5.1f}%  {r['avg_net_%']:+7.3f}%  {r['profit_factor']:5.3f}")

        # ── 3. 수익률 분포 ──
        dist_df = distribution_summary(tdf)
        dist_df.insert(0, "strategy", name)
        all_dist_rows.append(dist_df)

        print(f"\n  [수익률 분포]  "
              f"min {dist_df['min'].iloc[0]}%  "
              f"p10 {dist_df['p10'].iloc[0]}%  "
              f"p25 {dist_df['p25'].iloc[0]}%  "
              f"median {dist_df['p50'].iloc[0]}%  "
              f"p75 {dist_df['p75'].iloc[0]}%  "
              f"p90 {dist_df['p90'].iloc[0]}%  "
              f"max {dist_df['max'].iloc[0]}%  "
              f"skew {dist_df['skew'].iloc[0]}")

    # ── CSV 저장 ──────────────────────────────────────────────
    if all_slip_rows:
        p = f"{out_base}_slip.csv"
        pd.concat(all_slip_rows).to_csv(p, index=False, encoding="utf-8-sig")
        print(f"\n슬리피지 민감도 저장: {p}")

    if all_yearly_rows:
        p = f"{out_base}_yearly.csv"
        pd.concat(all_yearly_rows).to_csv(p, index=False, encoding="utf-8-sig")
        print(f"연도별 성과 저장: {p}")

    if all_dist_rows:
        p = f"{out_base}_dist.csv"
        pd.concat(all_dist_rows).to_csv(p, index=False, encoding="utf-8-sig")
        print(f"수익률 분포 저장: {p}")

    print("\n완료.\n")


if __name__ == "__main__":
    main()
