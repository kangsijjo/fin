# -*- coding: utf-8 -*-
"""
regime_gate_live_sim.py — rsi_reversal 국면 게이트를 **라이브와 동일한 조건**으로 검증 (2026-09-13)

⚠ 왜 다시 하는가 — §35 분석의 오류 정정
  §35 는 `capital_simulator` 로 강도 통과분 전체를 **공용 10슬롯**에 넣었다. 그러면 후보 수가 압도적인
  rsi_reversal(5.4만 건)이 포트폴리오의 90% 를 점령한다. 그런데 **라이브는 전략별 슬롯 상한을 이미 쓴다**
  (`integrated_dashboard_server._STRATEGY_MAX_SLOTS` = high_52w_filt 4 / rsi_reversal 4 / rsi_vol 2).
  `breaker_sweep.py` 주석에 이미 경고돼 있었다: *"공용 10슬롯으로 풀링하면 … 라이브와 전혀 다른 구성이 됨
  (첫 스윕에서 −98% 왜곡 확인) — 전략별 캡이 시뮬 신뢰성의 핵심"*. §35 의 'CAGR −19.2%, rsi_reversal 90% 점유'는
  **라이브가 아니라 상한 없는 가상 시스템의 값**이다.

이 스크립트는 breaker_sweep 의 라이브 등가 시뮬(`simulate`: 전략별 상한·우선순위·동일종목 중복 스킵·MTM)을
그대로 재사용해, **국면 게이트만** 바꿔 비교한다.

게이트: KOSDAQ 금융투자(증권사 자기매매) 10일 누적의 250일 트레일링 백분위 ≥ T 인 날만 rsi_reversal 진입 허용.
        (진입일 **전날까지**의 값만 사용 — shift(1) 적용, look-ahead 없음)

강도 필터: 워크포워드 채점본이 없으므로 **인샘플 강도점수**를 생성해 공급한다. 절대 수준은 낙관적이지만
        게이트 유무 비교는 양쪽에 동일 편향이라 **상대 비교는 유효**하다(그래서 '개선폭'만 읽는다).
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

_argv = sys.argv
sys.argv = ["breaker_sweep"]          # breaker_sweep 은 모듈 레벨 argparse — import 시 중립 인자 공급
from breaker_sweep import simulate, ACCOUNTS, PRIORITY, SLOTS, CAPITAL0  # noqa: E402
sys.argv = _argv

from strategy_engine import ALL_STRATEGIES, DEFAULT_COSTS   # noqa: E402
from strategies.daily_loader import load_macro_daily        # noqa: E402
from capital_simulator import build_price_lookup            # noqa: E402
from factor_scorer import IC_FEATURES, LIVE_STRATEGY_NAMES, MIN_IC_COVERAGE  # noqa: E402

TARGET = "rsi_reversal"
FWD = "20240908"


def strength_map():
    """(entry_date, code, live_strategy) → 강도점수. trades_history_v3 기반 인샘플."""
    t = pd.read_csv("trades_history_v3.csv", low_memory=False, dtype={"code": str})
    for c in ("entry_date",):
        t[c] = t[c].astype(str).str.replace("-", "").str[:8]
    x = t[t["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)].copy()
    x["code"] = x["code"].str.zfill(6)
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
    s = np.clip(5.0 + np.divide(raw, den, out=np.zeros_like(raw), where=den > 0) * 5.0, 0, 10)
    return dict(zip(zip(x["entry_date"], x["code"], x["strategy"]), s))


def regime_series():
    d = pd.read_csv("macro_data/kosdaq_investor_by_date.csv", dtype={"date": str}).set_index("date").sort_index()
    acc = d["금융투자"].astype(float).rolling(10, min_periods=10).sum().shift(1)   # 전날까지
    return acc.rolling(250, min_periods=120).apply(lambda s: s.rank(pct=True).iloc[-1] * 100, raw=False)


def main():
    print("=" * 100)
    print("  rsi_reversal 국면 게이트 — 라이브 등가 시뮬(전략별 슬롯 상한·우선순위·중복스킵·MTM)")
    print("=" * 100)
    df = load_macro_daily().reset_index(drop=True)
    df["code"] = df["code"].astype(str).str.zfill(6)
    mkt = df.groupby("date")["change_pct"].mean()
    df["mkt_strong"] = df["date"].map((mkt.rolling(60, min_periods=60).mean() > 0).to_dict()).fillna(False)
    price_map, trading_dates = build_price_lookup(df)
    print(f"거래일 {len(trading_dates):,} | forward {FWD}~")

    live = {s.name: s for s in ALL_STRATEGIES if s.name in ACCOUNTS["kiwoom"]}
    rows = []
    for nm, capn in ACCOUNTS["kiwoom"].items():
        ts = live[nm].backtest(df.copy(), DEFAULT_COSTS)
        for t in ts:
            ed, xd = str(t.entry_date), str(t.exit_date)
            if len(ed) == 8 and len(xd) == 8 and t.entry_price > 0:
                rows.append({"entry_date": ed, "exit_date": xd, "code": str(t.code).zfill(6),
                             "strategy": nm, "entry_price": float(t.entry_price),
                             "net_pct": float(t.net_pct), "score": float(getattr(t, "score", 0) or 0)})
        print(f"  [{nm}] {len(ts):,}건 (슬롯상한 {capn})")
    tdf = pd.DataFrame(rows)

    sm = strength_map()
    _E2L = {"star_high_52w_20_filt": "high_52w_filt"}
    key = list(zip(tdf["entry_date"], tdf["code"], tdf["strategy"].map(lambda s: _E2L.get(s, s))))
    tdf["strength"] = [sm.get(k, np.nan) for k in key]
    matched = tdf["strength"].notna().mean() * 100
    tdf = tdf[tdf["strength"] >= 5.7].copy()      # 무기록은 fail-closed (라이브 규약)
    print(f"  강도 채점 매칭 {matched:.1f}% | 5.7 통과 {len(tdf):,}건 "
          f"(rsi_reversal {int((tdf.strategy == TARGET).sum()):,} = {(tdf.strategy == TARGET).mean() * 100:.1f}%)")

    reg = regime_series()
    tdf["regime"] = tdf["entry_date"].map(reg)

    def run(d, label):
        out = []
        for tag, sub in (("full", d), ("fwd", d[d.entry_date >= FWD])):
            r = simulate(sub, price_map, trading_dates, ACCOUNTS["kiwoom"], PRIORITY["kiwoom"])
            out.append(r)
        f = lambda r, k: "-" if not r or r.get(k) is None else (f"{r[k]:.1f}" if isinstance(r[k], float) else f"{r[k]}")
        print(f"  {label:34s}{len(d):>8,}{f(out[0],'ret'):>10s}{f(out[0],'mdd'):>9s} | "
              f"{f(out[1],'ret'):>10s}{f(out[1],'mdd'):>9s}")
        return out

    print(f"\n  {'구성':34s}{'매매':>8s}{'full 수익%':>10s}{'MDD':>9s} | {'fwd 수익%':>10s}{'MDD':>9s}")
    run(tdf, "현행(게이트 없음)")
    for T in (30, 40, 50, 60):
        ok = tdf["regime"] >= T
        run(tdf[(tdf.strategy != TARGET) | ok.fillna(False)], f"국면 >= {T} 인 날만 rsi_rev")
    run(tdf[tdf.strategy != TARGET], "rsi_reversal 제외")


if __name__ == "__main__":
    main()
