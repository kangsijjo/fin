"""
전략 A2 — 기관+외국인 동반 순매수 × 눌림목 매수.

가설: 기관/외국인이 연속 매수 중인데 주가가 60일선 아래 or 5일 연속 하락(눌림목)이면
      단기 약세를 틈타 수급 세력이 추가 매집 — 반등 기대.

- 진입 조건:
    1. 외국인 + 기관 순매수 연속 n_days일 이상
    2. 눌림목: (종가 < 60일 이동평균) OR (5일 연속 하락: close < close[-5])
    3. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 15일)
- 손절: stop_loss_pct 설정 시 (예: -5.0 ~ -7.0)

권장 테스트 overlay:
  python profit_target_test.py --stop -5
  python profit_target_test.py --stop -7
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops, _add_market_gate


def _consec_pos(x, n):
    return (x > 0).astype(int).rolling(n, min_periods=n).sum() >= n


class InstPullbackStrategy(BaseStrategy):
    name = "inst_pullback"
    timeframe = "daily"

    def __init__(self, n_days=3, ma_days=60, holding_days=15,
                 min_trading_value=3_000_000_000,
                 use_market_filter=False,
                 stop_loss_pct=None,
                 name=None):
        self.n_days = n_days
        self.ma_days = ma_days
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        self.stop_loss_pct = stop_loss_pct
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        grp = df.groupby("code")

        # 기관 + 외국인 연속 순매수
        df["for_consec"] = grp["foreign_net"].transform(
            lambda x: _consec_pos(x, self.n_days)
        )
        df["inst_consec"] = grp["inst_net"].transform(
            lambda x: _consec_pos(x, self.n_days)
        )

        # 눌림목 1: 종가 < 60일 이동평균
        df["ma60"] = grp["close"].transform(
            lambda x: x.rolling(self.ma_days, min_periods=self.ma_days).mean()
        )
        below_ma = df["close"] < df["ma60"]

        # 눌림목 2: 5일 연속 하락 (현재 종가 < 5일 전 종가)
        df["close_5d_ago"] = grp["close"].shift(5)
        falling_5d = (df["close"] < df["close_5d_ago"]) & df["close_5d_ago"].notna()

        pullback = below_ma | falling_5d

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            df["for_consec"] &
            df["inst_consec"] &
            pullback &
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
