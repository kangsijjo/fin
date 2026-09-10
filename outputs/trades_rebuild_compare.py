# -*- coding: utf-8 -*-
"""
trades_rebuild_compare.py — 학습 데이터 재생성본을 교체하기 전 영향 비교 (2026-09-11)

배경: prm_net_5d_ratio(IC +0.141) 는 학습 데이터에서 라이브6 표본의 4.2%(3,552건)에만 있었다.
program_trading 백필 + make_trades_history_v3 배선(2026-09-10)으로 표본을 넓힌 재생성본을 만들었다.
trades_history_v3.csv 는 **라이브 강도점수의 IC 원천**이라, 교체하면 IC 가중치 → 강도점수 분포 →
임계 5.7 통과 여부까지 즉시 바뀐다. 교체 전에 그 영향을 수치로 확인한다.

비교
  C1 행수·기간·피처 커버리지 (특히 prm_net_5d_ratio / prm_net_5d_raw).
  C2 IC 변화: factor_scorer 와 동일 잣대(라이브6·풀링 Spearman)로 전 피처 IC·질량 재분배.
  C3 강도점수 분포: score_ic 재현으로 평균·표준편차·임계별 통과율 변화.
  C4 통과 매매의 실제 수익: 각 CSV 기준으로 5.7 통과분 평균 net%(같은 매매 풀에서 비교).
  C5 임계 재조정 필요량: 기존과 같은 통과율을 주는 새 임계.
  C6 회귀 확인: 두 파일에 공통인 (code,date) 행에서 기존 피처 값이 변하지 않았는지(재생성 안정성).

사용: .venv/Scripts/python.exe trades_rebuild_compare.py [--new trades_history_v3_new.csv]
"""
import os
import sys
import argparse

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from factor_scorer import IC_FEATURES, LIVE_STRATEGY_NAMES, FEATURE_META, MIN_IC_COVERAGE

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path):
    d = pd.read_csv(path, low_memory=False, dtype={"code": str})
    d["date"] = d["date"].astype(str).str.replace("-", "").str[:8]
    d["code"] = d["code"].str.zfill(6)
    return d


def ic_table(df):
    live = df[df["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)]
    y = live["net_pct"].astype(float)
    out = {}
    for f in IC_FEATURES:
        col = f
        if f == "crd_remn_rt" and "crd_remn_rt_y" in live.columns:
            col = "crd_remn_rt_y"
        if col not in live.columns:
            continue
        v = live[col].astype(float)
        m = v.notna() & y.notna()
        if m.sum() < 100:
            continue
        r, _ = spearmanr(v[m], y[m])
        if not np.isnan(r):
            out[f] = (float(r), int(m.sum()))
    return out, live


def scores(live, ic):
    full = sum(abs(v[0]) * 0.5 for v in ic.values())
    raw, avail = np.zeros(len(live)), np.zeros(len(live))
    for f, (w, _) in ic.items():
        col = "crd_remn_rt_y" if (f == "crd_remn_rt" and "crd_remn_rt_y" in live.columns) else f
        v = live[col].astype(float)
        sv = np.sort(v[v.notna()].to_numpy())
        arr = v.to_numpy(dtype=float)
        ok = ~np.isnan(arr)
        if not ok.any():
            continue
        pct = np.searchsorted(sv, arr[ok], side="left") / len(sv)
        raw[ok] += w * (pct - 0.5)
        avail[ok] += abs(w) * 0.5
    cov = avail / full if full else np.zeros_like(avail)
    denom = np.where((avail > 0) & (cov >= MIN_IC_COVERAGE), avail, full)
    return np.clip(5.0 + np.divide(raw, denom, out=np.zeros_like(raw), where=denom > 0) * 5.0, 0, 10), cov


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="trades_history_v3.csv")
    ap.add_argument("--new", default="trades_history_v3_new.csv")
    a = ap.parse_args()
    o, n = load(os.path.join(HERE, a.old)), load(os.path.join(HERE, a.new))

    print(f"{'=' * 96}\n [C1] 규모·커버리지\n{'=' * 96}")
    print(f"  {'':22s} {'기존':>16s} {'재생성':>16s} {'변화':>12s}")
    print(f"  {'행수':22s} {len(o):16,} {len(n):16,} {len(n) - len(o):+12,}")
    print(f"  {'기간':22s} {o.date.min() + '~' + o.date.max():>16s} {n.date.min() + '~' + n.date.max():>16s}")
    for c in ("prm_net_5d_ratio", "prm_net_5d_raw", "crd_remn_rt", "for_net5_db", "rsi_db", "news_sent_7d"):
        if c not in o.columns or c not in n.columns:
            continue
        a_, b_ = o[c].notna().mean() * 100, n[c].notna().mean() * 100
        print(f"  {c:22s} {a_:15.1f}% {b_:15.1f}% {b_ - a_:+11.1f}%p")
    lo = o[o.strategy.isin(LIVE_STRATEGY_NAMES)]
    ln = n[n.strategy.isin(LIVE_STRATEGY_NAMES)]
    print(f"  라이브6 prm 표본        {lo.prm_net_5d_ratio.notna().sum():15,} {ln.prm_net_5d_ratio.notna().sum():15,} "
          f"{ln.prm_net_5d_ratio.notna().sum() - lo.prm_net_5d_ratio.notna().sum():+12,}")

    ic_o, live_o = ic_table(o)
    ic_n, live_n = ic_table(n)
    print(f"\n{'=' * 96}\n [C2] IC 변화 (라이브6·풀링 Spearman — factor_scorer 동일 잣대)\n{'=' * 96}")
    mass_o = sum(abs(v[0]) for v in ic_o.values())
    mass_n = sum(abs(v[0]) for v in ic_n.values())
    print(f"  {'피처':22s} {'기존 IC':>10s} {'표본':>9s} {'신규 IC':>10s} {'표본':>9s} {'IC변화':>9s} {'질량%':>14s}")
    keys = sorted(set(ic_o) | set(ic_n), key=lambda k: -abs(ic_n.get(k, ic_o.get(k, (0, 0)))[0]))
    for f in keys:
        a_ic, a_n = ic_o.get(f, (np.nan, 0))
        b_ic, b_n = ic_n.get(f, (np.nan, 0))
        lab = FEATURE_META.get(f, (f, 0))[0]
        m_o = abs(a_ic) / mass_o * 100 if not np.isnan(a_ic) else np.nan
        m_n = abs(b_ic) / mass_n * 100 if not np.isnan(b_ic) else np.nan
        star = " ★" if f.startswith("prm_") else ""
        print(f"  {lab:22s} {a_ic:+10.4f} {a_n:9,} {b_ic:+10.4f} {b_n:9,} {b_ic - a_ic:+9.4f} "
              f"{m_o:6.1f}→{m_n:5.1f}{star}")

    s_o, cov_o = scores(live_o, ic_o)
    s_n, cov_n = scores(live_n, ic_n)
    print(f"\n{'=' * 96}\n [C3] 강도점수 분포 (각 CSV 자체 기준)\n{'=' * 96}")
    print(f"  기존: 평균 {s_o.mean():.3f} std {s_o.std():.3f} | 커버리지 {cov_o.mean() * 100:.1f}%")
    print(f"  신규: 평균 {s_n.mean():.3f} std {s_n.std():.3f} | 커버리지 {cov_n.mean() * 100:.1f}%")
    print(f"  {'임계':>6s} {'기존 통과율':>12s} {'신규 통과율':>12s} {'변화':>9s} | {'기존 평균net':>12s} {'신규 평균net':>12s}")
    for T in (5.5, 5.7, 6.0, 6.3):
        po, pn = (s_o >= T).mean() * 100, (s_n >= T).mean() * 100
        ao = live_o[s_o >= T]["net_pct"].mean() if (s_o >= T).any() else np.nan
        an = live_n[s_n >= T]["net_pct"].mean() if (s_n >= T).any() else np.nan
        print(f"  {T:6.1f} {po:11.1f}% {pn:11.1f}% {pn - po:+8.1f}p | {ao:+12.3f} {an:+12.3f}")

    print(f"\n{'=' * 96}\n [C5] 임계 재조정 — 기존 5.7 과 같은 통과율을 주는 신규 임계\n{'=' * 96}")
    k = int((s_o >= 5.7).sum())
    thr = float(np.sort(s_n)[::-1][k - 1]) if 0 < k <= len(s_n) else np.nan
    print(f"  기존 5.7 통과 {k:,}건({k / len(s_o) * 100:.1f}%) → 신규 임계 {thr:.2f} 에서 같은 비율")
    print(f"  신규 {thr:.2f} 통과분 평균 net {live_n[s_n >= thr]['net_pct'].mean():+.3f} "
          f"(기존 5.7 통과분 {live_o[s_o >= 5.7]['net_pct'].mean():+.3f})")

    print(f"\n{'=' * 96}\n [C6] 회귀 확인 — 공통 행에서 기존 피처가 변하지 않았나\n{'=' * 96}")
    key = ["code", "date", "strategy"]
    j = o.merge(n, on=key, how="inner", suffixes=("_o", "_n"))
    print(f"  공통 행 {len(j):,}")
    for c in ("net_pct", "score_tv", "rsi14", "kospi_ret_20d", "for_5d"):
        if f"{c}_o" not in j.columns:
            continue
        d = (j[f"{c}_o"] - j[f"{c}_n"]).abs()
        bad = (d > 1e-6).sum()
        print(f"  {c:18s} 불일치 {bad:,}행 ({bad / max(len(j), 1) * 100:.2f}%) 최대오차 {d.max():.6g}")


if __name__ == "__main__":
    main()
