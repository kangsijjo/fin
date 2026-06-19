"""
전략 A5 — 52주 신고가 × 공매도 잔고 감소.

가설: 신고가 돌파 + 공매도 세력이 포지션을 줄이면 (잔고 비중 감소) 쇼트 스퀴즈
      가능성 ↑ → 단순 신고가 대비 추가 상승 기대.

데이터 출처:
  db/short/YYYY-MM/YYYYMMDD.csv
  컬럼: 티커(=code), 비중(공매도 시총 대비 비율 %)
  수집 기간: krx_collector.py 실행 이후 (일반적으로 2022~ or 2024~)
  ※ 데이터 없는 날짜/종목은 신호에서 제외됨 (안전 우선)

- 진입 조건:
    1. 종가 > 직전 252 거래일 고가 (52주 신고가)
    2. 공매도 비중 오늘 < 5일 전 (비중 감소 추세)
    3. 거래대금 ≥ min_trading_value
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 20일)
- 손절: stop_loss_pct 설정 시 (예: -8.0)

권장 테스트 overlay:
  python profit_target_test.py --stop -8
"""

import glob
import os
import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops


def _load_short_ratio(base_dir="./db/short") -> pd.DataFrame:
    """db/short/YYYY-MM/YYYYMMDD.csv 전부 로드 → (code, date, short_ratio) DataFrame.

    '비중' 컬럼이 없으면 빈 DataFrame 반환 (데이터 없음 시 신호 없음).
    """
    files = sorted(glob.glob(os.path.join(base_dir, "*", "*.csv")))
    if not files:
        return pd.DataFrame(columns=["code", "date", "short_ratio"])

    dfs = []
    for f in files:
        date_str = os.path.splitext(os.path.basename(f))[0]
        try:
            tmp = pd.read_csv(f, encoding="utf-8-sig", dtype={"티커": str})
        except Exception:
            continue
        if "티커" not in tmp.columns or "비중" not in tmp.columns:
            continue
        tmp = tmp[["티커", "비중"]].rename(columns={"티커": "code", "비중": "short_ratio"})
        tmp["code"] = tmp["code"].astype(str).str.zfill(6)
        tmp["date"] = date_str
        dfs.append(tmp)

    if not dfs:
        return pd.DataFrame(columns=["code", "date", "short_ratio"])
    return pd.concat(dfs, ignore_index=True)


class High52wShortDecreaseStrategy(BaseStrategy):
    name = "high52w_short_decrease"
    timeframe = "daily"

    def __init__(self, lookback_days=252, short_lookback=5,
                 holding_days=20,
                 min_trading_value=3_000_000_000,
                 stop_loss_pct=None,
                 name=None):
        self.lookback = lookback_days
        self.short_lookback = short_lookback   # 공매도 비중 비교 기준 (N일 전)
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.stop_loss_pct = stop_loss_pct
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        grp = df.groupby("code")

        # 52주 신고가
        df["prev_high"] = grp["high"].transform(
            lambda x: x.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        )
        high52w_sig = (df["close"] > df["prev_high"]) & df["prev_high"].notna()

        # 공매도 잔고 비중 로드 + 병합
        short_df = _load_short_ratio()
        if short_df.empty:
            # 공매도 데이터 없음 → 신호 없음
            print(f"  [{self.name}] 공매도 데이터 없음 — db/short/ 폴더 확인 필요")
            return []

        df = df.merge(short_df[["code", "date", "short_ratio"]],
                      on=["code", "date"], how="left")

        # 공매도 비중 감소: 오늘 < N일 전
        df["short_ratio_Nd_ago"] = grp["short_ratio"].shift(self.short_lookback)
        short_decrease = (
            df["short_ratio"].notna() &
            df["short_ratio_Nd_ago"].notna() &
            (df["short_ratio"] < df["short_ratio_Nd_ago"])
        )

        df["signal"] = (
            high52w_sig &
            short_decrease &
            (df["trading_value"] >= self.min_tv)
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
