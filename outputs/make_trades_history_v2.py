"""
trades_history.csv v2 — 종목별 feature 추가.

기존 5개 매크로 feature (KOSDAQ 지수, 외인/기관) +
종목별 7개 feature:
  rsi14       — Wilder RSI (14일)
  atr_pct     — ATR / close × 100 (14일)
  vol_ratio   — today_volume / mean(volume[T-20..T-1])
  tv_ratio    — today_trading_value / mean(value[T-20..T-1])
  for_5d      — foreign_net 5일 합 / market_cap × 1000 (정규화)
  ins_5d      — inst_net 5일 합 / market_cap × 1000
  mcap_class  — 시가총액 분류 (1=대, 2=중, 3=소)

→ stock_features.csv 저장 + trades_history.csv 와 merge 한 v2 저장.
"""

import os
import csv
import pandas as pd
import numpy as np

from strategies.daily_loader import load_macro_daily, default_costs, filter_universe
from strategies.high_with_filters import HighWithFiltersStrategy


def compute_stock_features(df):
    """전체 종목 일별 feature 계산. 효율성을 위해 vectorized."""
    print("[stock_features] 계산 시작...")
    df = df.sort_values(["code", "date"]).copy()

    # RSI14 (Wilder)
    delta = df.groupby("code")["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.groupby(df["code"]).ewm(alpha=1/14, adjust=False, min_periods=14).mean().reset_index(0, drop=True)
    avg_loss = loss.groupby(df["code"]).ewm(alpha=1/14, adjust=False, min_periods=14).mean().reset_index(0, drop=True)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    # ATR14 (% of close)
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df.groupby("code")["close"].shift(1)).abs()
    low_close = (df["low"] - df.groupby("code")["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr14"] = tr.groupby(df["code"]).ewm(alpha=1/14, adjust=False, min_periods=14).mean().reset_index(0, drop=True)
    df["atr_pct"] = (df["atr14"] / df["close"]) * 100

    # 거래량 비율 (today / 20일 평균, signal 시점 직전까지)
    # [fix] 이전엔 rolling 후 전역 shift(1) → 종목 첫 행에 직전 종목 값이 누입됐음.
    #       shift 를 그룹 내부에서 수행하도록 transform 으로 교체.
    vol_ma20 = df.groupby("code")["volume"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=20).mean())
    df["vol_ratio"] = df["volume"] / vol_ma20

    # 거래대금 비율
    tv_ma20 = df.groupby("code")["trading_value"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=20).mean())
    df["tv_ratio"] = df["trading_value"] / tv_ma20

    # 외인/기관 5일 누적 (현재 행 포함, 시가총액으로 정규화)
    df["for_5d_sum"] = df.groupby("code")["foreign_net"].rolling(5, min_periods=5).sum().reset_index(0, drop=True)
    df["ins_5d_sum"] = df.groupby("code")["inst_net"].rolling(5, min_periods=5).sum().reset_index(0, drop=True)
    df["for_5d"] = df["for_5d_sum"] / df["market_cap"].replace(0, np.nan) * 1000
    df["ins_5d"] = df["ins_5d_sum"] / df["market_cap"].replace(0, np.nan) * 1000

    # 시가총액 분류 (1=대형, 2=중형, 3=소형)
    df["mcap_class"] = 3
    df.loc[df["market_cap"] >= 1_000_000_000_000, "mcap_class"] = 2  # 1조 이상 = 중형
    df.loc[df["market_cap"] >= 10_000_000_000_000, "mcap_class"] = 1  # 10조 이상 = 대형

    feat_cols = ["code", "date", "rsi14", "atr_pct", "vol_ratio", "tv_ratio",
                 "for_5d", "ins_5d", "mcap_class"]
    out = df[feat_cols].copy()
    print(f"[stock_features] {len(out):,} 행 계산 완료")
    return out


def main():
    print("=== trades_history v2 (종목별 feature 추가) ===\n")
    df = load_macro_daily()
    df = filter_universe(df)
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)
    costs = default_costs()

    strat = HighWithFiltersStrategy(
        lookback_days=500, holding_days=40,
        use_market_filter=True, use_volume_filter=False,
        name="h500_40_MKT",
    )
    trades = strat.backtest(df, costs)
    print(f"[backtest] {len(trades)} 매매")

    # 종목별 feature 계산
    stock_feat = compute_stock_features(df)
    stock_feat["date"] = stock_feat["date"].astype(str)
    stock_feat["code"] = stock_feat["code"].astype(str).str.zfill(6)

    # signal_date 계산 (entry_date 직전 영업일)
    code_dates = {}
    for code, g in df.groupby("code"):
        code_dates[code] = sorted(g["date"].tolist())

    def signal_date_of(code, entry_date):
        dates = code_dates.get(code, [])
        try:
            idx = dates.index(entry_date)
            if idx > 0:
                return dates[idx - 1]
        except ValueError:
            pass
        return entry_date

    rows = []
    sf_map = stock_feat.set_index(["code", "date"]).to_dict("index")
    for t in trades:
        sig_date = signal_date_of(t.code, t.entry_date)
        sf = sf_map.get((t.code, sig_date), {})
        rows.append({
            "date": sig_date,
            "code": t.code,
            "entry_date": t.entry_date,
            "exit_date": t.exit_date,
            "yield_pct": round(t.net_pct, 4),
            "exit_reason": t.exit_reason,
            "rsi14": round(sf.get("rsi14"), 2) if sf.get("rsi14") is not None and not pd.isna(sf.get("rsi14")) else None,
            "atr_pct": round(sf.get("atr_pct"), 3) if sf.get("atr_pct") is not None and not pd.isna(sf.get("atr_pct")) else None,
            "vol_ratio": round(sf.get("vol_ratio"), 3) if sf.get("vol_ratio") is not None and not pd.isna(sf.get("vol_ratio")) else None,
            "tv_ratio": round(sf.get("tv_ratio"), 3) if sf.get("tv_ratio") is not None and not pd.isna(sf.get("tv_ratio")) else None,
            "for_5d": round(sf.get("for_5d"), 4) if sf.get("for_5d") is not None and not pd.isna(sf.get("for_5d")) else None,
            "ins_5d": round(sf.get("ins_5d"), 4) if sf.get("ins_5d") is not None and not pd.isna(sf.get("ins_5d")) else None,
            "mcap_class": sf.get("mcap_class", 3),
        })

    out = "./trades_history_v2.csv"
    cols = ["date","code","entry_date","exit_date","yield_pct","exit_reason",
            "rsi14","atr_pct","vol_ratio","tv_ratio","for_5d","ins_5d","mcap_class"]
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"[saved] {out}, {len(rows)} 행")

    # feature 채움 통계
    n_with_rsi = sum(1 for r in rows if r["rsi14"] is not None)
    n_with_vol = sum(1 for r in rows if r["vol_ratio"] is not None)
    print(f"  rsi14 있음:    {n_with_rsi}/{len(rows)}")
    print(f"  vol_ratio 있음: {n_with_vol}/{len(rows)}")


if __name__ == "__main__":
    main()
