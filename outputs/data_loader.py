"""
저장된 CSV 데이터를 읽어 백테스트에 쓰기 좋은 형태(pandas DataFrame)로 변환.

- ranking CSV:  data/rankings/YYYYMMDD.csv
- 분봉 CSV:     db/minute/YYYY-MM/YYYYMMDD/CODE.csv
- investor CSV: db/investor/YYYY-MM/YYYYMMDD.csv  (일자별 종목별 외인/기관/개인)
- daily CSV:    db/daily/YYYY-MM/YYYYMMDD.csv     (수집일 기준 종목별 60일 일봉)
"""

import os
import glob
from datetime import datetime
import pandas as pd

import config


def list_available_dates():
    """수집된 데이터가 있는 날짜 목록 (분봉 기준).

    db/minute/YYYY-MM/YYYYMMDD/ 디렉토리들의 마지막 세그먼트(YYYYMMDD)를 수집.
    """
    dates = set()
    for d in glob.glob(f"{config.DB_MINUTE_DIR}/*/*"):
        if os.path.isdir(d):
            name = os.path.basename(d)
            if len(name) == 8 and name.isdigit():
                dates.add(name)
    return sorted(dates)


def load_ranking(date_str):
    """특정 날짜의 거래대금 상위 종목 로드"""
    path = f"{config.RANKING_DIR}/{date_str}.csv"
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, encoding="utf-8-sig")


def load_minute_bars(stock_code, date_str):
    """
    특정 종목, 특정 날짜의 분봉 DataFrame 로드.

    Returns: DataFrame with columns
      [datetime, open, high, low, close, volume]
      datetime은 pandas Timestamp. 시간 오름차순 정렬.
    또는 None (파일 없음).

    경로: db/minute/{YYYY-MM}/{YYYYMMDD}/{CODE}.csv
    """
    yyyy_mm = f"{date_str[:4]}-{date_str[4:6]}"
    path = f"{config.DB_MINUTE_DIR}/{yyyy_mm}/{date_str}/{stock_code}.csv"
    if not os.path.exists(path):
        return None

    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"date": str, "time": str})
    if df.empty:
        return None

    df["datetime"] = pd.to_datetime(
        df["date"] + df["time"].str.zfill(6),
        format="%Y%m%d%H%M%S",
    )
    df = df[["datetime", "open", "high", "low", "close", "volume"]]
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def load_all_for_date(date_str):
    """
    특정 날짜의 모든 종목 분봉을 dict로 로드.

    Returns: {stock_code: DataFrame, ...}
    """
    ranking = load_ranking(date_str)
    if ranking is None:
        return {}

    result = {}
    for _, row in ranking.iterrows():
        code = str(row["code"]).zfill(6)
        df = load_minute_bars(code, date_str)
        if df is not None and len(df) > 0:
            result[code] = df
    return result


def get_stock_name_map(date_str):
    """그날 ranking의 {code: name} 매핑"""
    ranking = load_ranking(date_str)
    if ranking is None:
        return {}
    return {
        str(row["code"]).zfill(6): row["name"]
        for _, row in ranking.iterrows()
    }


# ============================================================
# investor / daily 로더 (외인/기관/개인 + 일봉)
# ============================================================

def _investor_path(date_str):
    yyyy_mm = f"{date_str[:4]}-{date_str[4:6]}"
    return f"{config.DB_INVESTOR_DIR}/{yyyy_mm}/{date_str}.csv"


def _daily_path(date_str):
    yyyy_mm = f"{date_str[:4]}-{date_str[4:6]}"
    return f"{config.DB_DAILY_DIR}/{yyyy_mm}/{date_str}.csv"


def load_investor_for_date(date_str):
    """
    특정 날짜의 외인/기관/개인 매매 데이터 로드.

    Returns: {stock_code: row_dict} 또는 빈 dict (파일 없음)
      row_dict 주요 키:
        foreign_net_qty, foreign_net_value,
        institution_net_qty, institution_net_value,
        personal_net_qty, personal_net_value,
        current_price, change_pct, trading_value
    """
    path = _investor_path(date_str)
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"code": str})
    if df.empty:
        return {}
    result = {}
    for _, row in df.iterrows():
        code = str(row["code"]).zfill(6)
        result[code] = row.to_dict()
    return result


def load_daily_for_date(date_str, lookback=None):
    """
    특정 수집일의 종목별 일봉 60일치 로드.

    Returns: {stock_code: DataFrame} 또는 빈 dict (파일 없음)
      DataFrame 컬럼: [bar_date, open, high, low, close, volume, trading_value, change_pct]
      bar_date 오름차순 정렬.

    lookback=N 이면 종목별 최근 N개 행만 반환 (메모리/속도 절약).
    """
    path = _daily_path(date_str)
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path, encoding="utf-8-sig",
                     dtype={"code": str, "bar_date": str})
    if df.empty:
        return {}
    df = df.sort_values(["code", "bar_date"]).reset_index(drop=True)
    cols = ["bar_date", "open", "high", "low", "close",
            "volume", "trading_value", "change_pct"]
    result = {}
    for code, sub in df.groupby("code"):
        code = str(code).zfill(6)
        s = sub[cols].reset_index(drop=True)
        if lookback is not None and len(s) > lookback:
            s = s.tail(lookback).reset_index(drop=True)
        result[code] = s
    return result


def list_investor_dates():
    """db/investor 에 수집된 모든 날짜 목록 (정렬)."""
    dates = set()
    for f in glob.glob(f"{config.DB_INVESTOR_DIR}/*/*.csv"):
        name = os.path.basename(f).rsplit(".", 1)[0]
        if len(name) == 8 and name.isdigit():
            dates.add(name)
    return sorted(dates)


def previous_trading_day(date_str):
    """
    date_str 직전 영업일 반환 (db/investor 기준).

    Returns: YYYYMMDD 문자열 또는 None (이전 데이터 없음).

    주의: 달력상 D-1 이 아니라 "데이터가 있는 직전 날짜". 주말/공휴일
    및 수집 누락일은 자동으로 건너뜀.
    """
    dates = list_investor_dates()
    prev = [d for d in dates if d < date_str]
    return prev[-1] if prev else None


if __name__ == "__main__":
    dates = list_available_dates()
    print(f"수집된 날짜 {len(dates)}개: {dates[:5]}{'...' if len(dates) > 5 else ''}")

    if dates:
        d = dates[-1]
        print(f"\n[{d}] ranking 확인:")
        r = load_ranking(d)
        if r is not None:
            print(r.head())

        print(f"\n[{d}] 모든 종목 분봉 로드:")
        bars = load_all_for_date(d)
        print(f"  {len(bars)}개 종목 로드 완료")
        if bars:
            code = list(bars.keys())[0]
            print(f"\n  [{code}] 분봉 샘플:")
            print(bars[code].head())
            print(f"  총 {len(bars[code])}개 분봉")
