# -*- coding: utf-8 -*-
"""
regime_strength_sim.py — 국면 피처를 강도점수에 넣으면 실제 매매가 어떻게 바뀌나 (2026-09-10, 최종 검정)

왜 이 검정이 결정적인가
  mkt_fin_10d 는 **시장 단위** 값이라 같은 날 모든 종목에 동일하다. 따라서 강도점수의
  '종목 간 순위'는 전혀 바꾸지 않고 **날짜별 점수 수준만** 움직인다 = 순수 마켓타이밍 필터.
  임계(MIN_STRENGTH_SCORE 5.7) 통과 여부만 바뀌므로, 효과는 "언제 사고 언제 쉬는가"로만 나타난다.
  이진 게이트(자본곡선 붕괴)와 달리 다른 피처가 강하면 나쁜 국면에서도 통과할 수 있는 연속 필터다.

검정 (factor_scorer.score_ic 를 그대로 재현: available-only 정규화, 5.0 + Σic·(pct-0.5)/denom·5)
  S1 현행 20피처 강도점수 재구성 → 임계별 통과율·평균 net% 가 기존 문서(5.7 통과 14.8%)와 정합한지.
  S2 mkt_fin_10d 추가 후 동일 계산 → 통과 매매의 평균 net%·건수·연도별 변화.
  S3 임계 재조정: 추가 후 '기존과 같은 통과 건수'를 주는 임계는 얼마이고, 그때 수익은?
     (새 피처가 분포를 이동시키므로 5.7 고정 비교는 불공정 — 통과율 고정 비교가 정답)
  S4 기간별(A/B/C) 동일 통과율 기준 개선폭 — 세 기간 전부 양수여야 채택.
  S5 현재(2026-09) 국면에서 강도점수가 얼마나 내려가는지 실측 + 8월 매매정지와의 상호작용.

판정: 채택 = 동일 통과율 기준으로 세 기간 전부 평균 net% 개선 AND 전체 개선 >= +0.3%p.
"""
import os
import sys
import bisect

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from factor_scorer import IC_FEATURES, LIVE_STRATEGY_NAMES, MIN_IC_COVERAGE

HERE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(HERE, "trades_history_v3.csv")
CACHE = os.path.join(HERE, "macro_data", "kosdaq_investor_by_date.csv")
PERIODS = {"A 2018-20": ("20180101", "20210101"), "B 2021-24.09": ("20210101", "20240908"),
           "C 2024.09-": ("20240908", "20990101")}
NEW = "mkt_fin_10d"


def build_regime(lag=1):
    d = pd.read_csv(CACHE, dtype={"date": str}).set_index("date").sort_index()
    acc = d["금융투자"].astype(float).rolling(10, min_periods=10).sum().shift(lag)
    return acc.rolling(250, min_periods=120).apply(lambda s: s.rank(pct=True).iloc[-1] * 100.0, raw=False).rename(NEW)


def compute_scores(df, feats_ic, feat_sorted):
    """factor_scorer.score_ic 재현 — 행별 강도점수(0~10)."""
    full_mass = sum(abs(ic) * 0.5 for ic in feats_ic.values())
    raw = np.zeros(len(df))
    avail = np.zeros(len(df))
    for feat, ic in feats_ic.items():
        col = df[feat].to_numpy(dtype=float)
        sv = feat_sorted[feat]
        n = len(sv)
        ok = ~np.isnan(col)
        if not ok.any():
            continue
        pct = np.full(len(df), np.nan)
        pct[ok] = np.searchsorted(sv, col[ok], side="left") / n
        raw[ok] += ic * (pct[ok] - 0.5)
        avail[ok] += abs(ic) * 0.5
    cov = np.divide(avail, full_mass, out=np.zeros_like(avail), where=full_mass > 0)
    denom = np.where((avail > 0) & (cov >= MIN_IC_COVERAGE), avail, full_mass)
    total = 5.0 + np.divide(raw, denom, out=np.zeros_like(raw), where=denom > 0) * 5.0
    return np.clip(total, 0.0, 10.0), cov


def prep(x, feats):
    ic, sorted_vals = {}, {}
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
        ic[f] = float(r)
        sorted_vals[f] = np.sort(v[m].to_numpy())
        if col != f:
            x[f] = v
    return ic, sorted_vals


def report(x, s_old, s_new, label_old="현행 20피처", label_new="+ 시장국면"):
    print(f"\n  {'임계':>6s} {'통과율(현행)':>12s} {'평균net':>9s} | {'통과율(신규)':>12s} {'평균net':>9s} | {'개선':>8s}")
    for T in (5.5, 5.7, 6.0, 6.3):
        a, b = x[s_old >= T], x[s_new >= T]
        if len(a) < 50 or len(b) < 50:
            continue
        print(f"  {T:6.1f} {len(a) / len(x) * 100:11.1f}% {a['net_pct'].mean():+9.3f} | "
              f"{len(b) / len(x) * 100:11.1f}% {b['net_pct'].mean():+9.3f} | {b['net_pct'].mean() - a['net_pct'].mean():+8.3f}")


def main():
    reg = build_regime()
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    t["entry_date"] = t["entry_date"].astype(str).str.replace("-", "").str[:8]
    x = t[t["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)].copy()
    x = x.merge(reg, left_on="entry_date", right_index=True, how="left")
    x = x[x[NEW].notna()].reset_index(drop=True)
    print(f"라이브 6전략 {len(x):,}건 (국면값 있는 행) | {x.entry_date.min()}~{x.entry_date.max()}")

    ic_old, sv_old = prep(x, IC_FEATURES)
    ic_new, sv_new = prep(x, IC_FEATURES + [NEW])
    print(f"IC 피처 수: 현행 {len(ic_old)} → 신규 {len(ic_new)} | {NEW} IC {ic_new.get(NEW, float('nan')):+.4f} "
          f"(질량 {abs(ic_new.get(NEW, 0)) / sum(abs(v) for v in ic_new.values()) * 100:.1f}%)")

    s_old, cov_old = compute_scores(x, ic_old, sv_old)
    s_new, cov_new = compute_scores(x, ic_new, sv_new)
    print(f"\n [S1] 강도점수 분포 — 현행: 평균 {s_old.mean():.2f} std {s_old.std():.2f} | "
          f"5.7 통과 {(s_old >= 5.7).mean() * 100:.1f}% | 커버리지 평균 {cov_old.mean() * 100:.1f}%")
    print(f"      (문서 실측: 6~8월 available-only 전환 후 std 0.76 / 5.7 통과 14.8% — 정합성 확인용)")
    print(f" [S2] 국면 추가 — 신규: 평균 {s_new.mean():.2f} std {s_new.std():.2f} | "
          f"5.7 통과 {(s_new >= 5.7).mean() * 100:.1f}%")
    report(x, s_old, s_new)

    # ── S3: 동일 통과 '건수' 기준 공정 비교 ──
    print(f"\n [S3] 동일 통과율 기준(공정 비교) — 현행 5.7 통과 건수와 같아지는 신규 임계")
    target_n = int((s_old >= 5.7).sum())
    thr_new = float(np.sort(s_new)[::-1][target_n - 1]) if target_n > 0 else np.nan
    a, b = x[s_old >= 5.7], x[s_new >= thr_new]
    print(f"   현행 5.7 → {len(a):,}건 평균 {a['net_pct'].mean():+.3f} | 신규 {thr_new:.2f} → {len(b):,}건 평균 {b['net_pct'].mean():+.3f} "
          f"| 개선 {b['net_pct'].mean() - a['net_pct'].mean():+.3f}%p")
    print(f"   교체율: 현행 통과분 중 {100 - len(set(a.index) & set(b.index)) / max(len(a), 1) * 100:.0f}% 가 다른 매매로 교체")

    # ── S4: 기간별 (동일 통과율) ──
    print(f"\n [S4] 기간별 — 각 기간 안에서 동일 통과 건수로 맞춘 비교")
    print(f"   {'기간':14s} {'건수':>8s} {'현행 평균':>10s} {'신규 평균':>10s} {'개선':>9s} {'승률(현→신)':>16s}")
    ok_all = True
    for p, (lo, hi) in PERIODS.items():
        m = (x.entry_date >= lo) & (x.entry_date < hi)
        if m.sum() < 500:
            continue
        so, sn = s_old[m.to_numpy()], s_new[m.to_numpy()]
        xs = x[m]
        k = int((so >= 5.7).sum())
        if k < 50:
            print(f"   {p:14s} (통과 {k}건 — 표본부족)")
            continue
        thr = float(np.sort(sn)[::-1][k - 1])
        aa, bb = xs[so >= 5.7], xs[sn >= thr]
        d = bb["net_pct"].mean() - aa["net_pct"].mean()
        ok_all &= d > 0
        print(f"   {p:14s} {k:8,} {aa['net_pct'].mean():+10.3f} {bb['net_pct'].mean():+10.3f} {d:+9.3f} "
              f"{(aa['net_pct'] > 0).mean() * 100:7.1f}→{(bb['net_pct'] > 0).mean() * 100:6.1f}%")
    print(f"   → 세 기간 전부 개선: {'예 ✔' if ok_all else '아니오'}")

    # ── S5: 현재 국면의 실제 하방 압력 ──
    print(f"\n [S5] 현재 국면(2026-09)에서의 강도점수 영향")
    cur = reg.dropna()
    latest = float(cur.iloc[-1])
    sv = sv_new[NEW]
    pct_now = np.searchsorted(sv, latest, side="left") / len(sv)
    mass = sum(abs(v) * 0.5 for v in ic_new.values())
    delta_now = ic_new[NEW] * (pct_now - 0.5) / mass * 5.0
    print(f"   최근값 {latest:.0f} 백분위 → 피처 백분위 {pct_now * 100:.0f}% → 강도점수 {delta_now:+.2f}점")
    print(f"   최근 60거래일 범위: {cur.tail(60).min():.0f}~{cur.tail(60).max():.0f} 백분위 "
          f"→ 강도 영향 {ic_new[NEW] * (np.searchsorted(sv, cur.tail(60).min()) / len(sv) - 0.5) / mass * 5:+.2f} ~ "
          f"{ic_new[NEW] * (np.searchsorted(sv, cur.tail(60).max()) / len(sv) - 0.5) / mass * 5:+.2f}점")
    print(f"   ⚠ 2026-08 강도 최대 5.63(<5.7)로 매수 0건. 국면 피처는 좋을 때 +, 나쁠 때 - 로 양방향이나,")
    print(f"     지금은 하방이므로 임계 5.7 고정 시 매매 정지가 길어진다 → S3 처럼 임계 동반 재조정 필요.")


if __name__ == "__main__":
    main()
