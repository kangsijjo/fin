# -*- coding: utf-8 -*-
"""
regime_gate_capital.py — 국면 게이트의 **자본곡선·독립성·배포형태** 최종 검정 (2026-09-10, 사전등록)

평균 net% 개선(regime_gate_final.py: 세 기간 전부 +0.19~+3.26, 임계 10~50 전 구간 일관)만으로는
배포를 결정할 수 없다. 매매를 27% 잘라내므로 **회전율이 줄어 자본곡선(CAGR)** 이 나빠질 수 있고,
기존 강도점수의 최대가중 피처(kospi_ret_20d, IC -0.17)와 **같은 것을 다르게 부른 것**일 수도 있다.

검정
  C1 자본곡선: 10슬롯 자본 제약(capital_simulator, 라이브 매매 이력 기준) — 무게이트 vs 게이트(차단) vs
     가중 축소(0.5배). 기간 A/B/C 및 전체. CAGR·MDD·Sharpe·매매수·자본회전.
  C2 독립성: fin10 vs kospi_ret_20d(매매 피처) 및 KOSDAQ 20일 시장수익률의 순위상관 + 동시 순위회귀
     표준화계수. 2원표(3×3)로 '시장이 오를 때만 좋은 것' 인지 확인.
  C3 국면 지속성: fin10 백분위의 자기상관·국면 전환 빈도 — 게이트가 하루살이 잡음이 아니라 며칠~몇 주
     지속되는 상태인지(실행 가능성). 국면별 평균 지속일.
  C4 현재 국면 진단: 최근 값(2026-09 기준 4~16)의 역사적 빈도와 **그 구간에 실제로 무슨 일이 있었나**
     (해당 백분위 구간 진입 후 20일 매매 평균 net, 시장 수익).
  C5 배포형태 비교: (a) 완전차단 (b) 포지션 0.5배 (c) 강도점수 가산(국면 백분위를 0~1 로 정규화해
     강도점수에 ±0.3 가산하는 근사) — 매매 유지율 대비 개선.

판정: 배포 후보 = 자본곡선 CAGR·MDD 가 세 기간 전부 개선 AND kospi_ret_20d 통제 후 잔존 AND 국면 지속 >= 3일.
"""
import os
import sys
import argparse

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from strategies.base import StrategyTrade
from capital_simulator import simulate_capital

HERE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(HERE, "trades_history_v3.csv")
CACHE = os.path.join(HERE, "macro_data", "kosdaq_investor_by_date.csv")
PERIODS = ["A 2018-20", "B 2021-24.09", "C 2024.09-"]
BOUNDS = {"A 2018-20": ("20180101", "20210101"), "B 2021-24.09": ("20210101", "20240908"),
          "C 2024.09-": ("20240908", "20990101"), "전체": ("20180101", "20990101")}


def build(lag=1):
    d = pd.read_csv(CACHE, dtype={"date": str}).set_index("date").sort_index()
    if "기관합계" not in d.columns:
        d["기관합계"] = d[[c for c in ("금융투자", "보험", "투신", "사모", "은행", "기타금융", "연기금") if c in d.columns]].sum(axis=1)
    out = pd.DataFrame(index=d.index)
    for name, (col, w) in {"fin10": ("금융투자", 10), "inst20": ("기관합계", 20), "ind10": ("개인", 10)}.items():
        acc = d[col].astype(float).rolling(w, min_periods=w).sum().shift(lag)
        out[name] = acc.rolling(250, min_periods=120).apply(lambda s: s.rank(pct=True).iloc[-1] * 100.0, raw=False)
    out["combo"] = (out["fin10"] + (100 - out["ind10"])) / 2
    return out


def load_x(reg):
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    for c in ("entry_date", "exit_date"):
        t[c] = t[c].astype(str).str.replace("-", "").str[:8]
    t["period"] = np.select([t["entry_date"] < "20210101", t["entry_date"] < "20240908"], PERIODS[:2], PERIODS[2])
    x = t.merge(reg, left_on="entry_date", right_index=True, how="inner")
    return x.dropna(subset=["fin10"])


def to_trades(df, weight=1.0):
    """CSV 행 → StrategyTrade. entry_price 는 MTM 미사용이라 상수. score 는 거래대금 순위(작을수록 큼)."""
    out = []
    for r in df.itertuples():
        out.append(StrategyTrade(
            strategy=r.strategy, code=str(r.code), entry_date=r.entry_date, entry_price=10000.0,
            exit_date=r.exit_date, exit_price=10000.0 * (1 + r.net_pct / 100), holding_days=20,
            gross_pct=float(getattr(r, "gross_pct", r.net_pct)), cost_pct=0.0,
            net_pct=float(r.net_pct) * weight,
            score=-float(getattr(r, "score_tv", 0) or 0)))
    return out


def sim(df, label, weight=1.0):
    tr = to_trades(df, weight)
    s = simulate_capital(tr) or {}
    return dict(label=label, n=len(tr), cagr=s.get("cagr_pct"), mdd=s.get("real_mdd_pct"),
                sharpe=s.get("real_sharpe"), final=s.get("final_capital"))


def c1(x, col, T):
    print(f"\n{'=' * 100}\n [C1] 자본곡선 (10슬롯 제약) — 무게이트 vs {col}>={T} 차단 vs 0.5배 축소\n{'=' * 100}")
    print(f"  {'기간':14s} {'형태':12s} {'매매':>8s} {'CAGR%':>9s} {'MDD%':>9s} {'Sharpe':>8s}")
    for p in PERIODS + ["전체"]:
        lo, hi = BOUNDS[p]
        s = x[(x.entry_date >= lo) & (x.entry_date < hi)]
        if len(s) < 100:
            continue
        for lab, sub, w in (("무게이트", s, 1.0), ("게이트 차단", s[s[col] >= T], 1.0), ("0.5배 축소", s, None)):
            if w is None:
                sub = s.copy()
                sub["net_pct"] = np.where(sub[col] >= T, sub["net_pct"], sub["net_pct"] * 0.5)
            r = sim(sub, lab)
            print(f"  {p:14s} {lab:12s} {r['n']:8,} {str(r['cagr']):>9s} {str(r['mdd']):>9s} {str(r['sharpe']):>8s}")
        print()


def c2(x, col):
    print(f"\n{'=' * 100}\n [C2] 독립성 — {col} 이 kospi_ret_20d(강도점수 최대가중, IC -0.17)와 다른 것인가\n{'=' * 100}")
    z = x.dropna(subset=["kospi_ret_20d", col]).copy()
    ry, rf, rk = z["net_pct"].rank(), z[col].rank(), z["kospi_ret_20d"].rank()
    print(f"  변수 간 순위상관: {col} vs kospi_ret_20d {rf.corr(rk):+.4f}  (0 에 가까우면 서로 다른 정보)")
    X = np.column_stack([np.ones(len(z)), rf, rk])
    b = np.linalg.lstsq(X, ry, rcond=None)[0]
    print(f"  net_pct 단독 순위상관: {col} {ry.corr(rf):+.4f} | kospi_ret_20d {ry.corr(rk):+.4f}")
    print(f"  동시회귀 표준화계수:   {col} {b[1] * rf.std() / ry.std():+.4f} | kospi_ret_20d {b[2] * rk.std() / ry.std():+.4f}")
    z["f3"] = pd.cut(z[col], [0, 33.3, 66.7, 100.001], labels=["금융투자 매도", "중간", "금융투자 매수"], include_lowest=True)
    z["k3"] = pd.qcut(z["kospi_ret_20d"].rank(method="first"), 3, labels=["시장 하락", "중간", "시장 상승"])
    for p in PERIODS:
        s = z[z.period == p]
        two = s.pivot_table(index="k3", columns="f3", values="net_pct", aggfunc="mean", observed=True)
        cnt = s.pivot_table(index="k3", columns="f3", values="net_pct", aggfunc="size", observed=True)
        print(f"  [{p}] 2원표 평균 net%")
        for m, row in two.iterrows():
            print("     " + f"{str(m):8s} " + "  ".join(f"{row[c]:+7.2f}({int(cnt.loc[m, c]):,})" for c in two.columns))


def c3(reg, col, T):
    print(f"\n{'=' * 100}\n [C3] 국면 지속성 — 실행 가능한 상태인가(하루살이 잡음 아닌가)\n{'=' * 100}")
    s = reg[col].dropna()
    print(f"  자기상관: 1일 {s.autocorr(1):+.3f} | 5일 {s.autocorr(5):+.3f} | 20일 {s.autocorr(20):+.3f}")
    on = (s >= T).astype(int)
    runs, cur = [], 1
    for i in range(1, len(on)):
        if on.iloc[i] == on.iloc[i - 1]:
            cur += 1
        else:
            runs.append((on.iloc[i - 1], cur))
            cur = 1
    runs.append((on.iloc[-1], cur))
    up = [r[1] for r in runs if r[0] == 1]
    dn = [r[1] for r in runs if r[0] == 0]
    print(f"  '진입 허용({col}>={T})' 구간: {len(up)}회, 평균 {np.mean(up):.1f}일 (중앙 {np.median(up):.0f}) | 전체 일수 비중 {on.mean() * 100:.0f}%")
    print(f"  '진입 금지' 구간: {len(dn)}회, 평균 {np.mean(dn):.1f}일 (중앙 {np.median(dn):.0f})")
    print(f"  → 평균 {np.mean(up):.0f}일 지속이면 일 단위 판정으로 실행 가능(매일 1콜 갱신).")


def c4(x, reg, col):
    print(f"\n{'=' * 100}\n [C4] 현재 국면 진단 — 지금이 어떤 상태이고 역사적으로 무슨 일이 있었나\n{'=' * 100}")
    cur = reg[col].dropna()
    last = cur.iloc[-1]
    print(f"  최근 20 거래일 {col}: " + ", ".join(f"{i[4:]}:{v:.0f}" for i, v in cur.tail(20).items()))
    print(f"  현재값 {last:.0f} 백분위 | 역사적으로 이 수준 이하였던 날 {(cur <= last).mean() * 100:.1f}%")
    b = pd.cut(x[col], [0, 10, 20, 30, 50, 70, 100.001],
               labels=["0-10", "10-20", "20-30", "30-50", "50-70", "70-100"], include_lowest=True)
    tab = x.groupby(b, observed=True)["net_pct"].agg(["mean", "median", "size"])
    print(f"  {'국면 구간':10s} {'평균 net%':>10s} {'중앙':>8s} {'매매수':>9s} {'승률':>7s}")
    for k, r in tab.iterrows():
        w = (x.loc[b == k, "net_pct"] > 0).mean() * 100
        print(f"  {str(k):10s} {r['mean']:+10.3f} {r['median']:+8.2f} {int(r['size']):9,} {w:6.1f}%")


def c5(x, col, T):
    print(f"\n{'=' * 100}\n [C5] 배포형태 비교 — 매매 유지율 대비 개선\n{'=' * 100}")
    print(f"  {'형태':22s} {'전체 평균':>10s} {'A':>9s} {'B':>9s} {'C':>9s} {'매매유지':>9s}")
    forms = [("무게이트", lambda s: (s["net_pct"], 1.0)),
             (f"완전차단 (>={T})", lambda s: (s.loc[s[col] >= T, "net_pct"], (s[col] >= T).mean())),
             ("0.5배 축소", lambda s: (np.where(s[col] >= T, s["net_pct"], s["net_pct"] * 0.5), 1.0)),
             ("연속가중 (백분위/100)", lambda s: (s["net_pct"] * (s[col] / 100), 1.0))]
    for lab, fn in forms:
        vals, keep = fn(x)
        row = f"  {lab:22s} {np.mean(vals):+10.3f}"
        for p in PERIODS:
            s = x[x.period == p]
            v, _ = fn(s)
            row += f" {np.mean(v):+9.3f}"
        row += f" {keep * 100:8.0f}%"
        print(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--col", default="fin10")
    ap.add_argument("--T", type=int, default=30)
    a = ap.parse_args()
    reg = build()
    x = load_x(reg)
    print(f"라이브 매매 {len(x):,}건 | 기간별 " + ", ".join(f"{p} {n:,}" for p, n in x['period'].value_counts().sort_index().items()))
    c1(x, a.col, a.T)
    c2(x, a.col)
    c3(reg, a.col, a.T)
    c4(x, reg, a.col)
    c5(x, a.col, a.T)


if __name__ == "__main__":
    main()
