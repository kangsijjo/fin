"""
전략 C — 스윙 골든크로스 (MA20 × MA60).

가설: MA20이 MA60을 상향 돌파(골든크로스)하고 RSI가 과열/과매도 중간 구간에
      있으면 중기 추세 전환 초입 신호.

- 진입 조건:
    1. 오늘 MA20 > MA60  AND  어제 MA20 <= MA60  (신규 골든크로스)
    2. RSI14 ≥ rsi_low (기본 40) AND RSI14 ≤ rsi_high (기본 70)
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 15일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


def _calc_rsi(close_series, period=14):
    """Wilder's EMA 방식 RSI."""
    delta = close_series.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / (loss + 1e-9)
    return 100 - 100 / (1 + rs)


class SwingGoldenCrossStrategy(BaseStrategy):
    name = "swing_golden_cross"
    timeframe = "daily"

    def __init__(self, fast=20, slow=60, rsi_low=40, rsi_high=70,
                 holding_days=15, min_trading_value=3_000_000_000,
                 use_market_filter=False, name=None):
        self.fast = fast
        self.slow = slow
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        df["ma_fast"] = grp["close"].transform(
            lambda x: x.rolling(self.fast, min_periods=self.fast).mean()
        )
        df["ma_slow"] = grp["close"].transform(
            lambda x: x.rolling(self.slow, min_periods=self.slow).mean()
        )

        rsi_parts = []
        for code, g in grp:
            r = _calc_rsi(g["close"], period=14)
            rsi_parts.append(r)
        df["rsi14"] = pd.concat(rsi_parts).sort_index()

        df["prev_fast"] = grp["ma_fast"].shift(1)
        df["prev_slow"] = grp["ma_slow"].shift(1)

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            (df["ma_fast"] > df["ma_slow"]) &
            (df["prev_fast"] <= df["prev_slow"]) &
            (df["rsi14"] >= self.rsi_low) &
            (df["rsi14"] <= self.rsi_high) &
            (df["trading_value"] >= self.min_tv) &
            df["ma_fast"].notna() &
            df["ma_slow"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
