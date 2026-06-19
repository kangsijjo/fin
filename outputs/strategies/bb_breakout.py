"""
전략 H — 볼린저밴드 상단 돌파.

가설: 볼린저밴드 상단을 거래량 동반으로 돌파하면 추세 가속 신호.
      단, 밴드폭이 충분히 넓을 때만 진입 (과도한 수축 후 노이즈 방지).

- 볼린저밴드: 20일 MA ± 2σ
- 진입 조건:
    1. 종가 > 상단밴드 (upper = MA20 + 2*std)
    2. 거래량 ≥ 20일 평균 거래량 × vol_mult (기본 1.5)
    3. BB 위치 (bb_pct) — 상단 돌파이므로 > 1.0
    4. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 10일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class BbBreakoutStrategy(BaseStrategy):
    name = "bb_breakout"
    timeframe = "daily"

    def __init__(self, period=20, std_mult=2.0, vol_mult=1.5,
                 holding_days=10, min_trading_value=3_000_000_000,
                 use_market_filter=False, name=None):
        self.period = period
        self.std_mult = std_mult
        self.vol_mult = vol_mult
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        df["bb_ma"]  = grp["close"].transform(
            lambda x: x.rolling(self.period, min_periods=self.period).mean()
        )
        df["bb_std"] = grp["close"].transform(
            lambda x: x.rolling(self.period, min_periods=self.period).std()
        )
        df["bb_upper"] = df["bb_ma"] + self.std_mult * df["bb_std"]

        df["vol_ma"] = grp["volume"].transform(
            lambda x: x.shift(1).rolling(self.period, min_periods=self.period).mean()
        )

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            (df["close"] > df["bb_upper"]) &
            (df["volume"] >= df["vol_ma"] * self.vol_mult) &
            (df["trading_value"] >= self.min_tv) &
            df["bb_upper"].notna() &
            df["vol_ma"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
