"""
전략 D — 20일 신고가 돌파 매매.

가설: 20일 신고가를 거래량 증가 동반으로 돌파하면 단기 모멘텀 가속 신호.

- 진입 조건:
    1. 종가 > 직전 20일 최고가 (high 기준)
    2. 거래량 ≥ 20일 평균 거래량 × vol_mult (기본 1.5)
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 10일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class Breakout20DStrategy(BaseStrategy):
    name = "breakout_20d"
    timeframe = "daily"

    def __init__(self, lookback=20, vol_mult=1.5, holding_days=10,
                 min_trading_value=3_000_000_000,
                 use_market_filter=False, name=None):
        self.lookback = lookback
        self.vol_mult = vol_mult
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        df["prev_high_20"] = grp["high"].transform(
            lambda x: x.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        )
        df["vol_ma20"] = grp["volume"].transform(
            lambda x: x.shift(1).rolling(self.lookback, min_periods=self.lookback).mean()
        )

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            (df["close"] > df["prev_high_20"]) &
            (df["volume"] >= df["vol_ma20"] * self.vol_mult) &
            (df["trading_value"] >= self.min_tv) &
            df["prev_high_20"].notna() &
            df["vol_ma20"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
