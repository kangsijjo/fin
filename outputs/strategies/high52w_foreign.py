"""
전략 A1 — 52주 신고가 × 외국인 연속 순매수.

가설: 52주 신고가 돌파(모멘텀) + 외국인 n일 연속 순매수(정보 세력 수급 확인) →
      단순 신고가 전략 대비 허위 신호 감소.

- 진입 조건:
    1. 종가 > 직전 252 거래일 고가 (52주 신고가 돌파)
    2. 외국인 순매수 연속 foreign_n일 이상 (foreign_net > 0)
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 20일)
- 손절: stop_loss_pct 설정 시 (예: -8.0) 일중 저가 기준 발동

권장 테스트 overlay:
  python profit_target_test.py --stop -8  (baseline 유지, -8% 손절 추가)
  python profit_target_test.py --stop -10
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops, _add_market_gate


def _consec_pos(x, n):
    return (x > 0).astype(int).rolling(n, min_periods=n).sum() >= n


class High52wForeignStrategy(BaseStrategy):
    name = "high52w_foreign"
    timeframe = "daily"

    def __init__(self, lookback_days=252, foreign_n=3, holding_days=20,
                 min_trading_value=3_000_000_000,
                 use_market_filter=False,
                 stop_loss_pct=None,
                 name=None):
        self.lookback = lookback_days
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

        # 52주 신고가 신호 (전일까지의 lookback 최고가 대비 오늘 종가)
        df["prev_high"] = grp["high"].transform(
            lambda x: x.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        )

        # 외국인 연속 순매수
        df["for_consec"] = grp["foreign_net"].transform(
            lambda x: _consec_pos(x, self.foreign_n)
        )

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            (df["close"] > df["prev_high"]) &
            df["for_consec"] &
            (df["trading_value"] >= self.min_tv) &
            df["prev_high"].notna() &
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
