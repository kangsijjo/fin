"""
전략 A5 — 52주 신고가 × 공매도 잔고 감소.

가설: 신고가 돌파 + 공매도 세력이 포지션을 줄이면 (잔고 비중 감소) 쇼트 스퀴즈
      가능성 ↑ → 단순 신고가 대비 추가 상승 기대.

데이터 출처 [2026-10-12 변경 — 사용자 결정]:
  macro_data/shortfund_kosdaq/YYYYMMDD.csv 의 short_ratio = **코스닥 공매도 '거래' 비중(%)**
  (pykrx get_shorting_volume_by_ticker, 2021-01~, backfill_shorting_fundamental.py 수집 ·
   run_recheck.bat 20:00 에 최근 30거래일 자동 보충)
  ※ 종전 출처 db/short/ 는 이 전략에서 한 번도 쓸 수 없었다(8년 백테스트 0건):
     ① krx_collector 가 pykrx 기본값 **코스피**만 받아 코스닥 유니버스와 종목이 0개 겹침
     ② '잔고' 엔드포인트가 06-02 부터 실패해 95일 중 90일이 '거래 비중'으로 조용히 폴백(지표 혼재)
     ③ 2026-06 이후 데이터뿐. → 잔고 비중 과거치는 backfill_short_balance.py --probe 로 별도 확인.
  ⚠ 의미 변화: '공매도 잔고 감소'가 아니라 '공매도 거래 비중 감소'다. §34 에선 거래 비중이 높은
     종목이 이후 더 올랐다(IC +0.096) — '감소' 조건이 불리할 수 있고, 그건 백테스트가 판정한다.
  ※ 데이터 없는 날짜/종목은 신호에서 제외됨 (안전 우선)

- 진입 조건:
    1. 종가 > 직전 252 거래일 고가 (52주 신고가)
    2. 공매도 거래 비중 오늘 < 5일 전 (비중 감소 추세)
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


_SHORTFUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "macro_data", "shortfund_kosdaq")


def _load_short_ratio(base_dir=None) -> pd.DataFrame:
    """macro_data/shortfund_kosdaq/YYYYMMDD.csv 전부 로드 → (code, date, short_ratio) DataFrame.

    short_ratio = 코스닥 공매도 거래 비중(%). 파일/컬럼이 없으면 빈 DataFrame (신호 없음).
    """
    base_dir = base_dir or _SHORTFUND_DIR
    files = sorted(glob.glob(os.path.join(base_dir, "*.csv")))
    if not files:
        return pd.DataFrame(columns=["code", "date", "short_ratio"])

    dfs = []
    for f in files:
        date_str = os.path.splitext(os.path.basename(f))[0]
        if not date_str.isdigit():
            continue
        try:
            tmp = pd.read_csv(f, encoding="utf-8-sig", dtype={"code": str},
                              usecols=lambda c: c in ("code", "short_ratio"))
        except Exception:
            continue
        if "code" not in tmp.columns or "short_ratio" not in tmp.columns:
            continue
        tmp["code"] = tmp["code"].astype(str).str.zfill(6)
        tmp["short_ratio"] = pd.to_numeric(tmp["short_ratio"], errors="coerce")
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
            print(f"  [{self.name}] 공매도 데이터 없음 — macro_data/shortfund_kosdaq/ 확인 "
                  f"(python backfill_shorting_fundamental.py --market KOSDAQ --start 20210101)")
            return []

        df = df.merge(short_df[["code", "date", "short_ratio"]],
                      on=["code", "date"], how="left")

        # 공매도 비중 감소: 오늘 < N일 전
        # merge 후 df 를 다시 그룹핑 — 옛 grp(79줄, 병합 전)엔 short_ratio 가 없어
        # 'Column not found: short_ratio' 예외로 전략이 통째 실패했음(2026-06-30 수정).
        df["short_ratio_Nd_ago"] = df.groupby("code")["short_ratio"].shift(self.short_lookback)
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
