"""
전략 — 외국인 지분율(보유비중) 상승 × 가격 확인(신고가).  (2026-07-21 신설)

가설: 외국인 '순매수 플로우'(하루치, 노이즈 큼) 대신 '지분율(보유비중) 상승'(누적
      매집·확신 반영, 느리지만 안정적) + 신고가 돌파로 가격까지 확인하면 헛신호가
      줄어든다. 기존 외국인 순매수 3전략(h52w_for3d_mkt 등)의 대안·비교군.

- 진입 조건:
    1. 외국인 지분율이 직전 ratio_n 거래일 대비 ratio_delta_min(%p) 이상 상승
    2. 종가 > 직전 lookback_days 고가 (신고가 = 가격 확인)
    3. 거래대금 ≥ min_trading_value
    (옵션) 시장강세 게이트
- 진입가: 다음날 시가 / 청산: holding_days 일 후 종가 / 손절: stop_loss_pct 설정 시

주의: df 에 'foreign_holding_ratio' 컬럼(외국인 지분율 %, foreign_ratio 테이블 merge)이
      있어야 한다. 없으면(백필 전) 신호 0건으로 안전 폴백(조용한 크래시 대신).

단점(고지): 지분율은 하루 0.0X%씩 느린 신호라 진입이 순매수 방식보다 늦을 수 있고,
      외국인 지분율이 높은 대형주로 후보가 쏠릴 수 있음 → baseline A/B 비교로 검증 필요.
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops, _add_market_gate

RATIO_COL = "foreign_holding_ratio"


class ForeignRatioHighStrategy(BaseStrategy):
    name = "foreign_ratio_high"
    timeframe = "daily"

    def __init__(self, ratio_n=5, ratio_delta_min=0.0,
                 lookback_days=20, holding_days=20,
                 min_trading_value=1_000_000_000,
                 use_market_filter=False,
                 stop_loss_pct=None,
                 name=None):
        self.ratio_n = ratio_n                    # 지분율 변화 관찰 창(거래일)
        self.ratio_delta_min = ratio_delta_min    # 최소 상승폭(%p). 0 = 그냥 상승
        self.lookback = lookback_days             # 신고가 창(가격 확인)
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        self.stop_loss_pct = stop_loss_pct
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        # 지분율 컬럼이 없으면(백필 전) 신호 0건으로 안전 폴백 — 조용한 크래시 방지.
        if RATIO_COL not in df.columns:
            df["signal"] = False
            return _make_trades_for_signals(
                df, holding_days=self.holding_days,
                strategy_name=self.name, costs=costs)

        grp = df.groupby("code")

        # 신고가(전일까지 lookback 고가 대비 오늘 종가) — 가격 확인
        df["prev_high"] = grp["high"].transform(
            lambda x: x.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        )

        # 외국인 지분율 상승분(오늘 지분율 − ratio_n 거래일 전 지분율)
        df["ratio_chg"] = grp[RATIO_COL].transform(
            lambda x: x - x.shift(self.ratio_n)
        )
        ratio_rising = df["ratio_chg"] >= self.ratio_delta_min

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            (df["close"] > df["prev_high"]) &
            ratio_rising &
            df["ratio_chg"].notna() &
            df[RATIO_COL].notna() &
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
