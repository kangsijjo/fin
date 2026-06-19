"""
전략 K — 스토캐스틱 골든크로스.

가설: %K가 %D를 과매도 구간(< 30)에서 상향 돌파하면
      단기 바닥 반등 신호.

- 스토캐스틱:
    %K = (close - lowest_low_N) / (highest_high_N - lowest_low_N) × 100
    %D = %K의 M일 이동평균 (시그널)
- 진입 조건:
    1. %K > %D (오늘) AND %K <= %D (어제) — 신규 골든크로스
    2. 어제 %K < oversold_level (기본 30) — 과매도 구간 탈출
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 10일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class StochasticCrossStrategy(BaseStrategy):
    name = "stochastic_cross"
    timeframe = "daily"

    def __init__(self, k_period=14, d_period=3, oversold=30,
                 holding_days=10, min_trading_value=3_000_000_000,
                 use_market_filter=False, name=None):
        self.k_period = k_period
        self.d_period = d_period
        self.oversold = oversold
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        lowest  = grp["low"].transform(
            lambda x: x.rolling(self.k_period, min_periods=self.k_period).min()
        )
        highest = grp["high"].transform(
            lambda x: x.rolling(self.k_period, min_periods=self.k_period).max()
        )
        denom = (highest - lowest).replace(0, float("nan"))
        df["stoch_k"] = (df["close"] - lowest) / denom * 100
        df["stoch_d"] = grp["stoch_k"].transform(
            lambda x: x.rolling(self.d_period, min_periods=self.d_period).mean()
        )

        df["prev_k"] = grp["stoch_k"].shift(1)
        df["prev_d"] = grp["stoch_d"].shift(1)

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            (df["stoch_k"] > df["stoch_d"]) &
            (df["prev_k"]  <= df["prev_d"]) &
            (df["prev_k"]  <  self.oversold) &
            (df["trading_value"] >= self.min_tv) &
            df["stoch_k"].notna() &
            df["stoch_d"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
