"""
전략 F — MA 정배열 (이동평균 리본).

가설: MA5 > MA20 > MA60 > MA120 이 모두 정배열이고 신규로 정렬되는 시점이
      강세 추세의 가장 확신도 높은 신호.

- 진입 조건:
    1. MA5 > MA20 > MA60 > MA120 (오늘)
    2. 어제는 정배열이 아니었음 (= 신규 정배열 발생)
       OR 정배열 진입 후 N일 이내 (momentum 옵션)
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 20일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class MaRibbonStrategy(BaseStrategy):
    name = "ma_ribbon"
    timeframe = "daily"

    def __init__(self, holding_days=20, min_trading_value=3_000_000_000,
                 new_cross_only=True, use_market_filter=False, name=None):
        """
        new_cross_only: True → 신규 정배열 시점만 진입 (어제 정배열 아니었음)
                        False → 정배열 유지 중 매일 진입 가능
        """
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.new_cross_only = new_cross_only
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        df["ma5"]   = grp["close"].transform(lambda x: x.rolling(5,   min_periods=5).mean())
        df["ma20"]  = grp["close"].transform(lambda x: x.rolling(20,  min_periods=20).mean())
        df["ma60"]  = grp["close"].transform(lambda x: x.rolling(60,  min_periods=60).mean())
        df["ma120"] = grp["close"].transform(lambda x: x.rolling(120, min_periods=120).mean())

        ribbon = (
            (df["ma5"] > df["ma20"]) &
            (df["ma20"] > df["ma60"]) &
            (df["ma60"] > df["ma120"])
        )

        if self.new_cross_only:
            prev_ribbon = ribbon.groupby(df["code"]).shift(1).fillna(False)
            sig = ribbon & ~prev_ribbon
        else:
            sig = ribbon

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            sig &
            (df["trading_value"] >= self.min_tv) &
            df["ma5"].notna() & df["ma20"].notna() &
            df["ma60"].notna() & df["ma120"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
