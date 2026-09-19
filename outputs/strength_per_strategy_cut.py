# -*- coding: utf-8 -*-
"""
strength_per_strategy_cut.py — 전략별 '고정' 임계 (배포 가능한 형태) (2026-09-18)

앞선 두 검정의 결과
  · 절대 단일 임계(5.7) : 무작위 대비 평균 56% (p=0.09, 유의하지 않음). 6전략 중 4개를 통과율 0%로 차단.
  · 전략별 상대 임계     : 무작위 대비 평균 72.7% (22/24가 50 초과, p<0.0001). 6전략 전부 유지.

그런데 상대 임계를 **그대로 라이브에 쓸 수는 없다.** '올해 신호의 상위 X%'는
그 해가 끝나야 알 수 있고(미래 참조), 롤링 창으로 바꾸면 **항상 상위 X%를 사게 되어**
절대 점수 수준이 담고 있는 국면 정보를 버린다 — 2026-08-11 에 시간축 백분위를 기각한
바로 그 이유다(최근60일 상위15% +1.08% vs 고정5.7 +1.94%).

그래서 배포 가능한 절충안을 검정한다:
  **전략별 고정 임계** = 각 전략의 '학습기간 점수 분포'에서 상위 X% 지점을 잘라
  그 값을 **상수로 고정**해 테스트 기간에 적용한다.
    - 전략 간 척도 차이(rsi_reversal 5.92 vs high_52w_filt 2.84)는 보정된다.
    - 임계가 고정이므로 나쁜 국면에서는 통과 건수가 실제로 줄어든다 = 국면 정보 유지.
    - 미래를 보지 않는다(임계는 테스트 연도 이전 데이터로만 산출).
  = '5.7 하나' 대신 '전략마다 제 눈금에 맞춘 5.7' 을 쓰는 것.

기준선은 동일한 무작위 30세트(자본곡선). 승률은 쓰지 않는다.

사용: python strength_per_strategy_cut.py [--seeds 30]
"""
import os
import sys
import argparse
import pickle

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

import strength_year_sim as S
import strength_threshold_study as ST

TOPS = [80, 60, 50, 40, 30, 20, 10]
WINDOWS = {"2025년": ("20250101", "20251231"),
           "2026년": ("20260101", "20261231"),
           "2025~현재": ("20250101", "20261231")}


def fixed_cuts(top):
    """전략별 고정 임계 — 테스트 연도 Y 는 Y 이전 매매의 상위 top% 지점을 상수로 쓴다.

    반환: {(연도, live_strategy): 점수컷}  및 진단용 표.
    """
    h = pd.read_csv("trades_history_v3.csv", low_memory=False, dtype={"code": str})
    h["ed"] = h["entry_date"].astype(str).str.replace("-", "").str[:8]
    h["code"] = h["code"].astype(str).str.zfill(6)
    cuts, diag = {}, []
    for yr in ("2025", "2026"):
        train = h[h["ed"] < f"{yr}0101"]
        ic, ref = S.fit_scorer(train)
        tr = train.copy()
        tr["sc"] = S.score_rows(tr, ic, ref)
        for nm, g in tr.groupby("strategy"):
            if nm not in S.HOLD and nm != "high_52w_filt":
                continue
            v = g["sc"].dropna()
            if len(v) < 200:
                continue
            c = float(np.percentile(v, 100 - top))
            cuts[(yr, nm)] = c
            diag.append((yr, nm, len(v), float(v.mean()), c))
    return cuts, diag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    a = ap.parse_args()

    print("=" * 94)
    print("  전략별 고정 임계 — '5.7 하나' 대신 '전략마다 제 눈금에 맞춘 5.7'")
    print("=" * 94)
    T, price_map, tdates, panel, elig = ST.load_all()
    T = ST.walkforward_scores(T)
    T["yr"] = T["entry_date"].str[:4]

    rcache = ST.CACHE.replace(".pkl", "_rnd.pkl")
    rnds = []
    with open(rcache, "rb") as f:
        have = pickle.load(f)
    for s in sorted(have)[:a.seeds]:
        R = have[s].copy()
        R["yr"] = R["entry_date"].str[:4]
        R["live_strategy"] = R["strategy"].map(lambda x: S.ENGINE_TO_LIVE.get(x, x))
        rnds.append(R)
    print(f"  무작위 대조 {len(rnds)}세트 (동일 캐시)")
    sim = S._load_sim()

    def combo(Q):
        vals = [r["ret"] for acct in S.ACCOUNTS
                if (r := ST.port(sim, Q, acct, price_map, tdates))]
        return (np.mean([1 + v / 100 for v in vals]) * 100 - 100) if vals else None

    def apply_cut(X, cuts):
        k = list(zip(X["yr"], X["live_strategy"]))
        th = np.array([cuts.get(kk, np.inf) for kk in k])
        return X[X["score"].to_numpy() >= th]

    # 임계표 먼저 보여준다 — 전략마다 얼마나 다른지가 이 검정의 핵심
    _, diag = fixed_cuts(30)
    print("\n  [상위 30% 기준 전략별 고정 임계] — 단일 5.7 이 왜 불공정한지")
    print(f"    {'연도':>5s} {'전략':>22s} {'학습건수':>8s} {'학습평균':>8s} {'임계':>7s}")
    for yr, nm, n, mu, c in sorted(diag):
        print(f"    {yr:>5s} {nm:>22s} {n:>8,} {mu:>8.2f} {c:>7.2f}")

    for wname, (lo, hi) in WINDOWS.items():
        W = T[(T["entry_date"] >= lo) & (T["entry_date"] <= hi) & T["score"].notna()]
        if not len(W):
            continue
        print("\n" + "=" * 94)
        print(f"  ■ {wname}   채점된 매매 {len(W):,}건")
        print("=" * 94)
        print(f"  {'기준':>16s} {'통과':>7s} {'평균%':>7s} {'전략':>4s} {'키움':>6s} {'KIS':>5s} | "
              f"{'합산%':>7s} | {'무작위중앙':>9s} {'범위':>12s} {'이김':>5s}")
        rows = []
        for top in TOPS:
            cuts, _ = fixed_cuts(top)
            Q = apply_cut(W, cuts)
            rows.append((f"전략별 상위{top}%", Q, cuts))
        rows.append(("절대 5.7(현행)", W[W["score"] >= 5.7], None))

        for lab, Q, cuts in rows:
            if not len(Q):
                print(f"  {lab:>16s} {0:>7,}  — 통과 없음")
                continue
            c = combo(Q)
            rc = []
            for R in rnds:
                RQ = R[(R["entry_date"] >= lo) & (R["entry_date"] <= hi) & R["score"].notna()]
                RQ = apply_cut(RQ, cuts) if cuts else RQ[RQ["score"] >= 5.7]
                v = combo(RQ) if len(RQ) else None
                if v is not None:
                    rc.append(v)
            nk = ST.port(sim, Q, "키움 안C", price_map, tdates)
            nd = ST.port(sim, Q, "KIS 안D", price_map, tdates)
            med = float(np.median(rc)) if rc else None
            pct = float(np.mean([c > v for v in rc])) * 100 if (rc and c is not None) else None
            print(f"  {lab:>16s} {len(Q):>7,} {Q['net_pct'].mean():>+7.2f} "
                  f"{Q['strategy'].nunique():>4d} {(nk['n_exec'] if nk else 0):>6} "
                  f"{(nd['n_exec'] if nd else 0):>5} | {(f'{c:+.1f}' if c is not None else '-'):>7s} | "
                  f"{(f'{med:+.1f}' if med is not None else '-'):>9s} "
                  f"{(f'{min(rc):+.0f}~{max(rc):+.0f}' if rc else '-'):>12s} "
                  f"{(f'{pct:.0f}%' if pct is not None else '-'):>5s}")

    # 국면 정보가 유지되는지 — 고정 임계면 나쁜 장에서 통과가 줄어야 한다
    print("\n" + "=" * 94)
    print("  ■ 국면 정보 유지 확인 — 고정 임계면 나쁜 장에서 통과 건수가 실제로 줄어야 한다")
    print("=" * 94)
    cuts, _ = fixed_cuts(30)
    Q = apply_cut(T[T["score"].notna()], cuts)
    q = Q.assign(분기=Q["entry_date"].str[:4] + "Q" + ((Q["entry_date"].str[4:6].astype(int) - 1) // 3 + 1).astype(str))
    w = T[T["score"].notna()]
    w = w.assign(분기=w["entry_date"].str[:4] + "Q" + ((w["entry_date"].str[4:6].astype(int) - 1) // 3 + 1).astype(str))
    tab = pd.DataFrame({"전체신호": w.groupby("분기").size(), "통과": q.groupby("분기").size()}).fillna(0)
    tab["통과율%"] = (tab["통과"] / tab["전체신호"] * 100).round(1)
    print(tab.loc[tab.index >= "2025Q1"].to_string())
    print("\n  ※ 통과율이 분기마다 달라야 '국면 정보 유지'다. 일정하면 시간축 백분위와 같아져")
    print("     2026-08-11 에 기각된 '항상 상위 X% 를 산다'와 다를 바 없게 된다.")


if __name__ == "__main__":
    main()
