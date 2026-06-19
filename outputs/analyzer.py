"""
백테스트 결과 분석.

- 모드별 통계 비교
- 매매 내역 CSV 저장
- 핵심 지표: 매매 건수, 승률, 평균 익절/손절폭, 손익비, 누적 수익률, MDD, 종료 사유 분포
"""

import os
import pandas as pd
import numpy as np
from dataclasses import asdict

import config


def trades_to_dataframe(trades):
    """Trade 리스트를 DataFrame으로 변환"""
    if not trades:
        return pd.DataFrame()
    rows = []
    for t in trades:
        d = asdict(t)
        rows.append(d)
    df = pd.DataFrame(rows)
    return df


def summarize(trades, label=""):
    """
    Trade 리스트의 핵심 통계를 dict로 반환.
    """
    if not trades:
        return {
            "label": label,
            "n_trades": 0,
            "win_rate_pct": 0.0,
            "avg_net_pct": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "profit_factor": 0.0,
            "cum_pct": 0.0,
            "mdd_pct": 0.0,
            "avg_holding_min": 0.0,
        }

    df = trades_to_dataframe(trades)
    n = len(df)
    wins = df[df["net_pct"] > 0]
    losses = df[df["net_pct"] <= 0]

    win_rate = len(wins) / n * 100 if n else 0
    avg_net = df["net_pct"].mean()
    avg_win = wins["net_pct"].mean() if len(wins) else 0
    avg_loss = losses["net_pct"].mean() if len(losses) else 0

    # 손익비 (Profit Factor): 총 이익 / 총 손실의 절댓값
    total_win = wins["net_pct"].sum()
    total_loss = abs(losses["net_pct"].sum())
    pf = total_win / total_loss if total_loss > 0 else float("inf")

    # 누적 수익률 (단순 합산 — 자본 단위가 아니라 매매당 % 의 합)
    cum_pct = df["net_pct"].sum()

    # MDD: 매매 순서별 누적 수익률의 최대 낙폭
    df_sorted = df.sort_values("entry_time").reset_index(drop=True)
    cum = df_sorted["net_pct"].cumsum()
    peak = cum.cummax()
    drawdown = cum - peak
    mdd = drawdown.min()

    return {
        "label": label,
        "n_trades": n,
        "win_rate_pct": round(win_rate, 2),
        "avg_net_pct": round(avg_net, 3),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "cum_pct": round(cum_pct, 2),
        "mdd_pct": round(mdd, 2),
        "avg_holding_min": round(df["holding_minutes"].mean(), 1),
    }


def exit_reason_breakdown(trades):
    """종료 사유별 건수/평균 수익률"""
    if not trades:
        return pd.DataFrame()
    df = trades_to_dataframe(trades)
    grouped = df.groupby("exit_reason").agg(
        n=("net_pct", "count"),
        avg_net_pct=("net_pct", "mean"),
        total_net_pct=("net_pct", "sum"),
    ).reset_index()
    grouped["avg_net_pct"] = grouped["avg_net_pct"].round(3)
    grouped["total_net_pct"] = grouped["total_net_pct"].round(2)
    return grouped.sort_values("n", ascending=False)


def daily_summary(trades):
    """일별 손익 합계 (날짜별로 묶음)"""
    if not trades:
        return pd.DataFrame()
    df = trades_to_dataframe(trades)
    df["date"] = df["entry_time"].dt.strftime("%Y-%m-%d")
    daily = df.groupby("date").agg(
        n_trades=("net_pct", "count"),
        sum_net_pct=("net_pct", "sum"),
        avg_net_pct=("net_pct", "mean"),
    ).reset_index()
    daily["sum_net_pct"] = daily["sum_net_pct"].round(2)
    daily["avg_net_pct"] = daily["avg_net_pct"].round(3)
    return daily.sort_values("date")


def save_trades_csv(trades, path):
    """매매 내역 CSV 저장"""
    df = trades_to_dataframe(trades)
    if df.empty:
        return
    # 시간 컬럼 포맷팅
    if "entry_time" in df.columns:
        df["entry_time"] = pd.to_datetime(df["entry_time"]).dt.strftime("%Y-%m-%d %H:%M")
    if "exit_time" in df.columns:
        df["exit_time"] = pd.to_datetime(df["exit_time"]).dt.strftime("%Y-%m-%d %H:%M")
    df.to_csv(path, index=False, encoding="utf-8-sig")


def print_report(summaries, exit_reasons_per_mode):
    """모드별 비교 리포트 출력"""
    print("\n" + "=" * 78)
    print(" 백테스트 결과 비교 (룰 v2)")
    print("=" * 78)

    df = pd.DataFrame(summaries)
    print(df.to_string(index=False))

    print("\n" + "-" * 78)
    print(" 모드별 종료 사유 분포")
    print("-" * 78)
    for mode, reasons_df in exit_reasons_per_mode.items():
        print(f"\n[모드 {mode}]")
        if reasons_df.empty:
            print("  (매매 없음)")
        else:
            print(reasons_df.to_string(index=False))


if __name__ == "__main__":
    print("이 모듈은 backtest.py에서 호출됩니다. 단독 실행은 데모용입니다.")
