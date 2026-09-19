# -*- coding: utf-8 -*-
"""
strength_threshold_study.py — 강도 임계 전수 검토 (2025년 기준, 다각도) (2026-09-18)

질문: 2025년 이후 기준으로 강도 임계를 0~6.5 까지 훑으면 어떤 값이 가장 좋은가?

§38(최근 1년)에서 임계 스윕이 **단조가 아니었다**(5.0 −14.3 → 5.5 −20.0 → 6.5 +10.9).
단일 경로에서 나온 비단조 곡선은 노이즈와 구분되지 않는다. 그래서 이번엔 각도를 넓힌다.

 ① 연도별 워크포워드 — 테스트 연도 Y 의 IC·백분위 기준분포를 **Y 이전 데이터로만** 산출.
    (창 전체를 한 번에 채점하면 2026년 매매를 2026년 정보로 고른 셈이 된다)
 ② 기간 3분할 — 2025 / 2026 / 합산. 같은 임계가 두 해 모두에서 좋아야 신호다.
 ③ **무작위 대조를 자본곡선까지** — 같은 날·같은 전략 라벨·같은 유동성 조건의 무작위
    종목으로 트레이드를 통째로 바꿔 끼우고 **같은 슬롯 구조로 시뮬**한다.
    매매 단위 알파가 아니라 '포트폴리오가 무작위보다 나은가'를 직접 본다. 이게 결정적이다.
 ④ 분기별 안정성 — 임계별 수익이 특정 분기 하나에서 나온 것인지 확인.
 ⑤ 참여 전략 수 — 임계가 올라가며 전략이 꺼지는 정도(다양성 상실)를 같이 본다.

승률은 판단 근거로 쓰지 않는다(익절 규칙의 산물). 평균 net% 와 자본곡선을 분리해 본다.

사용: python strength_threshold_study.py [--seeds 5]
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

import strength_year_sim as S          # 청산 재계산·채점·라이브 구성 재사용(검산 완료본)
from strategy_engine import ALL_STRATEGIES, DEFAULT_COSTS
from strategies.daily_loader import load_macro_daily
from capital_simulator import build_price_lookup

CACHE = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp", "claude",
                     "C--fin", "_thr_study_cache.pkl")
THRESHOLDS = [0.0, 5.0, 5.2, 5.5, 5.7, 6.0, 6.3, 6.5]
WINDOWS = {                    # 이름: (시작, 끝)  — 진입일 기준
    "2025년":      ("20250101", "20251231"),
    "2026년":      ("20260101", "20261231"),
    "2025~현재":   ("20250101", "20261231"),
}


def load_all():
    """일봉·전략 백테스트·재청산 — 무거우므로 캐시."""
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as f:
                d = pickle.load(f)
            print(f"  캐시 사용: {CACHE}")
            return d["T"], d["price_map"], d["tdates"], d["panel"], d["elig"]
        except Exception:
            pass

    df = load_macro_daily().reset_index(drop=True)
    df["code"] = df["code"].astype(str).str.zfill(6)
    mkt = df.groupby("date")["change_pct"].mean()
    df["mkt_strong"] = df["date"].map(
        (mkt.rolling(60, min_periods=60).mean() > 0).to_dict()).fillna(False)
    price_map, tdates = build_price_lookup(df)
    panel = S.build_panel(df)

    live = {s.name: s for s in ALL_STRATEGIES
            if s.name in {n for c in S.ACCOUNTS.values() for n in c}}
    rows = []
    for acct, caps in S.ACCOUNTS.items():
        for nm in caps:
            for t in live[nm].backtest(df.copy(), DEFAULT_COSTS):
                ed = str(t.entry_date)
                if len(ed) != 8 or not t.entry_price or t.entry_price <= 0 or ed < "20240101":
                    continue
                r = S.reexit(panel, str(t.code).zfill(6), ed, float(t.entry_price),
                             S.HOLD[nm], S.STOP.get(nm), S.TAKE.get(nm))
                if r:
                    rows.append({"acct": acct, "strategy": nm, "code": str(t.code).zfill(6),
                                 "entry_date": ed, "exit_date": str(r[2]),
                                 "entry_price": float(t.entry_price),
                                 "net_pct": r[0], "why": r[1]})
    T = pd.DataFrame(rows)

    # 무작위 대조용 자격 풀: 날짜별 (code, trading_value)
    sub = df[df["date"] >= "20240101"]
    elig = {d: g[["code", "trading_value"]].to_numpy(object) for d, g in sub.groupby("date")}

    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump({"T": T, "price_map": price_map, "tdates": tdates,
                     "panel": panel, "elig": elig}, f)
    return T, price_map, tdates, panel, elig


def walkforward_scores(T):
    """연도별 워크포워드 채점 — 테스트 연도 Y 는 Y 이전 매매로만 학습."""
    h = pd.read_csv("trades_history_v3.csv", low_memory=False, dtype={"code": str})
    h["ed"] = h["entry_date"].astype(str).str.replace("-", "").str[:8]
    h["code"] = h["code"].astype(str).str.zfill(6)
    out = {}
    for yr in ("2025", "2026"):
        train = h[h["ed"] < f"{yr}0101"]
        ic, ref = S.fit_scorer(train)
        test = h[(h["ed"] >= f"{yr}0101") & (h["ed"] < f"{int(yr)+1}0101")].copy()
        test["sc"] = S.score_rows(test, ic, ref)
        for k, v in zip(zip(test["ed"], test["code"], test["strategy"]), test["sc"]):
            out[k] = v
        top = ", ".join(f"{k}{v:+.3f}" for k, v in
                        sorted(ic.items(), key=lambda kv: -abs(kv[1]))[:3])
        print(f"  {yr}년 테스트 ← 학습 {len(train):,}건 / IC {len(ic)}개 / 상위 {top}")
    T["live_strategy"] = T["strategy"].map(lambda s: S.ENGINE_TO_LIVE.get(s, s))
    T["score"] = [out.get((e, c, s), np.nan)
                  for e, c, s in zip(T["entry_date"], T["code"], T["live_strategy"])]
    return T


def make_random(T, panel, elig, seed):
    """같은 날·같은 전략 라벨·같은 유동성 조건의 무작위 종목으로 통째 교체.

    점수는 '대체된 원래 매매의 점수'를 그대로 물려받는다 → 임계 필터가 동일하게 걸리므로
    각 임계에서 '같은 날 같은 개수'가 보장된다(무작위 대조의 전제).
    """
    rs = np.random.RandomState(9000 + seed)
    rows = []
    for r in T.itertuples():
        pool = elig.get(r.entry_date)
        if pool is None:
            continue
        need = S.MIN_TV[r.strategy]
        cand = [c for c, tv in pool if tv and float(tv) >= need]
        if not cand:
            continue
        for _ in range(6):
            c = cand[rs.randint(len(cand))]
            p = panel.get(c)
            if p is None:
                continue
            i = np.searchsorted(p[0], r.entry_date)
            if i >= len(p[0]) or p[0][i] != r.entry_date or not p[1][i] or p[1][i] <= 0:
                continue
            rr = S.reexit(panel, c, r.entry_date, float(p[1][i]), S.HOLD[r.strategy],
                          S.STOP.get(r.strategy), S.TAKE.get(r.strategy))
            if rr:
                rows.append({"acct": r.acct, "strategy": r.strategy, "code": c,
                             "entry_date": r.entry_date, "exit_date": str(rr[2]),
                             "entry_price": float(p[1][i]), "net_pct": rr[0],
                             "score": r.score})
                break
    return pd.DataFrame(rows)


def port(sim, Q, acct, price_map, tdates):
    sub = Q[Q["acct"] == acct][["entry_date", "exit_date", "code", "strategy",
                                "entry_price", "net_pct", "score"]]
    if not len(sub):
        return None
    return sim(sub, price_map, tdates, S.ACCOUNTS[acct], S.PRIORITY[acct], None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5, help="무작위 대조 세트 수")
    a = ap.parse_args()

    print("=" * 96)
    print("  강도 임계 전수 검토 — 2025년 기준, 연도별 워크포워드 + 무작위 자본곡선 대조")
    print("=" * 96)
    print("\n[1/4] 데이터·백테스트·재청산")
    T, price_map, tdates, panel, elig = load_all()
    print(f"  재청산 매매 {len(T):,}건 | {T.entry_date.min()}~{T.entry_date.max()}")

    print("[2/4] 연도별 워크포워드 채점")
    T = walkforward_scores(T)
    print(f"  채점 매칭 {T['score'].notna().mean()*100:.1f}% (무기록은 fail-closed)")

    print(f"[3/4] 무작위 대조군 {a.seeds}세트 (자본곡선용)")
    rnds = []
    rcache = CACHE.replace(".pkl", "_rnd.pkl")
    have = {}
    if os.path.exists(rcache):
        try:
            with open(rcache, "rb") as f:
                have = pickle.load(f)
            print(f"  캐시에 {len(have)}세트 있음")
        except Exception:
            have = {}
    for s in range(a.seeds):
        if s in have:
            rnds.append(have[s])
            continue
        R = make_random(T, panel, elig, s)
        have[s] = R
        rnds.append(R)
        print(f"  세트 {s+1}/{a.seeds} 생성: {len(R):,}건", flush=True)
        with open(rcache, "wb") as f:      # 매 세트마다 저장 — 중단돼도 이어서 간다
            pickle.dump(have, f)

    print("[4/4] 임계 스윕\n")
    sim = S._load_sim()

    for wname, (lo, hi) in WINDOWS.items():
        W = T[(T["entry_date"] >= lo) & (T["entry_date"] <= hi)]
        if not len(W):
            continue
        print("=" * 96)
        print(f"  ■ {wname}  ({lo}~{min(hi, T.entry_date.max())})   창 전체 매매 {len(W):,}건")
        print("=" * 96)
        cov = W["score"].notna().mean() * 100
        print(f"  채점 커버리지 {cov:.1f}%  (임계 0 행은 미채점분 포함 — 그 외는 fail-closed)")
        print(f"  {'임계':>5s} {'통과':>7s} {'평균%':>7s} {'전략':>4s} | "
              f"{'키움 체결':>8s} {'수익%':>8s} {'MDD%':>7s} | {'KIS 체결':>8s} {'수익%':>8s} {'MDD%':>7s} | "
              f"{'합산%':>7s} | {'무작위중앙':>8s} {'무작위범위':>13s} {'이김':>6s}")
        best = []
        for th in THRESHOLDS:
            Q = W[W["score"] >= th] if th > 0 else W
            if not len(Q):
                continue
            cells, rets = [], {}
            for acct in S.ACCOUNTS:
                r = port(sim, Q, acct, price_map, tdates)
                rets[acct] = r["ret"] if r else None
                cells.append(f" {(r['n_exec'] if r else 0):>8} {(r['ret'] if r else '-'):>8} "
                             f"{(r['mdd'] if r else '-'):>7} |")
            vals = [v for v in rets.values() if v is not None]
            comb = np.mean([1 + v / 100 for v in vals]) * 100 - 100 if vals else None

            rcombs = []
            for R in rnds:
                RQ = R[(R["entry_date"] >= lo) & (R["entry_date"] <= hi)]
                RQ = RQ[RQ["score"] >= th] if th > 0 else RQ
                rv = []
                for acct in S.ACCOUNTS:
                    rr = port(sim, RQ, acct, price_map, tdates)
                    if rr:
                        rv.append(rr["ret"])
                if rv:
                    rcombs.append(np.mean([1 + v / 100 for v in rv]) * 100 - 100)
            nstrat = Q["strategy"].nunique()
            if rcombs and comb is not None:
                rmed = float(np.median(rcombs))
                # 백분위 = 무작위 분포에서 전략이 몇 %를 이겼나. 50%면 무작위와 구분 불가.
                pct = float(np.mean([comb > v for v in rcombs])) * 100
                rng = f"{min(rcombs):+.0f}~{max(rcombs):+.0f}"
                print(f"  {th:5.1f} {len(Q):>7,} {Q['net_pct'].mean():>+7.2f} {nstrat:>4d} |"
                      + "".join(cells)
                      + f" {comb:>+7.1f} | {rmed:>+8.1f} {rng:>13s} {pct:>6.0f}%")
                best.append((pct, comb - rmed, comb, th, nstrat))
            else:
                print(f"  {th:5.1f} {len(Q):>7,} {Q['net_pct'].mean():>+7.2f} {nstrat:>4d} |"
                      + "".join(cells)
                      + f" {(f'{comb:+.1f}' if comb is not None else '-'):>7s} | {'-':>8s} {'-':>13s} {'-':>6s}")
        if best:
            best.sort(reverse=True)
            p, e, c, th, ns = best[0]
            print(f"\n  → 무작위 분포를 가장 많이 이긴 임계: **{th}** "
                  f"(합산 {c:+.1f}%, 중앙 대비 {e:+.1f}%p, 무작위 {p:.0f}% 를 이김, 참여전략 {ns})")
            if p < 80:
                print(f"     ⚠ 최고값조차 무작위의 {p:.0f}% 밖에 못 이겼다 — 신호로 보기 어렵다"
                      f"(우연이면 기대 50%).")
        print()

    # ── 분기별 안정성 ───────────────────────────────────────────────────
    print("=" * 96)
    print("  ■ 분기별 안정성 — 한 분기가 전체를 만든 것은 아닌가 (합산 수익%)")
    print("=" * 96)
    qs = [("25Q1", "20250101", "20250331"), ("25Q2", "20250401", "20250630"),
          ("25Q3", "20250701", "20250930"), ("25Q4", "20251001", "20251231"),
          ("26Q1", "20260101", "20260331"), ("26Q2", "20260401", "20260630"),
          ("26Q3", "20260701", "20260918")]
    print(f"  {'임계':>5s} " + " ".join(f"{q[0]:>7s}" for q in qs) + f" {'양수분기':>8s}")
    for th in THRESHOLDS:
        cells, npos = [], 0
        for _, lo, hi in qs:
            Q = T[(T["entry_date"] >= lo) & (T["entry_date"] <= hi)]
            Q = Q[Q["score"] >= th] if th > 0 else Q
            vals = [r["ret"] for acct in S.ACCOUNTS
                    if (r := port(sim, Q, acct, price_map, tdates))]
            if vals:
                v = np.mean([1 + x / 100 for x in vals]) * 100 - 100
                cells.append(f"{v:>+7.1f}")
                npos += (v > 0)
            else:
                cells.append(f"{'-':>7s}")
        print(f"  {th:5.1f} " + " ".join(cells) + f" {npos:>6d}/7")

    print("\n  ※ 승률은 익절 규칙의 산물이므로 판단에 쓰지 않았다. 평균 net% 와 자본곡선은")
    print("     서로 다른 방향으로 움직일 수 있으므로 반드시 분리해 본다.")


if __name__ == "__main__":
    main()
