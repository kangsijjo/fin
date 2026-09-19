# -*- coding: utf-8 -*-
"""
strength_relative_threshold.py — 전략별 상대 임계 (2026-09-18)

§38 에서 드러난 문제: 강도점수는 중립적 품질 지표가 아니라 **역추세 선호 필터**다.
IC 최상위가 rsi_db −0.15 / bb_pct_db −0.15 이라 RSI 가 낮을수록 고득점한다.
결과로 전략별 평균 강도가 rsi_reversal 5.92 vs high_52w_filt 2.84 로 3점 넘게 벌어지고,
**단일 절대 임계 5.7 은 모멘텀 계열 4전략을 통과율 0% 로 영구 차단**한다.

그래서 검정한다: 절대 임계 대신 **전략별 자기 분포의 상위 X%** 를 쓰면?
  → 모든 전략이 '자기 기준으로 좋은 것'만 사게 되므로 다양성을 유지한 채 선별할 수 있다.

⚠ 2026-08-11 에 기각된 '백분위 전환'과는 다른 것이다. 그때 기각된 건
   **시간축 전역 백분위**(최근 60일 신호의 상위 15%)로, 절대 점수 수준이 담은 국면
   정보를 버린다는 이유였다. 여기서 보는 건 **전략축 백분위**(같은 전략끼리만 비교)이고,
   전략 간 척도 차이라는 구조적 편향을 보정하는 것이 목적이라 성격이 다르다.
   국면 정보는 유지된다 — 나쁜 장에서는 전략 자체의 신호가 줄기 때문이다.

기준선은 항상 무작위 진입(자본곡선). 승률은 쓰지 않는다.

사용: python strength_relative_threshold.py [--seeds 20]
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

WINDOWS = {"2025년": ("20250101", "20251231"),
           "2026년": ("20260101", "20261231"),
           "2025~현재": ("20250101", "20261231")}
TOPS = [100, 80, 60, 50, 40, 30, 20, 10]      # 전략별 상위 X%


def add_rank(T):
    """전략별·연도별 강도 백분위(0~100, 높을수록 강함).

    연도로 끊는 이유: 전략의 점수 분포 자체가 해마다 이동하므로(2026 평균이 낮다)
    전 기간 한 덩어리로 순위를 매기면 2026 매매가 통째로 하위권이 된다 = 시간축 편향.
    """
    T = T.copy()
    T["yr"] = T["entry_date"].str[:4]
    T["rank_pct"] = (T.groupby(["strategy", "yr"])["score"]
                      .rank(pct=True, method="average") * 100)
    return T


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    a = ap.parse_args()

    print("=" * 92)
    print("  전략별 상대 임계(자기 분포 상위 X%) vs 절대 임계 5.7")
    print("=" * 92)
    T, price_map, tdates, panel, elig = ST.load_all()
    T = ST.walkforward_scores(T)
    T = add_rank(T)

    rcache = ST.CACHE.replace(".pkl", "_rnd.pkl")
    rnds = []
    if os.path.exists(rcache):
        with open(rcache, "rb") as f:
            have = pickle.load(f)
        for s in sorted(have)[:a.seeds]:
            R = add_rank(have[s])
            rnds.append(R)
        print(f"  무작위 대조 {len(rnds)}세트 (캐시)")
    sim = S._load_sim()

    def combo(Q):
        vals = [r["ret"] for acct in S.ACCOUNTS
                if (r := ST.port(sim, Q, acct, price_map, tdates))]
        return (np.mean([1 + v / 100 for v in vals]) * 100 - 100) if vals else None

    for wname, (lo, hi) in WINDOWS.items():
        W = T[(T["entry_date"] >= lo) & (T["entry_date"] <= hi) & T["score"].notna()]
        if not len(W):
            continue
        print("\n" + "=" * 92)
        print(f"  ■ {wname}   채점된 매매 {len(W):,}건")
        print("=" * 92)
        print(f"  {'기준':>14s} {'통과':>7s} {'평균%':>7s} {'전략':>4s} {'키움체결':>7s} {'KIS체결':>7s} | "
              f"{'합산%':>7s} | {'무작위중앙':>9s} {'범위':>13s} {'이김':>5s}")

        rows = []
        for top in TOPS:
            Q = W[W["rank_pct"] >= (100 - top)]
            c = combo(Q)
            rc = []
            for R in rnds:
                RQ = R[(R["entry_date"] >= lo) & (R["entry_date"] <= hi) & R["score"].notna()]
                RQ = RQ[RQ["rank_pct"] >= (100 - top)]
                v = combo(RQ)
                if v is not None:
                    rc.append(v)
            rows.append(("상위 %d%%" % top, Q, c, rc))
        # 비교군: 현행 절대 5.7
        Q57 = W[W["score"] >= 5.7]
        rc57 = []
        for R in rnds:
            RQ = R[(R["entry_date"] >= lo) & (R["entry_date"] <= hi) & R["score"].notna()]
            RQ = RQ[RQ["score"] >= 5.7]
            v = combo(RQ)
            if v is not None:
                rc57.append(v)
        rows.append(("절대 5.7(현행)", Q57, combo(Q57), rc57))

        for lab, Q, c, rc in rows:
            nk = ST.port(sim, Q, "키움 안C", price_map, tdates)
            nd = ST.port(sim, Q, "KIS 안D", price_map, tdates)
            med = float(np.median(rc)) if rc else None
            pct = float(np.mean([c > v for v in rc])) * 100 if (rc and c is not None) else None
            print(f"  {lab:>14s} {len(Q):>7,} {Q['net_pct'].mean():>+7.2f} "
                  f"{Q['strategy'].nunique():>4d} {(nk['n_exec'] if nk else 0):>7} "
                  f"{(nd['n_exec'] if nd else 0):>7} | "
                  f"{(f'{c:+.1f}' if c is not None else '-'):>7s} | "
                  f"{(f'{med:+.1f}' if med is not None else '-'):>9s} "
                  f"{(f'{min(rc):+.0f}~{max(rc):+.0f}' if rc else '-'):>13s} "
                  f"{(f'{pct:.0f}%' if pct is not None else '-'):>5s}")

    # ── 전략별 참여 확인 — 상대 임계가 정말 다양성을 지키나 ────────────────
    print("\n" + "=" * 92)
    print("  ■ 전략별 체결 분포 — 절대 임계는 다양성을 버리고, 상대 임계는 지키는가")
    print("=" * 92)
    W = T[(T["entry_date"] >= "20250101") & T["score"].notna()]
    for lab, Q in (("절대 5.7", W[W["score"] >= 5.7]),
                   ("상위 30%", W[W["rank_pct"] >= 70]),
                   ("상위 50%", W[W["rank_pct"] >= 50])):
        cnt = {}
        for acct in S.ACCOUNTS:
            sub = Q[Q["acct"] == acct]
            if not len(sub):
                continue
            for nm, g in sub.groupby("strategy"):
                cnt[S.ENGINE_TO_LIVE.get(nm, nm)] = len(g)
        print(f"  {lab:>9s}: " + "  ".join(f"{k} {v:,}" for k, v in sorted(cnt.items())))


if __name__ == "__main__":
    main()
