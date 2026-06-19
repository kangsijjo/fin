"""
전략 #4 — 갭매매 (Gap Buy).

가설: 전일 종가 → 당일 시가 갭이 0.5~3.0% 이면 시가 매수, 종가 매도.
당일 청산 = 보유 1일.

거대한 갭(>3%)은 호재 소진 위험. 음수 갭은 약세 신호로 제외.
"""

import pandas as pd
from .base import BaseStrategy, StrategyTrade


class GapBuyStrategy(BaseStrategy):
    name = "gap_buy"
    timeframe = "daily"

    def __init__(self, gap_min_pct=0.5, gap_max_pct=3.0,
                 min_trading_value=1_000_000_000,
                 universe_top_n=None,
                 name=None):
        self.gap_min = gap_min_pct
        self.gap_max = gap_max_pct
        self.min_tv = min_trading_value
        self.top_n = universe_top_n
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        # 전일 종가 + 갭 계산 (벡터화)
        df["prev_close"] = df.groupby("code")["close"].shift(1)
        df["gap_pct"] = (df["open"] / df["prev_close"] - 1) * 100

        # 유동성 필터 (전일 거래대금)
        df["prev_tv"] = df.groupby("code")["trading_value"].shift(1)

        # 진입 시그널
        signal = (
            (df["gap_pct"] >= self.gap_min) &
            (df["gap_pct"] <= self.gap_max) &
            (df["prev_tv"] >= self.min_tv) &
            df["prev_close"].notna() &
            (df["open"] > 0) &
            (df["close"] > 0)
        )
        sig = df[signal].copy()

        # 거래대금 상위 N 제한 (날짜별)
        if self.top_n is not None and not sig.empty:
            sig = (sig.sort_values(["date", "prev_tv"], ascending=[True, False])
                      .groupby("date").head(self.top_n))

        # 수익률 계산 (시가→종가)
        sig["gross_pct"] = (sig["close"] / sig["open"] - 1) * 100
        sig["cost_pct"] = costs["total_pct"]
        sig["net_pct"] = sig["gross_pct"] - sig["cost_pct"]
        sig["exit_reason"] = "eod"   # end-of-day

        # StrategyTrade 리스트로
        trades = [
            StrategyTrade(
                strategy=self.name,
                code=r["code"],
                entry_date=r["date"],
                entry_price=float(r["open"]),
                exit_date=r["date"],
                exit_price=float(r["close"]),
                holding_days=0,
                gross_pct=float(r["gross_pct"]),
                cost_pct=float(r["cost_pct"]),
                net_pct=float(r["net_pct"]),
                exit_reason="eod",
            )
            for _, r in sig.iterrows()
        ]
        return trades
