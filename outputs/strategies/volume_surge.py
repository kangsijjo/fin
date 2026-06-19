"""
전략 #12 — 거래량 (거래대금) 급증.

가설: 거래대금이 평소 3배 이상 급증 = 시장 주목 → 단기 모멘텀.
- 진입: 거래대금 30일 평균 3배 이상. 다음날 시가.
- 청산: 1일 보유 후 종가.
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals


class VolumeSurgeStrategy(BaseStrategy):
    name = "volume_surge"
    timeframe = "daily"

    def __init__(self, baseline_days=30, surge_mult=3.0, holding_days=1,
                 min_trading_value=1_000_000_000, name=None):
        self.baseline = baseline_days
        self.mult = surge_mult
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        # transform 사용으로 그룹 경계 유지 (버그 수정 2026-06-18)
        df["tv_baseline"] = df.groupby("code")["trading_value"].transform(
            lambda x: x.shift(1).rolling(self.baseline, min_periods=self.baseline).mean()
        )
        df["signal"] = (
            (df["trading_value"] >= df["tv_baseline"] * self.mult) &
            (df["tv_baseline"] >= self.min_tv)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
