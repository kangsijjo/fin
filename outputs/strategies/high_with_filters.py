"""
전략 #11+ — 추세 (high_lookback) + 시장 컨텍스트 + 거래량 필터.

시장 컨텍스트: 그날 시장 전체 평균 등락률의 60일 이평선 > 0 (강세장)
거래량 필터: 신호 발생일 거래대금 자기 평균의 1.5배 (관심 종목)

기대: high_500d_h40 의 +10.54% 가 +12~15% 로 강화. 표본 감소 trade-off.
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops


class HighWithFiltersStrategy(BaseStrategy):
    name = "high_filtered"
    timeframe = "daily"

    def __init__(self, lookback_days=500, holding_days=40,
                 use_market_filter=True, market_ma_days=60,
                 use_volume_filter=True, vol_mult=1.5,
                 min_trading_value=3_000_000_000,
                 stop_loss_pct=None, trailing_peak_pct=None,
                 trailing_activate_pct=10.0,
                 time_check_days=None, time_check_min_pct=None,
                 time_stop_pct=None,
                 entry_at_close=False, slippage_pct=0.0,
                 gap_filter_pct=None,
                 exit_slippage_pct=0.0,
                 name=None):
        """
        min_trading_value 권장값 (자본 규모별):
          - 1억 이하 베팅:  3_000_000_000 (30억, 현재 기본값)
          - 1~5억 베팅:    5_000_000_000 (50억, 전문가 권장)
          - 5억 이상 베팅: 10_000_000_000 (100억)
        """
        self.lookback = lookback_days
        self.holding = holding_days
        self.use_mkt = use_market_filter
        self.mkt_ma = market_ma_days
        self.use_vol = use_volume_filter
        self.vol_mult = vol_mult
        self.min_tv = min_trading_value
        self.stop_loss_pct = stop_loss_pct
        self.trailing_peak_pct = trailing_peak_pct
        self.trailing_activate_pct = trailing_activate_pct
        self.time_check_days = time_check_days
        self.time_check_min_pct = time_check_min_pct
        self.time_stop_pct = time_stop_pct
        self.entry_at_close = entry_at_close
        self.slippage_pct = slippage_pct
        self.gap_filter_pct = gap_filter_pct
        self.exit_slippage_pct = exit_slippage_pct
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        # 신고가 신호 — transform 사용으로 그룹 경계 유지 (버그 수정 2026-06-18)
        df["prev_high"] = df.groupby("code")["high"].transform(
            lambda x: x.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        )
        sig = df["close"] > df["prev_high"]

        # 시장 강세 게이트 (그날 시장 평균 change_pct 의 60일 이평)
        if self.use_mkt:
            mkt = df.groupby("date")["change_pct"].mean()
            mkt_ma = mkt.rolling(self.mkt_ma, min_periods=self.mkt_ma).mean()
            mkt_dict = (mkt_ma > 0).to_dict()   # merge 대신 map — 신 pandas 호환
            df["mkt_strong"] = df["date"].map(mkt_dict).fillna(False)
            sig = sig & df["mkt_strong"]

        # 거래량 필터 (신호 발생일 거래대금이 자기 30일 평균의 vol_mult 배)
        if self.use_vol:
            df["tv_mean_30"] = (df.groupby("code")["trading_value"]
                                  .rolling(30, min_periods=30).mean()
                                  .reset_index(0, drop=True))
            sig = sig & (df["trading_value"] >= df["tv_mean_30"] * self.vol_mult)

        # 유동성
        sig = sig & (df["trading_value"] >= self.min_tv)

        df["signal"] = sig

        # 손절/trailing/time_stop 옵션 있으면 stop-aware 경로, 아니면 기존 벡터화 경로
        use_stops = (self.stop_loss_pct is not None or self.trailing_peak_pct is not None
                     or self.time_check_days is not None)
        if use_stops:
            return _make_trades_with_stops(
                df, holding_days=self.holding,
                strategy_name=self.name, costs=costs,
                stop_loss_pct=self.stop_loss_pct,
                trailing_peak_pct=self.trailing_peak_pct,
                trailing_activate_pct=self.trailing_activate_pct,
                time_check_days=self.time_check_days,
                time_check_min_pct=self.time_check_min_pct,
                time_stop_pct=self.time_stop_pct,  # [fix] 이전엔 전달 누락 → 항상 무시됐음
            )
        # 애프터마켓 매수 옵션 (entry_at_close=True) → 같은 날 close, entry_lag=0
        entry_col = "close" if self.entry_at_close else "open"
        entry_lag = 0 if self.entry_at_close else 1
        return _make_trades_for_signals(
            df, holding_days=self.holding,
            strategy_name=self.name, costs=costs,
            entry_col=entry_col, entry_lag=entry_lag,
            slippage_pct=self.slippage_pct,
            gap_filter_pct=self.gap_filter_pct,
            exit_slippage_pct=self.exit_slippage_pct)
