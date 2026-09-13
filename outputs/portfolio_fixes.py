# -*- coding: utf-8 -*-
"""
portfolio_fixes.py — §35 에서 찾은 개선 후보 3종 검증 (2026-09-13, 사용자 "세 가지 전부 진행")

후보(§35)
  ① rsi_reversal 제외 — 90.2% 를 점유하며 자본곡선을 망친다(CAGR −19.2%→+7.4%, MDD −91%→−52%).
  ② rsi_reversal 에만 **시장국면 필터** — 빼는 대신 '하락장 진입'만 막아 매매 수를 지킨다(미검증).
  ③ **슬롯 배정 기준** — 현행은 거래대금 순인데 무작위보다 나쁘다(−22.5% vs −17.0%).

검정 원칙 (다중비교 방어)
  · IS(~2024-09-08)에서 파라미터를 고르고 **OOS(2024-09-08~)에서 확인**한다. OOS 는 선택에 쓰지 않는다.
  · 자본곡선(10슬롯 capital_simulator) 기준 — 평균 net 개선은 자본곡선 개선이 아니다(§31.4 교훈).
  · 연도별 부호 일관성을 함께 본다. 한 해가 전체를 끌고 갈 수 있다.
  · 최종 조합은 ①②③ 를 교차해 IS 최적을 고르고 OOS 로만 판정한다.

국면 변수(전부 진입일 전날까지의 정보만 사용)
  · kospi_ret_20d — trades_history 에 이미 있는 피처(강도점수 IC 1위).
  · kosdaq_ret20  — 일봉에서 계산한 KOSDAQ 유동종목 등가중 20일 수익률.
  · mkt_fin_10d   — §31 의 KOSDAQ 금융투자 10일 누적 250일 트레일링 백분위.
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
from strategies.daily_loader import load_macro_daily
from capital_simulator import simulate_capital

HERE = os.path.dirname(os.path.abspath(__file__))
OOS = "20240908"
TARGET = "rsi_reversal"


def build():
    t = pd.read_csv(os.path.join(HERE, "trades_history_v3.csv"), low_memory=False, dtype={"code": str})
    for c in ("date", "entry_date", "exit_date"):
        t[c] = t[c].astype(str).str.replace("-", "").str[:8]
    x = t[t["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)].copy()
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
    p = x[x.strength >= 5.7].copy()

    # 국면 변수
    df = load_macro_daily(start_date="20180101").reset_index(drop=True)
    df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
    df = df.sort_values(["code", "date"])
    df["ret1"] = df.groupby("code", group_keys=False)["close"].pct_change()
    day = df[df.trading_value >= 1_000_000_000].groupby("date")["ret1"].mean().sort_index()
    kq = ((1 + day).rolling(20).apply(np.prod, raw=True) - 1).shift(1) * 100
    p["kosdaq_ret20"] = p["entry_date"].map(kq)
    cache = os.path.join(HERE, "macro_data", "kosdaq_investor_by_date.csv")
    if os.path.exists(cache):
        d2 = pd.read_csv(cache, dtype={"date": str}).set_index("date").sort_index()
        acc = d2["금융투자"].astype(float).rolling(10, min_periods=10).sum().shift(1)
        fin = acc.rolling(250, min_periods=120).apply(lambda s: s.rank(pct=True).iloc[-1] * 100, raw=False)
        p["mkt_fin_10d"] = p["entry_date"].map(fin)
    p["seg"] = np.where(p.entry_date >= OOS, "OOS", "IS")
    p["yr"] = p.entry_date.str[:4]
    return p


def cap(d, score_col="strength", slots=10):
    if len(d) < 30:
        return None
    tr = [StrategyTrade(strategy=r.strategy, code=str(r.code), entry_date=r.entry_date, entry_price=10000.0,
                        exit_date=r.exit_date, exit_price=10000.0, holding_days=20,
                        gross_pct=float(r.net_pct), cost_pct=0.0, net_pct=float(r.net_pct),
                        score=float(getattr(r, score_col))) for r in d.itertuples()]
    s = simulate_capital(tr, max_concurrent=slots) or {}
    return dict(n=len(tr), cagr=s.get("cagr_pct"), mdd=s.get("real_mdd_pct"), sharpe=s.get("real_sharpe"))


def show(label, d, score_col="strength", slots=10):
    out = []
    for seg in ("IS", "OOS"):
        r = cap(d[d.seg == seg], score_col, slots)
        out.append(r)
    a, b = out
    f = lambda r, k: ("-" if r is None or r.get(k) is None else f"{r[k]}")
    print(f"  {label:36s}{f(a,'n'):>7s}{f(a,'cagr'):>9s}{f(a,'mdd'):>9s} | {f(b,'n'):>7s}{f(b,'cagr'):>9s}{f(b,'mdd'):>9s}")
    return a, b


def main():
    p = build()
    print(f"강도 5.7 통과 {len(p):,}건 | IS {int((p.seg=='IS').sum()):,} / OOS {int((p.seg=='OOS').sum()):,}")
    print(f"\n{'=' * 104}\n [기준] 현행 (슬롯 배정 = 거래대금 순)\n{'=' * 104}")
    p["s_tv"] = -pd.to_numeric(p.score_tv, errors="coerce").fillna(9e9)
    print(f"  {'구성':36s}{'IS n':>7s}{'CAGR':>9s}{'MDD':>9s} | {'OOS n':>7s}{'CAGR':>9s}{'MDD':>9s}")
    show("현행 전부 · 거래대금 순", p, "s_tv")

    print(f"\n{'=' * 104}\n [③] 슬롯 배정 기준 — IS 로 고르고 OOS 로 확인\n{'=' * 104}")
    rng = np.random.default_rng(11)
    p["s_rand"] = rng.random(len(p))
    p["s_mix"] = p.strength + rng.random(len(p)) * 0.5      # 강도 + 소량 무작위(동점 분산)
    print(f"  {'배정 기준':36s}{'IS n':>7s}{'CAGR':>9s}{'MDD':>9s} | {'OOS n':>7s}{'CAGR':>9s}{'MDD':>9s}")
    for col, lab in (("s_tv", "거래대금 순(현행)"), ("strength", "강도점수 순"),
                     ("s_rand", "무작위"), ("s_mix", "강도+무작위 혼합")):
        show(lab, p, col)

    print(f"\n{'=' * 104}\n [①] rsi_reversal 제외\n{'=' * 104}")
    print(f"  {'구성':36s}{'IS n':>7s}{'CAGR':>9s}{'MDD':>9s} | {'OOS n':>7s}{'CAGR':>9s}{'MDD':>9s}")
    show("전부 · 강도순", p, "strength")
    show("rsi_reversal 제외 · 강도순", p[p.strategy != TARGET], "strength")

    print(f"\n{'=' * 104}\n [②] rsi_reversal 에만 국면 필터 (하락장 진입 금지) — IS 로 임계 선택\n{'=' * 104}")
    print(f"  {'필터':36s}{'IS n':>7s}{'CAGR':>9s}{'MDD':>9s} | {'OOS n':>7s}{'CAGR':>9s}{'MDD':>9s}")
    cands = []
    for var, label, ths in (("kospi_ret_20d", "KOSPI 20일", (-10, -5, 0, 5)),
                            ("kosdaq_ret20", "KOSDAQ 20일", (-10, -5, 0, 5)),
                            ("mkt_fin_10d", "금융투자 국면", (20, 30, 40, 50))):
        if var not in p.columns or p[var].notna().sum() < 1000:
            continue
        for T in ths:
            ok = p[var] >= T
            d = p[(p.strategy != TARGET) | ok.fillna(False)]
            a, b = show(f"{label} >= {T} 인 날만 rsi_rev", d, "strength")
            if a and a.get("cagr") is not None:
                cands.append((a["cagr"], var, T, a, b))

    print(f"\n{'=' * 104}\n [최종] IS 최상위 조합을 OOS 로만 판정\n{'=' * 104}")
    if cands:
        cands.sort(reverse=True, key=lambda c: c[0])
        best = cands[0]
        print(f"  IS 최적 국면필터: {best[1]} >= {best[2]}  (IS CAGR {best[3]['cagr']}, MDD {best[3]['mdd']})")
        print(f"  → **OOS 결과: CAGR {best[4]['cagr'] if best[4] else '-'}, MDD {best[4]['mdd'] if best[4] else '-'}**")
        ok = p[best[1]] >= best[2]
        d = p[(p.strategy != TARGET) | ok.fillna(False)]
        print(f"\n  연도별 CAGR (현행 vs 국면필터 vs rsi_rev 제외)")
        print(f"  {'연도':6s}{'현행':>10s}{'국면필터':>10s}{'제외':>10s}{'현행MDD':>10s}{'필터MDD':>10s}{'제외MDD':>10s}")
        for yr in sorted(p.yr.unique()):
            a = cap(p[p.yr == yr]); b = cap(d[d.yr == yr]); c = cap(p[(p.yr == yr) & (p.strategy != TARGET)])
            if not a:
                continue
            g = lambda r, k: "-" if r is None or r.get(k) is None else f"{r[k]}"
            print(f"  {yr:6s}{g(a,'cagr'):>10s}{g(b,'cagr'):>10s}{g(c,'cagr'):>10s}"
                  f"{g(a,'mdd'):>10s}{g(b,'mdd'):>10s}{g(c,'mdd'):>10s}")


if __name__ == "__main__":
    main()
