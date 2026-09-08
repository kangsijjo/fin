# -*- coding: utf-8 -*-
"""
flow_form_diagnostic.py - '같은 수급 정보, 다른 형태' 예측력 진단. (2026-09-08)

질문(사용자): 기관·외국인은 돈을 버는데 왜 수급선 크로스오버는 실패했나? 패턴이 있을 텐데.
가설: 정보 자체가 없는 게 아니라 **정보를 신호로 바꾼 형태**가 틀렸다.
  크로스오버 = 20일 누적선의 '부호 뒤집힘'만 보고 **규모를 버린다**(₩1천만 순매수 역전 = ₩500억 역전).
  → 규모(시총·거래대금 대비 강도)·짧은 창(5일)·수급-가격 괴리·시장단위 형태로 바꿔 비교한다.

전략 백테스트가 아니라 **진단**(IC·분위 스프레드). 파라미터 탐색 없음 - 아래 형태를 사전 고정.
  F1 크로스오버(사용자 규칙, 이진)          : SupplyCrossoverStrategy.signal_df 그대로
  F2 규모형 for_5d/ins_5d/smart_5d          : 5일 순매수 합 / 시총 x1000  (factor_scorer 와 동일 정의)
  F3 강도형 smart_5d_tv                     : 5일 순매수 합 / (직전20일 평균거래대금 x5) = 5일 회전율 대비
  F4 괴리형 (F2 상위10% 안에서 과거20일 수익률 3분위) : 아직 안 오른 매집 vs 이미 오른 매집
  F5 시장단위 : 전종목 합산 외국인 20일 누적 → 시장 전방 20일 수익률 (마켓타이밍)

예측 대상: 전방 20일 총수익률 = close[t+20]/open[t+1]-1 (비용은 상수라 IC 비교에 무관, gross 표기).
유동성 필터 거래대금>=10억. IS(<20240908)/OOS 분리. IC = 일별 횡단면 Spearman 의 평균(표준 정의).
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from strategies.daily_loader import load_macro_daily
from strategies.supply_reversal import SupplyCrossoverStrategy

HOLD = 20
MIN_TV = 1_000_000_000
OOS = "20240908"


def prep(df):
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    g = df.groupby("code", group_keys=False)
    nxt_open = g["open"].shift(-1).where(lambda s: s > 0)
    exit_close = g["close"].shift(-HOLD).where(lambda s: s > 0)
    df["fwd20"] = (exit_close / nxt_open - 1.0) * 100.0

    mcap = df["market_cap"].where(df["market_cap"] > 0)
    for5 = g["foreign_net"].transform(lambda s: s.fillna(0).rolling(5, min_periods=5).sum())
    ins5 = g["inst_net"].transform(lambda s: s.fillna(0).rolling(5, min_periods=5).sum())
    df["for_5d"] = for5 / mcap * 1000
    df["ins_5d"] = ins5 / mcap * 1000
    df["smart_5d"] = (for5 + ins5) / mcap * 1000
    tv20 = g["trading_value"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    df["smart_5d_tv"] = (for5 + ins5) / (tv20.where(tv20 > 0) * 5)
    df["mom20"] = g["close"].transform(lambda s: s / s.shift(20) - 1.0) * 100.0
    df["seg"] = np.where(df["date"] >= OOS, "OOS", "IS")
    return df


def daily_ic(df, feat, mask):
    """일별 횡단면 Spearman IC 의 평균·t값·양수비율."""
    sub = df.loc[mask & df[feat].notna() & df["fwd20"].notna(), ["date", feat, "fwd20"]]
    ics = []
    for _, d in sub.groupby("date"):
        if len(d) < 30:
            continue
        ics.append(d[feat].rank().corr(d["fwd20"].rank()))
    ics = pd.Series(ics).dropna()
    if len(ics) == 0:
        return None
    return dict(ic=ics.mean(), t=ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))),
                pos=(ics > 0).mean() * 100, n_days=len(ics))


def decile_spread(df, feat, mask):
    """일별 10분위 후 상위10% - 하위10% 평균 전방수익(풀링)."""
    sub = df.loc[mask & df[feat].notna() & df["fwd20"].notna(), ["date", feat, "fwd20"]].copy()
    sub["dec"] = sub.groupby("date")[feat].transform(
        lambda s: pd.qcut(s.rank(method="first"), 10, labels=False) if len(s) >= 30 else np.nan)
    sub = sub.dropna(subset=["dec"])
    top = sub[sub["dec"] == 9]["fwd20"].mean()
    bot = sub[sub["dec"] == 0]["fwd20"].mean()
    return top, bot, top - bot


def run_market(label, data_dir):
    print(f"\n{'=' * 78}\n {label}\n{'=' * 78}")
    df = load_macro_daily(start_date="20210101", data_dir=data_dir).reset_index(drop=True)
    df = SupplyCrossoverStrategy(take_profit_pct=None).signal_df(df)
    df = prep(df)
    liquid = (df["trading_value"] >= MIN_TV) & df["fwd20"].notna()
    print(f" 유효 종목-일 {int(liquid.sum()):,} | 종목 {df['code'].nunique():,}")

    for seg in ("IS", "OOS"):
        m = liquid & (df["seg"] == seg)
        base = df.loc[m, "fwd20"].mean()
        xo = df.loc[m & (df["signal"] == True), "fwd20"].mean()  # noqa: E712
        print(f"\n [{seg}] 유니버스 평균 전방20일 {base:+.3f}%  |  F1 크로스오버 {xo:+.3f}%  "
              f"→ 알파 {xo - base:+.3f}%p")
        print(f" {'형태':14s} {'IC':>8s} {'t값':>7s} {'양수일%':>8s} {'상위10%':>9s} {'하위10%':>9s} {'스프레드':>9s}")
        for feat in ("for_5d", "ins_5d", "smart_5d", "smart_5d_tv"):
            r = daily_ic(df, feat, m)
            if r is None:
                continue
            top, bot, sp = decile_spread(df, feat, m)
            print(f" {feat:14s} {r['ic']:+8.4f} {r['t']:+7.2f} {r['pos']:8.1f} "
                  f"{top:+9.3f} {bot:+9.3f} {sp:+9.3f}")

        # F4 괴리형: smart_5d 일별 상위10% 안에서 과거20일 수익률 3분위
        sub = df.loc[m & df["smart_5d"].notna() & df["mom20"].notna(), ["date", "smart_5d", "mom20", "fwd20"]].copy()
        sub["dec"] = sub.groupby("date")["smart_5d"].transform(
            lambda s: pd.qcut(s.rank(method="first"), 10, labels=False) if len(s) >= 30 else np.nan)
        top = sub[sub["dec"] == 9].copy()
        if len(top) >= 300:
            top["mom_t"] = pd.qcut(top["mom20"].rank(method="first"), 3, labels=["아직 안오름", "중간", "이미 오름"])
            agg = top.groupby("mom_t", observed=True)["fwd20"].agg(["mean", "count"])
            print(" F4 괴리형(스마트머니 상위10% 매집 중, 과거20일 수익률로 분할):")
            for k, row in agg.iterrows():
                print(f"     {k:8s} 전방20일 {row['mean']:+.3f}%  (n {int(row['count']):,})")

    # F5 시장단위: 전종목 합산 외국인 20일 누적 vs 시장 전방 20일 (등가중)
    day = df[liquid].groupby("date").agg(mkt_for=("foreign_net", "sum"), mkt_fwd=("fwd20", "mean")).sort_index()
    day["mkt_for20"] = day["mkt_for"].rolling(20, min_periods=20).sum()
    d2 = day.dropna()
    d2["q"] = pd.qcut(d2["mkt_for20"].rank(method="first"), 5, labels=False)
    print("\n F5 시장단위: 시장 전체 외국인 20일 누적 순매수 5분위 → 시장(등가중) 전방20일 수익")
    for q, r in d2.groupby("q")["mkt_fwd"].agg(["mean", "count"]).iterrows():
        tag = {0: "최대 순매도", 4: "최대 순매수"}.get(q, "")
        print(f"     Q{q + 1} {tag:8s} {r['mean']:+.3f}%  (일수 {int(r['count'])})")
    rc = d2["mkt_for20"].rank().corr(d2["mkt_fwd"].rank())
    print(f"     순위상관 {rc:+.3f}  (주의: 중첩 구간이라 표본 독립 아님 - 방향 참고용)")


def main():
    run_market("KOSDAQ (1,822종목)", None)
    run_market("KOSPI (943종목)", "macro_data/daily_kospi")


if __name__ == "__main__":
    main()
