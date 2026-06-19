"""
전략 I — 볼린저밴드 수축 후 돌파 (Squeeze Breakout).

가설: 변동성이 극도로 낮아진 뒤(밴드폭 최소) 가격이 방향성을 갖고 터질 때
      큰 추세가 시작된다 (래리 코너스 Squeeze Momentum).

- 진입 조건:
    1. BB 밴드폭 (bb_width = (upper-lower)/middle) 이 직전 120일 최솟값 근처
       (squeeze_pct_rank ≤ 20 → 하위 20% 이하 수축)
    2. 수축 해소 후 돌파: 어제 bb_width < n일 최소 AND 오늘 bb_width > 어제
    3. 돌파 방향 확인: 종가 > BB 중심선 (상향 돌파만)
    4. 거래대금 ≥ min_trading_value (기본 30억)
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 15일)
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate


class BbSqueezeStrategy(BaseStrategy):
    name = "bb_squeeze"
    timeframe = "daily"

    def __init__(self, bb_period=20, squeeze_window=120, squeeze_rank=20,
                 holding_days=15, min_trading_value=3_000_000_000,
                 use_market_filter=False, name=None):
        self.bb_period = bb_period
        self.squeeze_window = squeeze_window
        self.squeeze_rank = squeeze_rank   # 하위 N% 이하일 때 수축으로 간주
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        grp = df.groupby("code")

        df["bb_ma"]  = grp["close"].transform(
            lambda x: x.rolling(self.bb_period, min_periods=self.bb_period).mean()
        )
        df["bb_std"] = grp["close"].transform(
            lambda x: x.rolling(self.bb_period, min_periods=self.bb_period).std()
        )
        df["bb_width"] = (2 * self.std_mult * df["bb_std"]) / (df["bb_ma"] + 1e-9)

        # 밴드폭의 롤링 퍼센타일 (낮을수록 수축)
        df["bw_rank"] = grp["bb_width"].transform(
            lambda x: x.rolling(self.squeeze_window, min_periods=self.squeeze_window)
                       .rank(pct=True) * 100
        )

        # 수축 해소 시점: 어제까지 수축(하위 20%) + 오늘 밴드폭 확장
        df["prev_bw"]   = grp["bb_width"].shift(1)
        df["prev_rank"] = grp["bw_rank"].shift(1)

        squeeze_release = (
            (df["prev_rank"] <= self.squeeze_rank) &   # 어제 수축 중
            (df["bb_width"]  > df["prev_bw"])          # 오늘 확장 시작
        )

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            squeeze_release &
            (df["close"] > df["bb_ma"]) &             # 상향 돌파
            (df["trading_value"] >= self.min_tv) &
            df["bb_ma"].notna() &
            df["bw_rank"].notna() &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        return _make_trades_for_signals(
            df, holding_days=self.holding_days,
            strategy_name=self.name, costs=costs)

    @property
    def std_mult(self):
        return 2.0
