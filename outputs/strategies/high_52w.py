"""
전략 #11 — 52주 신고가 돌파.

가설: 1년 신고가 돌파 = 강력한 추세 신호.
- 진입: 종가가 직전 252영업일 최고가 돌파. 다음날 시가 매수.
- 청산: 20일 후 종가.
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals


class FiftyTwoWeekHighStrategy(BaseStrategy):
    name = "high_52w"
    timeframe = "daily"

    def __init__(self, lookback_days=252, holding_days=20,
                 min_trading_value=1_000_000_000, name=None):
        self.lookback = lookback_days
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        # transform 사용으로 그룹 경계 유지 (버그 수정 2026-06-18)
        df["prev_high_252"] = df.groupby("code")["high"].transform(
            lambda x: x.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        )
        df["signal"] = (
            (df["close"] > df["prev_high_252"]) &
            (df["trading_value"] >= self.min_tv)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
