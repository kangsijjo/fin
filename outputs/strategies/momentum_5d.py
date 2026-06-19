"""
전략 #8 — 5일 모멘텀.

가설: 5일 수익률 상위 종목은 추세가 지속됨. 매주 리밸런싱.
- 진입: 5일 수익률 상위 N개, 다음날 시가 매수
- 청산: 5일 후 종가 매도
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals


class FiveDayMomentumStrategy(BaseStrategy):
    name = "momentum_5d"
    timeframe = "daily"

    def __init__(self, top_n=20, holding_days=5,
                 min_trading_value=1_000_000_000, name=None):
        self.top_n = top_n
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        df["ret_5d"] = df.groupby("code")["close"].pct_change(5) * 100

        # 유동성 필터
        df = df[df["trading_value"] >= self.min_tv].copy()

        # 날짜별 5일 수익률 상위 N
        df["rank"] = df.groupby("date")["ret_5d"].rank(ascending=False, method="first")
        df["signal"] = df["rank"] <= self.top_n

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
