"""
전략 A — 거래량 돌파 + 이동평균.

가설: 거래량이 20일 평균의 3배 이상 폭증하면서 종가가 MA20 위에 있으면
      수급이 몰린 추세 전환/가속 신호.

- 진입 조건:
    1. 거래량 ≥ 20일 평균 × vol_mult (기본 3.0)
    2. 종가 ≥ MA20 (20일 단순이동평균)
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 20일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class VolMaBreakoutStrategy(BaseStrategy):
    name = "vol_ma_breakout"
    timeframe = "daily"

    def __init__(self, ma_period=20, vol_mult=3.0, holding_days=20,
                 min_trading_value=3_000_000_000,
                 use_market_filter=False, name=None):
        self.ma_period = ma_period
        self.vol_mult = vol_mult
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        df["ma20"] = grp["close"].transform(
            lambda x: x.rolling(self.ma_period, min_periods=self.ma_period).mean()
        )
        df["vol_ma20"] = grp["volume"].transform(
            lambda x: x.shift(1).rolling(self.ma_period, min_periods=self.ma_period).mean()
        )

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            (df["volume"] >= df["vol_ma20"] * self.vol_mult) &
            (df["close"] >= df["ma20"]) &
            (df["trading_value"] >= self.min_tv) &
            df["ma20"].notna() &
            df["vol_ma20"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
