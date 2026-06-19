"""
전략 E — 눌림목 매매 (MA 회복).

가설: MA60이 우상향인 상황에서 MA5가 MA20을 상향 돌파하면
      조정 후 재상승(눌림목 회복) 신호.

- 진입 조건:
    1. MA60 우상향: 오늘 MA60 > n일 전 MA60 (기본 5일 전)
    2. MA5 골든크로스 MA20: 오늘 MA5 > MA20 AND 어제 MA5 <= MA20
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 20일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class PullbackMaStrategy(BaseStrategy):
    name = "pullback_ma"
    timeframe = "daily"

    def __init__(self, ma60_slope_days=5, holding_days=20,
                 min_trading_value=3_000_000_000,
                 use_market_filter=False, name=None):
        self.ma60_slope_days = ma60_slope_days
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        df["ma5"]  = grp["close"].transform(lambda x: x.rolling(5,  min_periods=5).mean())
        df["ma20"] = grp["close"].transform(lambda x: x.rolling(20, min_periods=20).mean())
        df["ma60"] = grp["close"].transform(lambda x: x.rolling(60, min_periods=60).mean())

        df["ma60_lag"]  = grp["ma60"].shift(self.ma60_slope_days)
        df["prev_ma5"]  = grp["ma5"].shift(1)
        df["prev_ma20"] = grp["ma20"].shift(1)

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            (df["ma60"] > df["ma60_lag"]) &
            (df["ma5"]      > df["ma20"]) &
            (df["prev_ma5"] <= df["prev_ma20"]) &
            (df["trading_value"] >= self.min_tv) &
            df["ma5"].notna() & df["ma20"].notna() &
            df["ma60"].notna() & df["ma60_lag"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
