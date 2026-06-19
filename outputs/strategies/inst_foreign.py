"""
전략 J — 기관 + 외국인 동반 순매수.

가설: 기관과 외국인이 동시에 연속 순매수하면 정보 우위 세력이 함께
      포지션을 쌓는 중 — 개인 역행 방지 + 수급 확인.

- 진입 조건:
    1. 외국인 순매수 연속 n_days일 이상 (foreign_net > 0)
    2. 기관 순매수 연속 n_days일 이상 (inst_net > 0)
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 20일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class InstForeignStrategy(BaseStrategy):
    name = "inst_foreign"
    timeframe = "daily"

    def __init__(self, n_days=3, holding_days=20,
                 min_trading_value=3_000_000_000,
                 use_market_filter=False, name=None):
        self.n_days = n_days
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        def _consec_pos(x, n):
            return (x > 0).astype(int).rolling(n, min_periods=n).sum() >= n

        df["for_consec"] = grp["foreign_net"].transform(
            lambda x: _consec_pos(x, self.n_days)
        )
        df["inst_consec"] = grp["inst_net"].transform(
            lambda x: _consec_pos(x, self.n_days)
        )

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            df["for_consec"] &
            df["inst_consec"] &
            (df["trading_value"] >= self.min_tv) &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)
