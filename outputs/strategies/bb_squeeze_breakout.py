"""
전략 A3 — 볼린저밴드 수축 후 상단 돌파.

기존 bb_squeeze 와의 차이:
  bb_squeeze   : 수축 해소 + 종가 > 중심선 (BB middle)
  bb_squeeze_breakout: 수축 해소 + 종가 > 상단 밴드 (BB upper = middle + 2*std)
  → 진입 조건이 더 엄격 (상단 돌파 확인) → 신호 감소 but 강한 모멘텀만 선별

- 진입 조건:
    1. BB 밴드폭 이전날 하위 squeeze_rank%(기본 20%) 이하 수축 상태
    2. 오늘 밴드폭 > 어제 밴드폭 (수축 해소 시작)
    3. 종가 > BB 상단 (middle + 2*std) — 강한 상방 돌파
    4. 거래대금 ≥ min_trading_value
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 10일)
- 손절: stop_loss_pct 설정 시 (예: -5.0)

권장 테스트 overlay:
  python profit_target_test.py --stop -5
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops, _add_market_gate


class BbSqueezeBreakoutStrategy(BaseStrategy):
    name = "bb_squeeze_breakout"
    timeframe = "daily"

    def __init__(self, bb_period=20, squeeze_window=120, squeeze_rank=20,
                 std_mult=2.0, holding_days=10,
                 min_trading_value=3_000_000_000,
                 use_market_filter=False,
                 stop_loss_pct=None,
                 name=None):
        self.bb_period = bb_period
        self.squeeze_window = squeeze_window
        self.squeeze_rank = squeeze_rank
        self.std_mult = std_mult
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        self.stop_loss_pct = stop_loss_pct
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        grp = df.groupby("code")

        df["bb_ma"] = grp["close"].transform(
            lambda x: x.rolling(self.bb_period, min_periods=self.bb_period).mean()
        )
        df["bb_std"] = grp["close"].transform(
            lambda x: x.rolling(self.bb_period, min_periods=self.bb_period).std()
        )
        df["bb_upper"] = df["bb_ma"] + self.std_mult * df["bb_std"]
        df["bb_width"] = (2 * self.std_mult * df["bb_std"]) / (df["bb_ma"] + 1e-9)

        # 밴드폭 롤링 퍼센타일 (낮을수록 수축)
        df["bw_rank"] = grp["bb_width"].transform(
            lambda x: x.rolling(self.squeeze_window, min_periods=self.squeeze_window)
                       .rank(pct=True) * 100
        )

        df["prev_bw"]   = grp["bb_width"].shift(1)
        df["prev_rank"] = grp["bw_rank"].shift(1)

        squeeze_release = (
            (df["prev_rank"] <= self.squeeze_rank) &   # 어제 수축 중
            (df["bb_width"]  > df["prev_bw"])           # 오늘 확장 시작
        )

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            squeeze_release &
            (df["close"] > df["bb_upper"]) &            # 상단 돌파 (middle → upper로 강화)
            (df["trading_value"] >= self.min_tv) &
            df["bb_upper"].notna() &
            df["bw_rank"].notna() &
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
