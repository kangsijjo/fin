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


def _default_stock_db():
    """실제 stock.db 경로. [2026-10-12] 종전 기본값 './db/stock.db' 는 outputs/db 의 **0바이트 빈 파일**
    (06-17 생성)이라 쿼리가 늘 실패 → CSV 폴백(2026-06~ 뿐) → 이 전략이 8년 백테스트에서 0건이었다.
    실데이터는 Stock_AI_Project/data/stock.db (credit_balance 2022-01~, 296만 행)."""
    cands = []
    try:
        from fin_paths import STOCK_DB          # FIN_ROOT 기반 단일 출처
        cands.append(str(STOCK_DB))
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands += [os.path.join(here, "..", "..", "Stock_AI_Project", "data", "stock.db"),
              "./db/stock.db"]
    for p in cands:
        if os.path.exists(p) and os.path.getsize(p) > 0:   # 0바이트 빈 파일은 건너뛴다
            return p
    return cands[-1]


def _load_credit_ratio_db(db_path=None) -> pd.DataFrame:
    """stock.db credit_balance 테이블에서 (code, date, credit_remain_rt) 로드."""
    db_path = db_path or _default_stock_db()
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return pd.DataFrame()
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)   # 읽기 전용(수집기와 경합 방지)
        df = pd.read_sql(
            "SELECT date, ticker AS code, credit_remain_rt FROM credit_balance "
            "WHERE credit_remain_rt IS NOT NULL",
            con,
        )
        con.close()
        df["code"] = df["code"].astype(str).str.zfill(6)
        # [2026-10-12] DB 는 '2022-01-03'(대시형), 일봉은 '20220103' — 그대로 병합하면 한 건도 안 맞는다.
        df["date"] = df["date"].astype(str).str.replace("-", "", regex=False).str[:8]
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
                 data_lag=0, lag_mode="data",
                 name=None):
        """        data_lag : [2026-10-23] 보조데이터 공개 지연(거래일). 실매매에선 그날 값이 없다(신용 1~2일·공매도 다음날 공표 후 20:00 수집).
        lag_mode : "data"  = 신호일 t 에 **t−data_lag 일 값**(그때 공개된 최신값)으로 판정 → 다음날 시가 매수(가격 조건은 t 그대로)
                   "entry" = 판정은 원래대로(그날 값) 하되 **공개될 때까지 기다려** t+data_lag 일에 신호 → 그다음날 시가 매수
                   둘 다 실매매와 같은 정보 제약 — 백테스트가 미래 정보를 쓰지 않는다.
        """
        self.lookback = lookback_days
        self.credit_lookback = credit_lookback   # 신용잔고율 비교 기준 (N일 전)
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.stop_loss_pct = stop_loss_pct
        self.data_lag = int(data_lag or 0)
        self.lag_mode = lag_mode
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
        # merge 후 df 를 다시 그룹핑 — 옛 grp(108줄, 병합 전)엔 credit_ratio 가 없어
        # 'Column not found: credit_ratio' 예외로 전략이 통째 실패했음(2026-06-30 수정).
        if self.data_lag and self.lag_mode == "data":      # t 일엔 t−lag 일 값까지만 공개돼 있다
            df["credit_ratio"] = df.groupby("code")["credit_ratio"].shift(self.data_lag)
        df["credit_Nd_ago"] = df.groupby("code")["credit_ratio"].shift(self.credit_lookback)
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
        if self.data_lag and self.lag_mode == "entry":     # 공개를 기다려 lag 일 뒤에 신호(→ 그다음날 매수)
            df["signal"] = df.groupby("code")["signal"].shift(self.data_lag).fillna(False).astype(bool)

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
