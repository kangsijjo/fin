"""공용 결측 스캐너 — 수집기들이 '오늘 것'보다 먼저 빈 날짜(결측)를 찾아 채우게 함.

규칙: 모든 일자별 수집기는 수집 전에 recent_missing() 으로 최근 N영업일의
결측을 확인하고, 결측분부터 수집한 뒤 오늘분을 수집한다.
휴장일은 '{date}.csv.holiday' 마커로 표시해 재시도 낭비를 막는다.
"""

import os
from datetime import datetime

import pandas as pd


def month_path(base_dir, date_str):
    return f"{base_dir}/{date_str[:4]}-{date_str[4:6]}/{date_str}.csv"


def has_file(base_dir, date_str, month_subdir=True):
    p = month_path(base_dir, date_str) if month_subdir else f"{base_dir}/{date_str}.csv"
    return os.path.exists(p) or os.path.exists(p + ".holiday")


def mark_holiday(base_dir, date_str, month_subdir=True):
    """휴장일 마커 생성 — 이후 스캔에서 재시도하지 않음."""
    p = month_path(base_dir, date_str) if month_subdir else f"{base_dir}/{date_str}.csv"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p + ".holiday", "w").close()


def recent_missing(base_dir, lookback_bdays=10, month_subdir=True, include_today=True):
    """최근 N영업일 중 파일/마커가 없는 날짜 목록 (과거 → 최신 순)."""
    end = datetime.today()
    days = pd.bdate_range(end=end, periods=lookback_bdays + 1)
    out = []
    for d in days:
        ds = d.strftime("%Y%m%d")
        if not include_today and ds == end.strftime("%Y%m%d"):
            continue
        if not has_file(base_dir, ds, month_subdir):
            out.append(ds)
    return out
