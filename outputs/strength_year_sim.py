# -*- coding: utf-8 -*-
"""
strength_year_sim.py — "최근 1년, 강도 5.7 이상만 사서 손절/만기에 팔았으면?" (2026-09-18)

질문: 현재 라이브 6전략으로 강도(score_ic) >= 5.7 종목만 매매하고
      손절선 또는 만기선에서 청산했을 때 최근 1년 수익률은?

이 스크립트가 지키는 것 (backtest-validation-protocol)
  1) **라이브 등가 포트폴리오** — 전략별 슬롯 상한(4/4/2)으로 시뮬한다.
     공용 10슬롯으로 풀링하면 신호가 압도적인 rsi_reversal 이 포트폴리오를 점령해
     결론이 뒤집힌다(2026-09-13 실측: 같은 개입이 '+7% 개선' ↔ '악화'로 반전).
  2) **워크포워드 채점** — 강도의 IC·백분위 기준분포를 **창 시작 이전 데이터로만** 만든다.
     전체 기간으로 채점하면 미래를 보고 고른 셈이 되어 성과가 부풀려진다.
  3) **무작위 진입 대조** — 같은 날·같은 개수·같은 자격조건 무작위 종목에 **똑같은 청산
     규칙**을 적용해 비교한다. 알파 = 전략 − 무작위. 승률은 판단 근거로 쓰지 않는다
     (익절 규칙이 있으면 승률은 자동으로 높아진다 — 86% 착시).
  4) **갭 보정** — 손절선을 찍는 날은 대개 시가부터 그 아래로 열린다.
     체결가 = min(손절가, 당일 시가). '손절 -10% = 최대 손실 10%'는 성립하지 않는다.

라이브 청산 규칙(그대로 재현)
  키움 안C  high_52w_filt 익절 +50% / rsi_vol 익절 +20% / rsi_reversal 없음   손절 없음
  KIS  안D  h52w_for3d_mkt -15% / for_high20_mkt -10% / gc_for3d -26%          익절 없음
  공통      만기 = 진입 후 holding_days 일째 종가. 비용 0.245%(수수료+세금+슬리피지).

사용: python strength_year_sim.py [--start YYYYMMDD] [--min-strength 5.7] [--seeds 10]
"""
import os
import sys
import argparse

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from strategy_engine import ALL_STRATEGIES, DEFAULT_COSTS
from strategies.daily_loader import load_macro_daily
from capital_simulator import build_price_lookup
from factor_scorer import IC_FEATURES, MIN_IC_COVERAGE

# ── 라이브 구성 (kiwoom_trader / kis_trader 에서 그대로) ────────────────────
ENGINE_TO_LIVE = {"star_high_52w_20_filt": "high_52w_filt"}
ACCOUNTS = {
    "키움 안C": {"star_high_52w_20_filt": 4, "rsi_reversal": 4, "rsi_vol": 2},
    "KIS 안D":  {"h52w_for3d_mkt": 4, "for_high20_mkt": 4, "gc_for3d": 2},
}
PRIORITY = {
    "키움 안C": ["star_high_52w_20_filt", "rsi_reversal", "rsi_vol"],
    "KIS 안D":  ["h52w_for3d_mkt", "for_high20_mkt", "gc_for3d"],
}
STOP = {                      # kis_trader.STRATEGY_STOP (진입가 대비)
    "h52w_for3d_mkt": -0.15,
    "for_high20_mkt": -0.10,
    "gc_for3d":       -0.26,
}
TAKE = {                      # kiwoom_trader.PROFIT_TARGET
    "star_high_52w_20_filt": 0.50,
    "rsi_vol":               0.20,
}
HOLD = {"star_high_52w_20_filt": 20, "rsi_reversal": 5, "rsi_vol": 7,
        "h52w_for3d_mkt": 20, "for_high20_mkt": 20, "gc_for3d": 15}
MIN_TV = {"star_high_52w_20_filt": 3e9, "rsi_reversal": 1e9, "rsi_vol": 1e9,
          "h52w_for3d_mkt": 3e9, "for_high20_mkt": 3e9, "gc_for3d": 3e9}
COST = DEFAULT_COSTS["total_pct"]


# ══════════════════════════════════════════════════════════════════════════
# 1. 청산 재계산 — 손절(갭 보정) > 익절 > 만기
# ══════════════════════════════════════════════════════════════════════════
def build_panel(df):
    """종목별 (날짜 인덱스, open/high/low/close 배열) — 재청산 계산용."""
    panel = {}
    for code, g in df.sort_values(["code", "date"]).groupby("code"):
        panel[code] = (g["date"].to_numpy(),
                       g["open"].to_numpy(float), g["high"].to_numpy(float),
                       g["low"].to_numpy(float), g["close"].to_numpy(float))
    return panel


def reexit(panel, code, entry_date, entry_price, hold, stop_pct, take_pct):
    """라이브 청산 규칙으로 (net_pct, 사유, 청산일) 재계산. 실패 시 None.

    손절은 **일중 저가**가 손절선을 찍으면 발동하되, 체결가는 min(손절가, 당일 시가) —
    저가가 손절선을 찍는 날은 대개 시가부터 그 아래로 열리기 때문이다.
    익절은 일중 고가 기준, 체결가는 max(익절가, 당일 시가)(갭상승 시 시가 체결).
    같은 날 둘 다 닿으면 손절 우선(보수적).
    """
    p = panel.get(code)
    if p is None:
        return None
    dates, op, hi, lo, cl = p
    i = np.searchsorted(dates, entry_date)
    if i >= len(dates) or dates[i] != entry_date:
        return None
    last = min(i + hold - 1, len(dates) - 1)
    if last < i:
        return None
    sp = entry_price * (1 + stop_pct) if stop_pct is not None else None
    tp = entry_price * (1 + take_pct) if take_pct is not None else None
    for j in range(i, last + 1):
        if sp is not None and lo[j] <= sp:
            return (min(sp, op[j]) / entry_price - 1) * 100 - COST, "손절", dates[j]
        if tp is not None and hi[j] >= tp:
            return (max(tp, op[j]) / entry_price - 1) * 100 - COST, "익절", dates[j]
    return (cl[last] / entry_price - 1) * 100 - COST, "만기", dates[last]


# ══════════════════════════════════════════════════════════════════════════
# 2. 워크포워드 강도 채점 (factor_scorer.score_ic 재현)
# ══════════════════════════════════════════════════════════════════════════
def fit_scorer(train):
    """창 시작 이전 매매로만 IC·백분위 기준분포를 만든다."""
    ic, ref = {}, {}
    y = train["net_pct"].astype(float)
    for f in IC_FEATURES:
        if f not in train.columns:
            continue
        v = train[f].astype(float)
        m = v.notna() & y.notna()
        if m.sum() < 100:
            continue
        r, _ = spearmanr(v[m], y[m])
        if not np.isnan(r):
            ic[f] = float(r)
            ref[f] = np.sort(v[m].to_numpy())
    return ic, ref


def score_rows(x, ic, ref):
    """5.0 + Σ ic·(백분위−0.5) / denom · 5,  denom = available-only(커버리지 40% 이상)."""
    full = sum(abs(v) * 0.5 for v in ic.values())
    raw, avail = np.zeros(len(x)), np.zeros(len(x))
    for f, v in ic.items():
        if f not in x.columns:
            continue
        col = x[f].to_numpy(float)
        ok = ~np.isnan(col)
        if not ok.any():
            continue
        sv = ref[f]
        pct = np.searchsorted(sv, col[ok], side="left") / len(sv)
        raw[ok] += v * (pct - 0.5)
        avail[ok] += abs(v) * 0.5
    cov = np.divide(avail, full, out=np.zeros_like(avail), where=full > 0)
    denom = np.where((avail > 0) & (cov >= MIN_IC_COVERAGE), avail, full)
    return np.clip(5.0 + np.divide(raw, denom, out=np.zeros_like(raw), where=denom > 0) * 5.0,
                   0.0, 10.0)


# ══════════════════════════════════════════════════════════════════════════
# 3. 라이브 등가 포트폴리오 시뮬 (breaker_sweep.simulate 재사용)
# ══════════════════════════════════════════════════════════════════════════
def _load_sim():
    _argv = sys.argv
    sys.argv = ["breaker_sweep"]            # breaker_sweep 이 import 시 argparse 를 돈다
    try:
        from breaker_sweep import simulate
    finally:
        sys.argv = _argv
    return simulate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20250918", help="창 시작(진입일) YYYYMMDD")
    ap.add_argument("--min-strength", type=float, default=5.7)
    ap.add_argument("--seeds", type=int, default=10, help="무작위 대조 세트 수")
    a = ap.parse_args()

    print("=" * 78)
    print(f"  최근 1년 강도 {a.min_strength} 이상 매매 — 라이브 청산 규칙(손절/익절/만기)")
    print("=" * 78)

    print("\n[1/5] 일봉 로딩...")
    df = load_macro_daily().reset_index(drop=True)
    df["code"] = df["code"].astype(str).str.zfill(6)
    mkt = df.groupby("date")["change_pct"].mean()
    df["mkt_strong"] = df["date"].map(
        (mkt.rolling(60, min_periods=60).mean() > 0).to_dict()).fillna(False)
    price_map, tdates = build_price_lookup(df)
    panel = build_panel(df)
    print(f"  거래일 {len(tdates)}일 | {tdates[0]}~{tdates[-1]} | 종목 {df['code'].nunique():,}개")

    print("[2/5] 라이브 6전략 백테스트 → 청산 재계산...")
    live = {s.name: s for s in ALL_STRATEGIES
            if s.name in {n for c in ACCOUNTS.values() for n in c}}
    rows = []
    for acct, caps in ACCOUNTS.items():
        for nm in caps:
            ts = live[nm].backtest(df.copy(), DEFAULT_COSTS)
            n_re = 0
            for t in ts:
                ed = str(t.entry_date)
                if len(ed) != 8 or not t.entry_price or t.entry_price <= 0:
                    continue
                r = reexit(panel, str(t.code).zfill(6), ed, float(t.entry_price),
                           HOLD[nm], STOP.get(nm), TAKE.get(nm))
                if r is None:
                    continue
                net, why, xd = r
                rows.append({"acct": acct, "strategy": nm, "code": str(t.code).zfill(6),
                             "entry_date": ed, "exit_date": str(xd),
                             "entry_price": float(t.entry_price),
                             "net_pct": net, "why": why,
                             "engine_net": float(t.net_pct), "score": 0.0})
                n_re += 1
            print(f"  [{nm:24s}] {n_re:,}건  손절 {STOP.get(nm, '-')} 익절 {TAKE.get(nm, '-')} 만기 {HOLD[nm]}일")
    T = pd.DataFrame(rows)

    # 검산 — 손절·익절이 없는 전략은 엔진 값과 일치해야 한다(재계산 로직 자체의 검증)
    chk = T[T["strategy"] == "rsi_reversal"]
    d = (chk["net_pct"] - chk["engine_net"]).abs()
    print(f"\n  [검산] rsi_reversal(손절·익절 없음) 재계산 vs 엔진: "
          f"평균차 {d.mean():.4f}%p / 최대 {d.max():.3f}%p / 0.1%p 이내 {(d <= 0.1).mean()*100:.1f}%")

    print("[3/5] 워크포워드 채점...")
    hist = pd.read_csv("trades_history_v3.csv", low_memory=False, dtype={"code": str})
    hist["entry_date"] = hist["entry_date"].astype(str).str.replace("-", "").str[:8]
    hist["code"] = hist["code"].astype(str).str.zfill(6)
    train = hist[hist["entry_date"] < a.start]
    ic, ref = fit_scorer(train)
    print(f"  학습(창 이전) {len(train):,}건 → IC 피처 {len(ic)}개 "
          f"| 상위: " + ", ".join(f"{k}{v:+.3f}" for k, v in
                                  sorted(ic.items(), key=lambda kv: -abs(kv[1]))[:4]))
    feat = [c for c in IC_FEATURES if c in hist.columns]
    hist["_live"] = hist["strategy"].map(lambda s: s)
    hist["_sc"] = score_rows(hist, ic, ref)
    key = dict(zip(zip(hist["entry_date"], hist["code"], hist["strategy"]), hist["_sc"]))
    T["live_strategy"] = T["strategy"].map(lambda s: ENGINE_TO_LIVE.get(s, s))
    T["score"] = [key.get((e, c, s), np.nan)
                  for e, c, s in zip(T["entry_date"], T["code"], T["live_strategy"])]
    W = T[(T["entry_date"] >= a.start)].copy()
    print(f"  창({a.start}~) 매매 {len(W):,}건 | 채점 매칭 {W['score'].notna().mean()*100:.1f}% "
          f"(무기록은 fail-closed 로 제외 — 라이브 verify_strength 와 동일)")

    print("[4/5] 강도 필터 + 포트폴리오 시뮬...")
    simulate = _load_sim()
    P = W[W["score"] >= a.min_strength].copy()
    print(f"  강도 >= {a.min_strength} 통과: {len(P):,}건 "
          f"(창 전체의 {len(P)/max(1,len(W))*100:.2f}%)")

    print("\n" + "=" * 78)
    print(f"  결과 — {a.start} 이후 진입분")
    print("=" * 78)
    print(f"\n[매매 단위] 통과 {len(P):,}건")
    if len(P):
        print(f"  평균 {P['net_pct'].mean():+.3f}%  중앙 {P['net_pct'].median():+.3f}%  "
              f"승률 {(P['net_pct']>0).mean()*100:.1f}%  최악 {P['net_pct'].min():+.1f}%")
        print("  청산 사유: " + " / ".join(
            f"{k} {v}건({v/len(P)*100:.0f}%)" for k, v in P["why"].value_counts().items()))
        print("\n  전략별")
        for nm, g in P.groupby("live_strategy"):
            print(f"    {nm:18s} {len(g):4d}건  평균 {g['net_pct'].mean():+7.3f}%  "
                  f"승률 {(g['net_pct']>0).mean()*100:5.1f}%  "
                  + " ".join(f"{k}{v}" for k, v in g['why'].value_counts().items()))

    # ── 무작위 진입 대조 ────────────────────────────────────────────────
    print(f"\n[무작위 대조] 같은 날·같은 개수·같은 유동성 조건, 청산 규칙 동일 — {a.seeds}세트")
    elig = {}
    for dte, g in df[df["date"] >= a.start].groupby("date"):
        elig[dte] = g[["code", "trading_value"]].to_numpy(object)
    rnd_means = []
    for sd in range(a.seeds):
        rs = np.random.RandomState(7000 + sd)
        nets = []
        for _, r in P.iterrows():
            pool = elig.get(r["entry_date"])
            if pool is None:
                continue
            cand = [c for c, tv in pool if tv and float(tv) >= MIN_TV[r["strategy"]]]
            if not cand:
                continue
            for _try in range(6):
                c = cand[rs.randint(len(cand))]
                pp = panel.get(c)
                if pp is None:
                    continue
                i = np.searchsorted(pp[0], r["entry_date"])
                if i >= len(pp[0]) or pp[0][i] != r["entry_date"] or pp[4][i] <= 0:
                    continue
                rr = reexit(panel, c, r["entry_date"], float(pp[1][i]), HOLD[r["strategy"]],
                            STOP.get(r["strategy"]), TAKE.get(r["strategy"]))
                if rr:
                    nets.append(rr[0])
                    break
        if nets:
            rnd_means.append(float(np.mean(nets)))
    if rnd_means and len(P):
        rm = float(np.mean(rnd_means))
        print(f"  무작위 평균 {rm:+.3f}%  (세트별 {min(rnd_means):+.2f} ~ {max(rnd_means):+.2f})")
        print(f"  **알파 = 전략 {P['net_pct'].mean():+.3f}% − 무작위 {rm:+.3f}% "
              f"= {P['net_pct'].mean()-rm:+.3f}%p**")

    # ── 라이브 등가 자본곡선 ────────────────────────────────────────────
    print(f"\n[자본곡선] 계좌별 10슬롯·전략별 상한(4/4/2)·1,000만원 시작")
    print(f"  {'계좌':10s} {'매매':>5s} {'수익%':>9s} {'MDD%':>8s} {'샤프':>6s}")
    tot = {}
    for acct in ACCOUNTS:
        sub = P[P["acct"] == acct][["entry_date", "exit_date", "code", "strategy",
                                    "entry_price", "net_pct", "score"]].copy()
        if not len(sub):
            print(f"  {acct:10s} {0:>5d}  — 통과 매매 없음")
            tot[acct] = None
            continue
        r = simulate(sub, price_map, tdates, ACCOUNTS[acct], PRIORITY[acct], None, None)
        tot[acct] = r
        if r:
            print(f"  {acct:10s} {r['n_exec']:>5d} {r['ret']:>9} {r['mdd']:>8} {r['sharpe']:>6}")
    ok = [v for v in tot.values() if v]
    if len(ok) == 2:
        both = (1 + ok[0]["ret"] / 100) * 0.5 + (1 + ok[1]["ret"] / 100) * 0.5
        print(f"  {'합산(반반)':10s}       {(both-1)*100:>8.1f}%   ← 두 계좌에 자본을 반씩 넣었을 때")

    # ── 임계 스윕 — 5.7 이 좋은 선택인지 자체를 확인한다 ──────────────────
    print(f"\n[임계 스윕] 같은 창·같은 청산 규칙, 강도 임계만 바꿨을 때")
    print(f"  {'임계':>5s} {'통과매매':>7s} {'평균%':>7s} | "
          f"{'키움 수익%':>9s} {'MDD%':>7s} | {'KIS 수익%':>9s} {'MDD%':>7s}")
    for th in (0.0, 5.0, 5.5, 5.7, 6.0, 6.5):
        Q = W[W["score"] >= th] if th > 0 else W
        line = f"  {th:5.1f} {len(Q):>7,} {Q['net_pct'].mean():>+7.2f} |"
        for acct in ACCOUNTS:
            sub = Q[Q["acct"] == acct][["entry_date", "exit_date", "code", "strategy",
                                        "entry_price", "net_pct", "score"]]
            if not len(sub):
                line += f" {'-':>9s} {'-':>7s} |"
                continue
            r = simulate(sub, price_map, tdates, ACCOUNTS[acct], PRIORITY[acct], None, None)
            line += (f" {r['ret']:>9} {r['mdd']:>7} |" if r else f" {'-':>9s} {'-':>7s} |")
        print(line)

    # ── 라이브와의 채점 격차 — 백테스트 수치를 그대로 믿으면 안 되는 이유 ──
    try:
        L = pd.read_csv("db/signal_strength_log.csv", dtype={"signal_date": str})
        L["월"] = L["signal_date"].str[:6]
        L["sc"] = pd.to_numeric(L["score_ic"], errors="coerce")
        Wm = W.assign(월=W["entry_date"].str[:6])
        print(f"\n[캘리브레이션 점검] 같은 달, 백테스트 채점 vs 라이브 실측")
        print(f"  {'월':>7s} {'백테스트평균':>11s} {'라이브평균':>10s} {'격차':>7s} | "
              f"{'백테스트통과%':>12s} {'라이브통과%':>11s}")
        for m in sorted(set(L["월"]) & set(Wm["월"])):
            b, l = Wm[Wm["월"] == m]["score"], L[L["월"] == m]["sc"]
            if b.notna().sum() < 20 or l.notna().sum() < 20:
                continue
            print(f"  {m:>7s} {b.mean():>11.2f} {l.mean():>10.2f} {l.mean()-b.mean():>+7.2f} | "
                  f"{(b>=a.min_strength).mean()*100:>11.1f}% {(l>=a.min_strength).mean()*100:>10.1f}%")
        print("  ※ 라이브가 계통적으로 낮다 = 백테스트는 실제보다 **더 자주 산다**.")
    except Exception as e:
        print(f"  [캘리브레이션 점검 생략] {e}")

    print("\n[5/5] 저장")
    P.to_csv("strength_year_trades.csv", index=False, encoding="utf-8-sig")
    print(f"  통과 매매 내역 → strength_year_trades.csv ({len(P):,}행)")


if __name__ == "__main__":
    main()
