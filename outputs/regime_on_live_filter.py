# -*- coding: utf-8 -*-
"""
regime_on_live_filter.py — 실제 배포 시나리오: **강도 필터를 통과한 매매**에만 국면을 적용 (2026-09-10, 최종)

앞선 두 결과가 상반돼 보인다 — 이 스크립트가 그 모순을 푼다.
  · regime_gate_final: 전체 매매(113,683)에 국면 게이트 → 평균 +0.97%p, 세 기간 전부 개선.
  · regime_strength_sim: 강도점수에 피처로 섞고 **동일 통과율**로 비교 → 개선 -0.011%p (C 구간 -0.893).
  가설: 강도점수(IC 1위 kospi_ret_20d)가 **이미 국면을 상당 부분 잡고 있어서**, 강도 통과분에는
        국면 정보가 거의 남아 있지 않다. 그렇다면 게이트의 이득은 '강도가 이미 거르는 매매'를
        중복으로 거른 것에 불과하다.

검정
  L1 강도 통과 매매의 국면 분포 vs 전체 — 이미 좋은 국면에 몰려 있나(중복의 직접 증거).
  L2 강도 통과분에만 국면 게이트: 임계별 평균 net%·건수·기간별. 여기서 개선이 남으면 진짜 추가 정보.
  L3 강도 통과분의 국면 5분위 수익 — 단조성이 남아 있나.
  L4 동적 임계: 국면 좋은 날 5.5 / 나쁜 날 6.0 (매매 수 비슷하게 유지) vs 고정 5.7.
  L5 자본곡선(10슬롯): 강도 통과분 기준 무게이트 vs 국면 게이트 — 실제 배포에 가장 가까운 형태.

판정: 배포 = L2 개선 >= +0.3%p 이고 세 기간 부호 일치 AND L5 CAGR 개선. 아니면 '중복 확정, 배포 불필요'.
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from factor_scorer import IC_FEATURES, LIVE_STRATEGY_NAMES, MIN_IC_COVERAGE
from strategies.base import StrategyTrade
from capital_simulator import simulate_capital

HERE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(HERE, "trades_history_v3.csv")
CACHE = os.path.join(HERE, "macro_data", "kosdaq_investor_by_date.csv")
PERIODS = {"A 2018-20": ("20180101", "20210101"), "B 2021-24.09": ("20210101", "20240908"),
           "C 2024.09-": ("20240908", "20990101")}
NEW = "mkt_fin_10d"
THR = 5.7


def build_regime(lag=1):
    d = pd.read_csv(CACHE, dtype={"date": str}).set_index("date").sort_index()
    acc = d["금융투자"].astype(float).rolling(10, min_periods=10).sum().shift(lag)
    return acc.rolling(250, min_periods=120).apply(lambda s: s.rank(pct=True).iloc[-1] * 100.0, raw=False).rename(NEW)


def prep_ic(x, feats):
    ic, sv = {}, {}
    y = x["net_pct"].astype(float)
    for f in feats:
        col = "crd_remn_rt_y" if (f == "crd_remn_rt" and "crd_remn_rt_y" in x.columns) else f
        if col not in x.columns:
            continue
        v = x[col].astype(float)
        m = v.notna() & y.notna()
        if m.sum() < 100:
            continue
        r, _ = spearmanr(v[m], y[m])
        if np.isnan(r):
            continue
        ic[f], sv[f] = float(r), np.sort(v[m].to_numpy())
        if col != f:
            x[f] = v
    return ic, sv


def scores(df, ic, sv):
    full = sum(abs(v) * 0.5 for v in ic.values())
    raw, avail = np.zeros(len(df)), np.zeros(len(df))
    for f, w in ic.items():
        col = df[f].to_numpy(dtype=float)
        ok = ~np.isnan(col)
        if not ok.any():
            continue
        pct = np.searchsorted(sv[f], col[ok], side="left") / len(sv[f])
        raw[ok] += w * (pct - 0.5)
        avail[ok] += abs(w) * 0.5
    cov = avail / full if full else np.zeros_like(avail)
    denom = np.where((avail > 0) & (cov >= MIN_IC_COVERAGE), avail, full)
    return np.clip(5.0 + np.divide(raw, denom, out=np.zeros_like(raw), where=denom > 0) * 5.0, 0, 10)


def sim(df, w=1.0):
    tr = [StrategyTrade(strategy=r.strategy, code=str(r.code), entry_date=r.entry_date, entry_price=10000.0,
                        exit_date=r.exit_date, exit_price=10000.0, holding_days=20,
                        gross_pct=float(r.net_pct), cost_pct=0.0, net_pct=float(r.net_pct) * w,
                        score=-float(getattr(r, "score_tv", 0) or 0)) for r in df.itertuples()]
    s = simulate_capital(tr) or {}
    return s.get("cagr_pct"), s.get("real_mdd_pct"), s.get("real_sharpe")


def main():
    reg = build_regime()
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    for c in ("entry_date", "exit_date"):
        t[c] = t[c].astype(str).str.replace("-", "").str[:8]
    x = t[t["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)].copy()
    x = x.merge(reg, left_on="entry_date", right_index=True, how="left")
    x = x[x[NEW].notna()].reset_index(drop=True)
    ic, sv = prep_ic(x, IC_FEATURES)
    x["strength"] = scores(x, ic, sv)
    x["period"] = np.select([x.entry_date < "20210101", x.entry_date < "20240908"],
                            list(PERIODS)[:2], list(PERIODS)[2])
    passed = x[x.strength >= THR].copy()
    print(f"라이브 6전략 {len(x):,}건 | 강도 {THR} 통과 {len(passed):,}건 ({len(passed) / len(x) * 100:.1f}%) "
          f"평균 net {passed.net_pct.mean():+.3f} (전체 {x.net_pct.mean():+.3f})")

    # L1 중복의 직접 증거
    print(f"\n{'=' * 92}\n [L1] 강도 통과 매매는 이미 좋은 국면에 몰려 있나\n{'=' * 92}")
    print(f"   {NEW} 백분위 평균: 전체 {x[NEW].mean():.1f} | 강도 통과 {passed[NEW].mean():.1f} | 탈락 {x[x.strength < THR][NEW].mean():.1f}")
    b = pd.cut(x[NEW], [0, 20, 40, 60, 80, 100.001], labels=["0-20", "20-40", "40-60", "60-80", "80-100"], include_lowest=True)
    tab = pd.crosstab(b, x.strength >= THR, normalize="index") * 100
    print(f"   국면 구간별 강도 통과율: " + " | ".join(f"{k} {v[True]:.0f}%" for k, v in tab.iterrows()))
    print(f"   → 통과율이 국면에 따라 크게 다르면 강도점수가 이미 국면을 반영(중복) 중.")

    # L2 강도 통과분에만 게이트
    print(f"\n{'=' * 92}\n [L2] 강도 통과분에만 국면 게이트 ({NEW} >= T 인 날만 매수)\n{'=' * 92}")
    print(f"   {'T':>4s} {'건수':>8s} {'전체평균':>9s} {'개선':>8s} " + "".join(f"{p:>22s}" for p in PERIODS))
    base = passed.net_pct.mean()
    for T in (0, 10, 20, 30, 40, 50):
        k = passed[passed[NEW] >= T]
        row = f"   {T:4d} {len(k):8,} {k.net_pct.mean():+9.3f} {k.net_pct.mean() - base:+8.3f} "
        for p in PERIODS:
            a, bb = passed[passed.period == p], k[k.period == p]
            row += f"  {a.net_pct.mean():+6.2f}→{bb.net_pct.mean():+6.2f}({bb.net_pct.mean() - a.net_pct.mean():+5.2f})" if len(bb) > 30 else f"{'-':>22s}"
        print(row)

    # L3 통과분의 국면 5분위
    print(f"\n{'=' * 92}\n [L3] 강도 통과분의 국면 5분위 수익 — 단조성이 남아 있나\n{'=' * 92}")
    q = pd.cut(passed[NEW], [0, 20, 40, 60, 80, 100.001], labels=["Q1", "Q2", "Q3", "Q4", "Q5"], include_lowest=True)
    for p in list(PERIODS) + ["전체"]:
        s = passed if p == "전체" else passed[passed.period == p]
        qq = pd.cut(s[NEW], [0, 20, 40, 60, 80, 100.001], labels=["Q1", "Q2", "Q3", "Q4", "Q5"], include_lowest=True)
        g = s.groupby(qq, observed=True)["net_pct"].agg(["mean", "size"])
        print(f"   {p:14s} " + " | ".join(f"{k} {r['mean']:+6.2f}(n{int(r['size']):,})" for k, r in g.iterrows()))

    # L4 동적 임계
    print(f"\n{'=' * 92}\n [L4] 동적 임계 — 국면 좋으면 5.5 / 나쁘면 6.0 (매매수 유지) vs 고정 5.7\n{'=' * 92}")
    dyn = x[((x[NEW] >= 50) & (x.strength >= 5.5)) | ((x[NEW] < 50) & (x.strength >= 6.0))]
    print(f"   고정 5.7: {len(passed):,}건 평균 {passed.net_pct.mean():+.3f} | 동적: {len(dyn):,}건 평균 {dyn.net_pct.mean():+.3f} "
          f"| 개선 {dyn.net_pct.mean() - passed.net_pct.mean():+.3f}%p")
    for p in PERIODS:
        a, bb = passed[passed.period == p], dyn[dyn.period == p]
        print(f"     {p:14s} {a.net_pct.mean():+7.3f}({len(a):,}) → {bb.net_pct.mean():+7.3f}({len(bb):,}) "
              f"= {bb.net_pct.mean() - a.net_pct.mean():+.3f}")

    # L5 자본곡선
    print(f"\n{'=' * 92}\n [L5] 자본곡선(10슬롯) — 강도 통과분 기준, 실제 배포에 가장 가까운 형태\n{'=' * 92}")
    print(f"   {'기간':14s} {'형태':14s} {'건수':>8s} {'CAGR%':>9s} {'MDD%':>9s} {'Sharpe':>8s}")
    for p in list(PERIODS) + ["전체"]:
        s = passed if p == "전체" else passed[passed.period == p]
        if len(s) < 100:
            continue
        for lab, sub in (("무게이트", s), (f"국면>=30", s[s[NEW] >= 30]), ("동적임계", dyn if p == "전체" else dyn[dyn.period == p])):
            if len(sub) < 50:
                continue
            c, m, sh = sim(sub)
            print(f"   {p:14s} {lab:14s} {len(sub):8,} {str(c):>9s} {str(m):>9s} {str(sh):>8s}")
        print()


if __name__ == "__main__":
    main()
