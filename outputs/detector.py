"""
박스권 / 거래량 급증 신호 감지 모듈

룰 v2:
  - 박스: 직전 5분간 가격이 (현재가-5분전가) 기준 ±2% 이내
  - 박스 폭(고가-저가) / 기준가 가 0.8% ~ 4.0% 사이 (너무 좁거나 너무 넓으면 제외)
  - 거래량 급증: 직전 3분 평균 거래량의 2배 이상이 1분 안에 체결
"""

import pandas as pd
import numpy as np
import config


def detect_boxes(df, window_min=None, max_pct=None, min_pct=None):
    cfg = config.RULE_V2
    window = window_min or cfg["box_window_minutes"]
    max_p = max_pct or cfg["box_max_pct"]
    min_p = min_pct or cfg["box_min_pct"]

    df = df.copy()

    # [수정된 부분] shift(1)을 적용하여 현재 캔들을 미래참조하지 않고 과거 박스권만 측정
    df["box_high"] = df["high"].shift(1).rolling(window=window, min_periods=window).max()
    df["box_low"] = df["low"].shift(1).rolling(window=window, min_periods=window).min()
    df["box_ref"] = df["close"].shift(window + 1)  # 박스 시작점의 직전 종가 (기준가)

    df["box_width_pct"] = (df["box_high"] - df["box_low"]) / df["box_ref"] * 100

    dev_high = (df["box_high"] - df["box_ref"]).abs() / df["box_ref"] * 100
    dev_low = (df["box_low"] - df["box_ref"]).abs() / df["box_ref"] * 100
    df["box_range_pct"] = np.maximum(dev_high, dev_low)

    # is_box가 True라는 건 '직전 봉까지 박스가 예쁘게 유지되었다'는 뜻
    df["is_box"] = (
        (df["box_range_pct"] <= max_p) &
        (df["box_width_pct"] >= min_p) &
        df["box_ref"].notna()
    )
    return df


def detect_volume_surge(df, baseline_min=None, multiplier=None):
    cfg = config.RULE_V2
    base = baseline_min or cfg["volume_baseline_minutes"]
    mult = multiplier or cfg["volume_surge_multiplier"]

    df = df.copy()

    df["vol_baseline"] = (
        df["volume"].shift(1).rolling(window=base, min_periods=base).mean()
    )
    df["vol_ratio"] = df["volume"] / df["vol_baseline"]
    df["is_volume_surge"] = (
        (df["vol_ratio"] >= mult) & df["vol_baseline"].notna() & (df["vol_baseline"] > 0)
    )
    return df


def annotate_signals(df):
    df = detect_boxes(df)
    df = detect_volume_surge(df)
    return df


if __name__ == "__main__":
    import pandas as pd

    times = pd.date_range("2026-05-15 09:00", periods=30, freq="1min")
    prices = (
        [1000, 1005, 1010, 1015, 1008, 1012, 1005, 1010, 1015, 1010] + 
        [1020, 1025, 1030, 1028, 1035] + 
        [1040, 1042, 1045, 1043, 1048] +
        [1050, 1052, 1055, 1053, 1058] +
        [1060, 1058, 1055, 1052, 1050]
    )
    volumes = (
        [1000] * 10 + 
        [5000, 3000, 2500, 2000, 1500] + 
        [1500] * 15
    )
    df = pd.DataFrame({
        "datetime": times,
        "open": prices,
        "high": [p + 2 for p in prices],
        "low": [p - 2 for p in prices],
        "close": prices,
        "volume": volumes,
    })

    annotated = annotate_signals(df)
    print(annotated[[
        "datetime", "close", "volume",
        "is_box", "box_width_pct", "box_range_pct",
        "vol_ratio", "is_volume_surge",
    ]].to_string(index=False))