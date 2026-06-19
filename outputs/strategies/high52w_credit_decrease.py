"""
전략 A7 — 52주 신고가 × 신용잔고 감소.

가설: 신고가 돌파 + 신용잔고율 감소 = 레버리지 매도 압력 감소 + 모멘텀 →
      추가 오버행(신용매물) 리스크가 줄어 신고가 지속성 ↑.

데이터 출처:
  stock.db → credit_balance 테이블 (credit_remain_rt 컬럼)
  또는 db/credit/YYYY-MM/YYYYMMDD.csv → whol_loan_rmnd_rate (신용잔고율)
  수집 기간: 2022~ (krx_collector.py / credit_collector 실행 이후)
  ※ 데이터 없는 종목/날짜는 신호에서 제외됨

- 진입 조건:
    1. 종가 > 직전 252 거래일 고가 (52주 신고가)
    2. 신용잔고율 오늘 < lookback일 전 (감소 추세)
    3. 거래대금 ≥ min_trading_value
- 진입가: 다음날 시가
- 청산: holding_days 일 후 종가 (기본 20일)
- 손절: stop_loss_pct 설정 시 (예: -8.0)

권장 테스트 overlay:
  python profit_target_test.py --stop -8
"""

import glob
import os
import sqlite3
import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops


def _load_credit_ratio_db(db_path="./db/stock.db") -> pd.DataFrame:
    """stock.db credit_balance 테이블에서 (code, date, credit_remain_rt) 로드."""
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        con = sqlite3.connect(db_path)
        df = pd.read_sql(
            "SELECT date, ticker AS code, credit_remain_rt FROM credit_balance "
            "WHERE credit_remain_rt IS NOT NULL",
            con,
        )
        con.close()
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["date"] = df["date"].astype(str)
        df = df.rename(columns={"credit_remain_rt": "credit_ratio"})
        return df
    except Exception:
        return pd.DataFrame()


def _load_credit_ratio_csv(base_dir="./db/credit") -> pd.DataFrame:
    """db/credit/YYYY-MM/YYYYMMDD.csv → (code, date, credit_ratio) DataFrame.

    Kiwoom API credit CSV: whol_loan_rmnd_rate 컬럼 사용.
    """
    files = sorted(glob.glob(os.path.join(base_dir, "*", "*.csv")))
    if not files:
        return pd.DataFrame()

    dfs = []
    for f in files:
        date_str = os.path.splitext(os.path.basename(f))[0]
        try:
            tmp = pd.read_csv(f, encoding="utf-8-sig", dtype={"code": str})
        except Exception:
            continue
        if "code" not in tmp.columns:
            continue
        # 신용잔고율 컬럼: whol_loan_rmnd_rate (키움) or credit_remain_rt (KRX)
        rate_col = None
        for c in ("whol_loan_rmnd_rate", "credit_remain_rt"):
            if c in tmp.columns:
                rate_col = c
                break
        if rate_col is None:
            continue
        tmp = tmp[["code", rate_col]].rename(columns={rate_col: "credit_ratio"})
        tmp["code"] = tmp["code"].astype(str).str.zfill(6)
        tmp["date"] = date_str
        dfs.append(tmp)

    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


class High52wCreditDecreaseStrategy(BaseStrategy):
    name = "high52w_credit_decrease"
    timeframe = "daily"

    def __init__(self, lookback_days=252, credit_lookback=5,
                 holding_days=20,
                 min_trading_value=3_000_000_000,
                 stop_loss_pct=None,
                 name=None):
        self.lookback = lookback_days
        self.credit_lookback = credit_lookback   # 신용잔고율 비교 기준 (N일 전)
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

        # 신용잔고율 로드 — stock.db 우선, 없으면 CSV 폴백
        cred = _load_credit_ratio_db()
        if cred.empty:
            cred = _load_credit_ratio_csv()
        if cred.empty:
            print(f"  [{self.name}] 신용잔고 데이터 없음 — stock.db/db/credit/ 확인 필요")
            return []

        df = df.merge(cred[["code", "date", "credit_ratio"]],
                      on=["code", "date"], how="left")

        # 신용잔고율 감소: 오늘 < N일 전
        df["credit_Nd_ago"] = grp["credit_ratio"].shift(self.credit_lookback)
        credit_decrease = (
            df["credit_ratio"].notna() &
            df["credit_Nd_ago"].notna() &
            (df["credit_ratio"] < df["credit_Nd_ago"])
        )

        df["signal"] = (
            high52w_sig &
            credit_decrease &
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
