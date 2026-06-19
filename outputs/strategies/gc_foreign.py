"""
전략 A6 — 골든크로스(20/60) × 외국인 연속 순매수.

가설: MA20이 MA60을 상향 돌파(추세 전환)하는 타이밍에 외국인도 연속 매수 중이면
      추세 전환이 외부 수급으로 확인된다 → 단순 GC 대비 허위 신호 감소.

- 진입 조건:
    1. MA20 > MA60 AND 어제 MA20 <= MA60 (골든크로스 발생일)
    2. 외국인 순매수 연속 foreign_n일 이상 (foreign_net > 0)
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 15일)
- 손절: stop_loss_pct 설정 시 (예: -6.0)

권장 테스트 overlay:
  python profit_target_test.py --stop -6
  python profit_target_test.py --target 10 --stop -6
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops, _add_market_gate


def _consec_pos(x, n):
    return (x > 0).astype(int).rolling(n, min_periods=n).sum() >= n


class GcForeignStrategy(BaseStrategy):
    name = "gc_foreign"
    timeframe = "daily"

    def __init__(self, fast_ma=20, slow_ma=60, foreign_n=3,
                 holding_days=15,
                 min_trading_value=3_000_000_000,
                 use_market_filter=False,
                 stop_loss_pct=None,
                 name=None):
        self.fast_ma = fast_ma
        self.slow_ma = slow_ma
        self.foreign_n = foreign_n
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        self.stop_loss_pct = stop_loss_pct
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        grp = df.groupby("code")

        df["ma_fast"] = grp["close"].transform(
            lambda x: x.rolling(self.fast_ma, min_periods=self.fast_ma).mean()
        )
        df["ma_slow"] = grp["close"].transform(
            lambda x: x.rolling(self.slow_ma, min_periods=self.slow_ma).mean()
        )

        # 골든크로스: 오늘 ma_fast > ma_slow, 어제는 ma_fast <= ma_slow
        df["prev_fast"] = grp["ma_fast"].shift(1)
        df["prev_slow"] = grp["ma_slow"].shift(1)
        gc = (
            (df["ma_fast"] > df["ma_slow"]) &
            (df["prev_fast"] <= df["prev_slow"]) &
            df["ma_fast"].notna() & df["ma_slow"].notna() &
            df["prev_fast"].notna() & df["prev_slow"].notna()
        )

        # 외국인 연속 순매수
        df["for_consec"] = grp["foreign_net"].transform(
            lambda x: _consec_pos(x, self.foreign_n)
        )

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            gc &
            df["for_consec"] &
            (df["trading_value"] >= self.min_tv) &
            (df["mkt_strong"] if self.use_mkt else True)
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
