"""
전략 A4 — RSI 과매도 × 거래량 급증.

가설: RSI 과매도(공포 매도)에서 거래량이 급증하면 저점 소진 신호 →
      단순 RSI 반전보다 허위 신호(패닉 지속) 감소.

- 진입 조건:
    1. RSI(rsi_period) < rsi_threshold (기본 14기간, 30 이하)
    2. 거래대금 > 자기 vol_ma_days 이동평균 × vol_mult 배 (기본 20일, 2배)
    3. 거래대금 ≥ min_trading_value (절대 유동성 기준)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 7일)
- 손절: stop_loss_pct 설정 시 (예: -5.0)

권장 테스트 overlay:
  python profit_target_test.py --stop -5
  python profit_target_test.py --target 10 --stop -5
"""

import numpy as np
import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops


def _rsi(series, period=14):
    """Welles Wilder RSI."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


class RsiVolumeStrategy(BaseStrategy):
    name = "rsi_volume"
    timeframe = "daily"

    def __init__(self, rsi_period=14, rsi_threshold=30,
                 vol_ma_days=20, vol_mult=2.0,
                 holding_days=7,
                 min_trading_value=1_000_000_000,
                 stop_loss_pct=None,
                 name=None):
        self.rsi_period = rsi_period
        self.rsi_th = rsi_threshold
        self.vol_ma_days = vol_ma_days
        self.vol_mult = vol_mult
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.stop_loss_pct = stop_loss_pct
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        grp = df.groupby("code")

        df["rsi"] = grp["close"].transform(lambda s: _rsi(s, self.rsi_period))

        df["tv_ma"] = grp["trading_value"].transform(
            lambda x: x.rolling(self.vol_ma_days, min_periods=self.vol_ma_days).mean()
        )

        df["signal"] = (
            (df["rsi"] < self.rsi_th) &
            (df["trading_value"] > df["tv_ma"] * self.vol_mult) &
            (df["trading_value"] >= self.min_tv) &
            df["tv_ma"].notna()
        )

        if self.stop_loss_pct is not None:
            return _make_trades_with_stops(
                df, holding_days=self.holding_days,
                strategy_name=self.name, costs=costs,
                stop_loss_pct=self.stop_loss_pct,
            )
        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs,
        )
