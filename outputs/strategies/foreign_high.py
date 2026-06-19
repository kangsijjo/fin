"""
전략 B — 외국인 연속 매수 + 신고가 조합.

가설: 외국인이 N일 연속 순매수하면서 60일 신고가를 동시에 돌파하면
      수급과 기술적 신호가 동시에 확인된 강세 신호.

- 진입 조건:
    1. 외국인 순매수 연속 n_days일 이상 (foreign_net > 0)
    2. 종가 ≥ 직전 high_period 일 최고가 (60일 신고가 돌파)
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 20일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class ForeignHighStrategy(BaseStrategy):
    name = "foreign_high"
    timeframe = "daily"

    def __init__(self, n_days=5, high_period=60, holding_days=20,
                 min_trading_value=3_000_000_000,
                 use_market_filter=False, name=None):
        self.n_days = n_days
        self.high_period = high_period
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        def _consec_foreign(x):
            pos = (x > 0).astype(int)
            return pos.rolling(self.n_days, min_periods=self.n_days).sum() >= self.n_days

        df["foreign_consec"] = grp["foreign_net"].transform(_consec_foreign)
        df["prev_high_60"] = grp["high"].transform(
            lambda x: x.shift(1).rolling(self.high_period, min_periods=self.high_period).max()
        )

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            df["foreign_consec"] &
            (df["close"] >= df["prev_high_60"]) &
            (df["trading_value"] >= self.min_tv) &
            df["prev_high_60"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
