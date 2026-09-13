# -*- coding: utf-8 -*-
"""
improvement_map.py — "지금 방식보다 나아질 여지가 어디에 있나" 지도 (2026-09-13, 사용자 질문)

배경: 2026-09 에 세 방향이 막혔다 — 수급 크로스(§30·31·33), 새 피처 추가(§31·34, 세 번 모두 +0.00%p),
      청산 규칙 전수 탐색(§33, 4,830조합 전부 음수). 반면 **결측 해소는 통했다**(§32).
      그래서 "무엇이 안 되나" 대신 **"어디에 얼마나 남아 있나"** 를 측정한다.

측정 (전부 기존 데이터, 새 수집 없음)
  M1 전략 구성 — 라이브 6전략 각각의 기여. 나쁜 전략을 빼는 것만으로 개선되는가(반사실).
  M2 강도 임계 곡선 — 현재 5.7 이 최적인가. 구간별 한계수익(다음 1%p 를 더 자르면 얼마 버는가).
  M3 보유기간 — 20일 고정이 최적인가. 5~40일 곡선.
  M4 예측력 상한 — '완벽한 선별'(사후 최고 수익 순) 대비 현재가 몇 %를 실현하나. 남은 여지의 크기.
  M5 남은 결측 — §32 이후에도 9.6% 가 결측으로 버려진다. 어느 피처를 채우면 얼마를 되찾나.
  M6 회전율·슬롯 — 10슬롯 제약이 수익을 얼마나 깎는가(슬롯 부족으로 버려진 신호).

각 항목은 '개선 가능 규모(%p)' 로 환산해 우선순위를 매긴다.
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

from factor_scorer import IC_FEATURES, LIVE_STRATEGY_NAMES, MIN_IC_COVERAGE, FEATURE_META

HERE = os.path.dirname(os.path.abspath(__file__))
OOS = "20240908"


def load():
    t = pd.read_csv(os.path.join(HERE, "trades_history_v3.csv"), low_memory=False, dtype={"code": str})
    for c in ("date", "entry_date", "exit_date"):
        t[c] = t[c].astype(str).str.replace("-", "").str[:8]
    x = t[t["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)].copy()
    x["seg"] = np.where(x["entry_date"] >= OOS, "OOS", "IS")
    y = x["net_pct"].astype(float)
    ic, sv = {}, {}
    for f in IC_FEATURES:
        col = "crd_remn_rt_y" if (f == "crd_remn_rt" and "crd_remn_rt_y" in x.columns) else f
        if col not in x.columns:
            continue
        v = pd.to_numeric(x[col], errors="coerce")
        m = v.notna() & y.notna()
        if m.sum() < 100:
            continue
        r, _ = spearmanr(v[m], y[m])
        if not np.isnan(r):
            ic[f], sv[f], x[f] = float(r), np.sort(v[m].to_numpy()), v
    full = sum(abs(w) * 0.5 for w in ic.values())
    raw, av = np.zeros(len(x)), np.zeros(len(x))
    for f, w in ic.items():
        arr = x[f].to_numpy(dtype=float)
        ok = ~np.isnan(arr)
        raw[ok] += w * (np.searchsorted(sv[f], arr[ok], side="left") / len(sv[f]) - 0.5)
        av[ok] += abs(w) * 0.5
    cov = av / full
    den = np.where((av > 0) & (cov >= MIN_IC_COVERAGE), av, full)
    x["strength"] = np.clip(5.0 + np.divide(raw, den, out=np.zeros_like(raw), where=den > 0) * 5.0, 0, 10)
    x["cov"] = cov
    return x, ic


def seg_mean(d):
    return d[d.seg == "IS"].net_pct.mean(), d[d.seg == "OOS"].net_pct.mean()


def m1(x):
    print(f"\n{'=' * 100}\n [M1] 전략 구성 — 6전략 각각의 기여 (강도 5.7 통과분 기준)\n{'=' * 100}")
    p = x[x.strength >= 5.7]
    bi, bo = seg_mean(p)
    print(f"  전체 {len(p):,}건 | IS {bi:+.3f} OOS {bo:+.3f}")
    print(f"  {'전략':20s}{'건수':>8s}{'비중':>7s}{'IS':>9s}{'OOS':>9s}{'제외시 IS':>11s}{'제외시 OOS':>11s}")
    rows = []
    for s, d in p.groupby("strategy"):
        si, so = seg_mean(d)
        rest = p[p.strategy != s]
        ri, ro = seg_mean(rest)
        rows.append((s, len(d), si, so, ri - bi, ro - bo))
        print(f"  {s:20s}{len(d):8,}{len(d) / len(p) * 100:6.1f}%{si:+9.3f}{so:+9.3f}{ri - bi:+11.3f}{ro - bo:+11.3f}")
    best = max(rows, key=lambda r: min(r[4], r[5]))
    print(f"  → 제외 시 IS·OOS 둘 다 가장 개선되는 전략: **{best[0]}** (IS {best[4]:+.3f}, OOS {best[5]:+.3f})"
          if min(best[4], best[5]) > 0 else "  → 빼서 양쪽 개선되는 전략 없음")


def m2(x):
    print(f"\n{'=' * 100}\n [M2] 강도 임계 곡선 — 5.7 이 최적인가\n{'=' * 100}")
    print(f"  {'임계':>6s}{'통과율':>8s}{'IS':>9s}{'OOS':>9s}{'100신호당 총수익(IS/OOS)':>26s}")
    for T in (5.0, 5.3, 5.5, 5.7, 6.0, 6.3, 6.6, 7.0):
        d = x[x.strength >= T]
        if len(d) < 200:
            continue
        si, so = seg_mean(d)
        r = len(d) / len(x)
        print(f"  {T:6.1f}{r * 100:7.1f}%{si:+9.3f}{so:+9.3f}{si * r * 100:13.2f}{so * r * 100:13.2f}")


def m3(x):
    print(f"\n{'=' * 100}\n [M3] 보유기간 — 20일 고정이 최적인가 (전략 정의상 기간이 다르므로 실측 분포로 확인)\n{'=' * 100}")
    p = x[x.strength >= 5.7].copy()
    if "holding_days" in p.columns:
        g = p.groupby("holding_days").net_pct.agg(["mean", "size"])
        g = g[g["size"] >= 200]
        print(f"  {'보유일':>7s}{'건수':>9s}{'평균net':>10s}{'일평균':>9s}")
        for k, r in g.iterrows():
            print(f"  {int(k):7d}{int(r['size']):9,}{r['mean']:+10.3f}{r['mean'] / max(int(k), 1):+9.4f}")
    else:
        print("  holding_days 컬럼 없음")


def m4(x):
    print(f"\n{'=' * 100}\n [M4] 예측력 상한 — 완벽한 선별 대비 현재 실현률\n{'=' * 100}")
    for seg in ("IS", "OOS"):
        d = x[x.seg == seg]
        n_pass = int((d.strength >= 5.7).sum())
        cur = d[d.strength >= 5.7].net_pct.mean()
        perfect = d.net_pct.nlargest(n_pass).mean()      # 사후 최고 n개
        worst = d.net_pct.nsmallest(n_pass).mean()
        base = d.net_pct.mean()
        rng = perfect - base
        print(f"  [{seg}] 통과 {n_pass:,}건 | 무필터 {base:+.3f} | 현재 {cur:+.3f} | "
              f"완벽선별 {perfect:+.3f} | 최악선별 {worst:+.3f}")
        print(f"        현재가 잡은 몫: {(cur - base) / rng * 100:.1f}% "
              f"(무필터→완벽 구간 {rng:.2f}%p 중 {cur - base:.2f}%p)")


def m5(x, ic):
    print(f"\n{'=' * 100}\n [M5] 남은 결측 — 어느 피처를 채우면 얼마를 되찾나\n{'=' * 100}")
    tot = sum(abs(v) for v in ic.values())
    rows = []
    for f, w in ic.items():
        col = "crd_remn_rt_y" if (f == "crd_remn_rt" and "crd_remn_rt_y" in x.columns) else f
        covr = pd.to_numeric(x[col], errors="coerce").notna().mean()
        lost = abs(w) / tot * 100 * (1 - covr)
        rows.append((lost, f, covr * 100, abs(w) / tot * 100))
    rows.sort(reverse=True)
    print(f"  {'피처':22s}{'IC질량%':>9s}{'채움률':>8s}{'손실질량%':>10s}")
    for lost, f, covr, mass in rows[:8]:
        lab = FEATURE_META.get(f, (f, 0))[0]
        print(f"  {lab:22s}{mass:8.1f}%{covr:7.1f}%{lost:10.2f}")
    print(f"  합계 손실 {sum(r[0] for r in rows):.1f}% (§32 이전 18.9% → 현재)")


def m6(x):
    print(f"\n{'=' * 100}\n [M6] 회전율 — 슬롯 제약이 얼마나 깎는가\n{'=' * 100}")
    p = x[x.strength >= 5.7]
    per_day = p.groupby("entry_date").size()
    print(f"  강도 통과 신호: 총 {len(p):,}건 / {p.entry_date.nunique():,} 거래일 = 하루 평균 {per_day.mean():.1f}건")
    print(f"  하루 10건 초과인 날: {(per_day > 10).mean() * 100:.1f}% | 그 날들의 평균 신호 {per_day[per_day > 10].mean():.1f}건")
    over = int((per_day - 10).clip(lower=0).sum())
    print(f"  10슬롯이면 버려지는 신호 누적 {over:,}건 ({over / len(p) * 100:.1f}%)")
    print(f"  → 슬롯을 늘리면 이만큼 더 담지만, 자본이 분산돼 건당 비중이 줄어든다(수익률 아닌 총액 문제)")


def main():
    x, ic = load()
    print(f"라이브 6전략 {len(x):,}건 | 강도 5.7 통과 {int((x.strength >= 5.7).sum()):,}건 "
          f"({(x.strength >= 5.7).mean() * 100:.1f}%)")
    m1(x); m2(x); m3(x); m4(x); m5(x, ic); m6(x)


if __name__ == "__main__":
    main()
