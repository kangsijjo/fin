"""
전략 #9 — 5일 신고가 돌파.

가설: 직전 5일 최고가 + 거래량 2배 동반 돌파는 강한 상승 신호.
- 진입: 종가가 직전 5일 최고가 돌파 + 거래량 5일 평균 2배. 다음날 시가 매수.
- 청산: 5일 후 종가.
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals


class FiveDayBreakoutStrategy(BaseStrategy):
    name = "breakout_5d"
    timeframe = "daily"

    def __init__(self, lookback=5, vol_mult=2.0, holding_days=5,
                 min_trading_value=1_000_000_000, name=None):
        self.lookback = lookback
        self.vol_mult = vol_mult
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        # transform 사용으로 그룹 경계 유지 (버그 수정 2026-06-18)
        df["prev_high"] = df.groupby("code")["high"].transform(
            lambda x: x.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        )
        df["prev_vol_mean"] = df.groupby("code")["volume"].transform(
            lambda x: x.shift(1).rolling(self.lookback, min_periods=self.lookback).mean()
        )
        df["signal"] = (
            (df["close"] > df["prev_high"]) &
            (df["volume"] >= df["prev_vol_mean"] * self.vol_mult) &
            (df["trading_value"] >= self.min_tv)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
