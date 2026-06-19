"""
전략 #10 — RSI 반전 (mean reversion).

가설: RSI 30 이하는 과매도 → 반등 기대.
- 진입: RSI(14) < 30. 다음날 시가 매수.
- 청산: 5일 보유 후 종가 (단순화 — 진짜 RSI > 50 청산은 가변 보유라 복잡)
"""

import pandas as pd
import numpy as np
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals


def _rsi(series, period=14):
    """Welles Wilder RSI (정통 — HTS/MTS 와 일치).

    SMA 가 아닌 Wilder smoothing (EMA 변형, alpha=1/period).
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    # Wilder smoothing = ewm with alpha=1/period, adjust=False
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


class RsiReversalStrategy(BaseStrategy):
    name = "rsi_reversal"
    timeframe = "daily"

    def __init__(self, rsi_period=14, rsi_threshold=30, holding_days=5,
                 min_trading_value=1_000_000_000, name=None):
        self.rsi_period = rsi_period
        self.rsi_th = rsi_threshold
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        df["rsi"] = df.groupby("code")["close"].transform(
            lambda s: _rsi(s, self.rsi_period))
        df["signal"] = (
            (df["rsi"] < self.rsi_th) &
            (df["trading_value"] >= self.min_tv)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
