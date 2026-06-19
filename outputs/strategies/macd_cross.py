"""
전략 G — MACD 골든크로스.

가설: MACD 라인이 시그널 라인을 상향 돌파할 때 추세 전환 신호.
      히스토그램이 음수에서 양수로 전환되는 순간을 포착.

- MACD: 12일 EMA - 26일 EMA
- Signal: MACD의 9일 EMA
- 진입 조건:
    1. MACD > Signal (오늘) AND MACD <= Signal (어제) — 신규 골든크로스
    2. MACD 히스토그램이 0선 아래에서 돌파 (= 강세 전환, 선택적)
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 20일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class MacdCrossStrategy(BaseStrategy):
    name = "macd_cross"
    timeframe = "daily"

    def __init__(self, fast=12, slow=26, signal=9, holding_days=20,
                 min_trading_value=3_000_000_000,
                 require_zero_cross=True, use_market_filter=False, name=None):
        """
        require_zero_cross: True → 히스토그램이 0선 아래에서 골든크로스할 때만 진입
                            (과매도 → 전환 포착, 과열 구간 재진입 제외)
        """
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.require_zero_cross = require_zero_cross
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        # MACD 계산
        ema_fast = grp["close"].transform(
            lambda x: x.ewm(span=self.fast, adjust=False).mean()
        )
        ema_slow = grp["close"].transform(
            lambda x: x.ewm(span=self.slow, adjust=False).mean()
        )
        df["macd"] = ema_fast - ema_slow
        df["macd_signal"] = grp["macd"].transform(
            lambda x: x.ewm(span=self.signal, adjust=False).mean()
        )
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        df["prev_macd"]   = grp["macd"].shift(1)
        df["prev_signal"] = grp["macd_signal"].shift(1)
        df["prev_hist"]   = grp["macd_hist"].shift(1)

        # 신규 골든크로스
        cross = (df["macd"] > df["macd_signal"]) & (df["prev_macd"] <= df["prev_signal"])

        if self.require_zero_cross:
            # 히스토그램이 음수에서 양수로 전환 (0선 돌파)
            cross = cross & (df["prev_hist"] <= 0)

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            cross &
            (df["trading_value"] >= self.min_tv) &
            df["macd"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
