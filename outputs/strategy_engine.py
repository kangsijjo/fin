"""
전략 비교 엔진 (strategy_engine.py)
=====================================
전체 전략을 일봉 데이터로 백테스트하고 성과 비교표를 출력한다.

사용법:
    python strategy_engine.py                   # 전체 전략 비교
    python strategy_engine.py --strategy high   # 이름 포함 전략만
    python strategy_engine.py --start 20200101 --end 20241231
    python strategy_engine.py --out results/compare.csv
"""

import argparse
import os
import sys
import math
from datetime import datetime

import pandas as pd

from strategies.high_with_filters  import HighWithFiltersStrategy
from strategies.high_52w           import FiftyTwoWeekHighStrategy
from strategies.breakout_5d        import FiveDayBreakoutStrategy
from strategies.volume_surge       import VolumeSurgeStrategy
from strategies.momentum_5d        import FiveDayMomentumStrategy
from strategies.gap_buy            import GapBuyStrategy
from strategies.rsi_reversal       import RsiReversalStrategy
from strategies.vol_ma_breakout    import VolMaBreakoutStrategy
from strategies.foreign_high       import ForeignHighStrategy
from strategies.swing_golden_cross import SwingGoldenCrossStrategy
from strategies.breakout_20d       import Breakout20DStrategy
from strategies.pullback_ma        import PullbackMaStrategy
from strategies.ma_ribbon          import MaRibbonStrategy
from strategies.macd_cross         import MacdCrossStrategy
from strategies.bb_breakout        import BbBreakoutStrategy
from strategies.bb_squeeze         import BbSqueezeStrategy
from strategies.inst_foreign       import InstForeignStrategy
from strategies.stochastic_cross   import StochasticCrossStrategy
from strategies.high52w_foreign         import High52wForeignStrategy
from strategies.inst_pullback           import InstPullbackStrategy
from strategies.bb_squeeze_breakout     import BbSqueezeBreakoutStrategy
from strategies.rsi_volume              import RsiVolumeStrategy
from strategies.high52w_short_decrease  import High52wShortDecreaseStrategy
from strategies.gc_foreign              import GcForeignStrategy
from strategies.high52w_credit_decrease import High52wCreditDecreaseStrategy
from strategies.daily_loader       import load_macro_daily

DEFAULT_COSTS = {"fee_pct": 0.015, "tax_pct": 0.18, "slip_pct": 0.05, "total_pct": 0.245}
MKT = True

ALL_STRATEGIES = [
    HighWithFiltersStrategy(lookback_days=500, holding_days=40, name="star_high_500d_40"),
    HighWithFiltersStrategy(lookback_days=500, holding_days=20, name="star_high_500d_20"),
    HighWithFiltersStrategy(lookback_days=252, holding_days=20, name="star_high_52w_20_filt"),
    FiftyTwoWeekHighStrategy(lookback_days=252, holding_days=20, name="high_52w_raw"),
    FiveDayBreakoutStrategy(lookback=5, vol_mult=2.0, holding_days=5,  name="breakout_5d"),
    FiveDayBreakoutStrategy(lookback=5, vol_mult=2.0, holding_days=10, name="breakout_5d_h10"),
    VolumeSurgeStrategy(name="vol_surge_1d"),
    FiveDayMomentumStrategy(name="momentum_5d"),
    RsiReversalStrategy(name="rsi_reversal"),
    VolMaBreakoutStrategy(vol_mult=3.0, holding_days=20, name="vol_ma_x3"),
    VolMaBreakoutStrategy(vol_mult=3.0, holding_days=20, use_market_filter=MKT, name="vol_ma_x3_mkt"),
    VolMaBreakoutStrategy(vol_mult=2.0, holding_days=20, use_market_filter=MKT, name="vol_ma_x2_mkt"),
    ForeignHighStrategy(n_days=5, high_period=60, holding_days=20, name="for_high60"),
    ForeignHighStrategy(n_days=3, high_period=20, holding_days=20, use_market_filter=MKT, name="for_high20_mkt"),
    InstForeignStrategy(n_days=3, holding_days=20, name="inst_for_3d"),
    InstForeignStrategy(n_days=3, holding_days=20, use_market_filter=MKT, name="inst_for_3d_mkt"),
    InstForeignStrategy(n_days=5, holding_days=20, use_market_filter=MKT, name="inst_for_5d_mkt"),
    MaRibbonStrategy(new_cross_only=True, holding_days=20, name="ma_ribbon_new"),
    MaRibbonStrategy(new_cross_only=True, holding_days=20, use_market_filter=MKT, name="ma_ribbon_mkt"),
    SwingGoldenCrossStrategy(fast=20, slow=60, holding_days=15, name="gc_20_60"),
    SwingGoldenCrossStrategy(fast=20, slow=60, holding_days=15, use_market_filter=MKT, name="gc_20_60_mkt"),
    SwingGoldenCrossStrategy(fast=5,  slow=20, holding_days=10, use_market_filter=MKT, name="gc_5_20_mkt"),
    PullbackMaStrategy(holding_days=20, name="pullback"),
    PullbackMaStrategy(holding_days=20, use_market_filter=MKT, name="pullback_mkt"),
    Breakout20DStrategy(lookback=20, vol_mult=1.5, holding_days=10, name="bo_20d"),
    Breakout20DStrategy(lookback=20, vol_mult=1.5, holding_days=10, use_market_filter=MKT, name="bo_20d_mkt"),
    Breakout20DStrategy(lookback=60, vol_mult=1.5, holding_days=20, use_market_filter=MKT, name="bo_60d_mkt"),
    MacdCrossStrategy(require_zero_cross=True,  holding_days=20, name="macd_zx"),
    MacdCrossStrategy(require_zero_cross=False, holding_days=20, name="macd_all"),
    MacdCrossStrategy(require_zero_cross=True,  holding_days=20, use_market_filter=MKT, name="macd_zx_mkt"),
    BbBreakoutStrategy(holding_days=10, name="bb_break"),
    BbBreakoutStrategy(holding_days=10, use_market_filter=MKT, name="bb_break_mkt"),
    BbSqueezeStrategy(holding_days=15, name="bb_squeeze"),
    BbSqueezeStrategy(holding_days=15, use_market_filter=MKT, name="bb_squeeze_mkt"),
    StochasticCrossStrategy(oversold=30, holding_days=10, name="stoch_os30"),
    StochasticCrossStrategy(oversold=20, holding_days=10, use_market_filter=MKT, name="stoch_os20_mkt"),
    High52wForeignStrategy(foreign_n=3, holding_days=20, name="h52w_for3d"),
    High52wForeignStrategy(foreign_n=3, holding_days=20, use_market_filter=MKT, name="h52w_for3d_mkt"),
    InstPullbackStrategy(n_days=3, holding_days=15, name="inst_pb"),
    InstPullbackStrategy(n_days=3, holding_days=15, use_market_filter=MKT, name="inst_pb_mkt"),
    BbSqueezeBreakoutStrategy(holding_days=10, name="bb_sqz_bk"),
    BbSqueezeBreakoutStrategy(holding_days=10, use_market_filter=MKT, name="bb_sqz_bk_mkt"),
    RsiVolumeStrategy(holding_days=7, name="rsi_vol"),
    High52wShortDecreaseStrategy(holding_days=20, name="h52w_short_dec"),
    GcForeignStrategy(fast_ma=20, slow_ma=60, foreign_n=3, holding_days=15, name="gc_for3d"),
    GcForeignStrategy(fast_ma=20, slow_ma=60, foreign_n=3, holding_days=15, use_market_filter=MKT, name="gc_for3d_mkt"),
    High52wCreditDecreaseStrategy(holding_days=20, name="h52w_cred_dec"),
]


def _stats(trades, start_date, end_date):
    NULL_KEYS = ["n_trades","annual_trades","win_rate","avg_net","avg_win",
                 "avg_loss","profit_factor","avg_hold","ev_1slot","ev_10slot","mdd_roll","sharpe","worst_trade"]
    if not trades:
        return {k: None for k in NULL_KEYS}
    df = pd.DataFrame([t.__dict__ for t in trades])
    df["entry_date"] = df["entry_date"].astype(str)
    df["exit_date"]  = df["exit_date"].astype(str)
    df = df.sort_values("entry_date")
    n = len(df)
    wins   = df[df["net_pct"] > 0]["net_pct"]
    losses = df[df["net_pct"] <= 0]["net_pct"]
    win_rate      = len(wins) / n * 100
    avg_net       = df["net_pct"].mean()
    avg_win       = wins.mean()  if len(wins)   > 0 else 0.0
    avg_loss      = losses.mean() if len(losses) > 0 else 0.0
    profit_factor = (wins.sum() / (-losses.sum())) if losses.sum() < 0 else 999.0
    avg_hold      = df["holding_days"].mean()
    if start_date and end_date:
        d0 = datetime.strptime(str(start_date), "%Y%m%d")
        d1 = datetime.strptime(str(end_date),   "%Y%m%d")
    else:
        d0 = datetime.strptime(df["entry_date"].min(), "%Y%m%d")
        d1 = datetime.strptime(df["exit_date"].max(),  "%Y%m%d")
    years = max((d1 - d0).days / 365.25, 0.5)
    annual_trades = n / years
    slot_cap  = 252 / max(avg_hold, 1)
    ev_1slot  = min(annual_trades, slot_cap)       * avg_net
    ev_10slot = min(annual_trades, slot_cap * 10.) * avg_net
    roll10 = df["net_pct"].rolling(10, min_periods=1).sum()
    mdd_roll = roll10.min()
    rets = df["net_pct"]
    sharpe = (rets.mean() / (rets.std() + 1e-9)) * math.sqrt(annual_trades)
    return {"n_trades": n, "annual_trades": round(annual_trades,1), "win_rate": round(win_rate,1),
            "avg_net": round(avg_net,2), "avg_win": round(avg_win,2), "avg_loss": round(avg_loss,2),
            "profit_factor": round(profit_factor,2), "avg_hold": round(avg_hold,1),
            "ev_1slot": round(ev_1slot,1), "ev_10slot": round(ev_10slot,1),
            "mdd_roll": round(mdd_roll,2), "sharpe": round(sharpe,2), "worst_trade": round(rets.min(),2)}


def _equity_curve(trades, strategy_name):
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame([t.__dict__ for t in trades])
    df = df.sort_values("entry_date")
    df["cum_pct"] = df["net_pct"].cumsum()
    df["strategy"] = strategy_name
    return df[["strategy","entry_date","exit_date","cum_pct","net_pct","code"]]


def main():
    parser = argparse.ArgumentParser(description="전략 비교 백테스트 엔진")
    parser.add_argument("--start",    type=str, default=None)
    parser.add_argument("--end",      type=str, default=None)
    parser.add_argument("--strategy", type=str, default=None)
    parser.add_argument("--out",      type=str, default=None)
    parser.add_argument("--no-equity", action="store_true")
    args = parser.parse_args()

    strategies = ALL_STRATEGIES
    if args.strategy:
        strategies = [s for s in strategies if args.strategy.lower() in s.name.lower()]
        if not strategies:
            print(f"[ERROR] --strategy '{args.strategy}' 와 일치하는 전략 없음")
            sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  전략 비교 백테스트")
    print(f"  기간: {args.start or '전체'} ~ {args.end or '전체'}")
    print(f"  전략 수: {len(strategies)}")
    print(f"{'='*60}")

    print("\n[1/2] 일봉 데이터 로딩...")
    df_all = load_macro_daily(start_date=args.start, end_date=args.end)
    df_all = df_all.reset_index(drop=True)
    d_min = df_all["date"].min()
    d_max = df_all["date"].max()
    n_code = df_all["code"].nunique()
    print(f"      -> {len(df_all):,}행, {n_code:,}종목, 기간 {d_min} ~ {d_max}")

    results   = []
    eq_frames = []

    print("\n[2/2] 전략 백테스트 실행...")
    for strat in strategies:
        print(f"  > {strat.name} ... ", end="", flush=True)
        try:
            trades = strat.backtest(df_all.copy(), DEFAULT_COSTS)
            stats  = _stats(trades, args.start, args.end)
            stats["strategy"] = strat.name
            results.append(stats)
            if not args.no_equity:
                eq_frames.append(_equity_curve(trades, strat.name))
            print(f"{stats['n_trades']:,}건  승률 {stats['win_rate']}%  "
                  f"평균수익 {stats['avg_net']}%  EV/yr(10slot) {stats['ev_10slot']}%p  "
                  f"MDD(10연속) {stats['mdd_roll']}%")
        except Exception as e:
            print(f"ERROR: {e}")
            import traceback; traceback.print_exc()
            results.append({"strategy": strat.name, "n_trades": 0})

    df_res = pd.DataFrame(results)
    cols_order = ["strategy","n_trades","annual_trades","win_rate","avg_net","avg_win","avg_loss",
                  "profit_factor","avg_hold","ev_1slot","ev_10slot","mdd_roll","sharpe","worst_trade"]
    df_res = df_res.reindex(columns=[c for c in cols_order if c in df_res.columns])
    df_res = df_res.sort_values("ev_10slot", ascending=False).reset_index(drop=True)

    print(f"\n{'='*60}")
    print("  전략 성과 비교표")
    print(f"{'='*60}")
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:.2f}".format)
    print(df_res.to_string(index=False))

    os.makedirs("results", exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    out_path = args.out or f"results/strategy_compare_{today}.csv"
    df_res.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n  -> 비교표 저장: {out_path}")

    if not args.no_equity and eq_frames:
        eq_path = f"results/strategy_equity_{today}.csv"
        pd.concat(eq_frames).to_csv(eq_path, index=False, encoding="utf-8-sig")
        print(f"  -> 수익곡선 저장: {eq_path}")

    print("\n완료.\n")
    return df_res


if __name__ == "__main__":
    main()
