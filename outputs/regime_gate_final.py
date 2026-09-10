# -*- coding: utf-8 -*-
"""
regime_gate_final.py — 시장 국면 게이트 최종 설계·검정 (2026-09-10, 사전등록)

여기까지 확정된 것
  · 종목 단위 크로스(사용자 원안, 모든 변형): **무작위 진입보다 나쁨** — 승률 86% 는 +2% 익절 규칙의 산물
    (무작위도 86.1~86.3%). 알파 -0.02~-0.96%p. 전 변형 기각(2026-09-09 P0/V 검정).
  · 시장 단위 KOSDAQ 주체별 국면은 **진짜 사전표본(2018-20)에서도 생존**:
      기관합계 20일 백분위  월rc +0.172 / +0.182 / +0.456  (A 2018-20 / B 2021-24.09 / C 2024.09-)
      금융투자 10일 백분위  월rc +0.159 / +0.317 / +0.540  ← 세 기간 부호·크기 모두 일관
      개인     10일 백분위  월rc -0.339 / -0.217 / -0.321  ← 거울상, 세 기간 일관
      KOSPI 시장 수급은 전 기간 ≈0 (대조군 통과: KOSDAQ 특유 효과)
  · 금융투자 = 증권사 자기매매(프로그램/차익) = 종목 단위 prm_net_5d_ratio(IC +0.141)와 **같은 주체**.

이 스크립트가 결정하는 것 — "그래서 무엇을 어떻게 켜고 끄나"
  G1 단일 vs 결합: 금융투자10 / 기관합계20 / 개인10(역방향) 및 결합점수(z합)의 기간별 게이트 성능.
  G2 임계 민감도: 백분위 컷 10~50 을 훑어 IS/OOS/사전표본(A) 동시 개선 구간이 **평평한지**(과적합 아닌지).
  G3 연속형 vs 이진: 게이트(진입 차단) vs 가중(포지션 축소 0.5배) — 매매 수 손실 대비 개선.
  G4 실행 지연: 국면은 전일까지 값으로 계산했지만, 실제로는 **당일 장중 매수**다. 지연 0/1/2일 감도.
  G5 자본곡선: 게이트 전/후 실제 CAGR·MDD·Sharpe(capital_simulator, 라이브 매매 기준 근사).
  G6 최종 추천 파라미터 1개 고정 + 세 기간 전부의 성적표.

판정 원칙: 세 기간(A/B/C) 전부에서 개선 부호가 같아야 채택. 하나라도 반대면 보류.
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

HERE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(HERE, "trades_history_v3.csv")
CACHE = os.path.join(HERE, "macro_data", "kosdaq_investor_by_date.csv")
PERIODS = ["A 2018-20", "B 2021-24.09", "C 2024.09-"]


def trailing_pct(s, look=250):
    return s.rolling(look, min_periods=120).apply(lambda w: w.rank(pct=True).iloc[-1] * 100.0, raw=False)


def build(lag=1):
    d = pd.read_csv(CACHE, dtype={"date": str}).set_index("date").sort_index()
    if "기관합계" not in d.columns:
        d["기관합계"] = d[[c for c in ("금융투자", "보험", "투신", "사모", "은행", "기타금융", "연기금") if c in d.columns]].sum(axis=1)
    out = pd.DataFrame(index=d.index)
    specs = {"fin10": ("금융투자", 10), "fin20": ("금융투자", 20), "inst20": ("기관합계", 20),
             "inst10": ("기관합계", 10), "ind10": ("개인", 10), "ind20": ("개인", 20)}
    for name, (col, w) in specs.items():
        acc = d[col].astype(float).rolling(w, min_periods=w).sum().shift(lag)
        out[name] = trailing_pct(acc)
    # 결합: 금융투자(+) 와 개인(-) 의 백분위 평균 — 두 신호가 거울상이므로 평균이 잡음을 줄인다
    out["combo"] = (out["fin10"] + (100 - out["ind10"])) / 2
    out["combo3"] = (out["fin10"] + out["inst20"] + (100 - out["ind10"])) / 3
    return out


def load_trades():
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    t["entry_date"] = t["entry_date"].astype(str).str.replace("-", "").str[:8]
    t["year"] = t["entry_date"].str[:4]
    t["period"] = np.select([t["entry_date"] < "20210101", t["entry_date"] < "20240908"],
                            PERIODS[:2], PERIODS[2])
    return t


def gate_perf(x, col, T, invert=False):
    """T 이상(또는 invert 면 T 이하)만 진입. 기간별 (기준평균, 게이트평균, 유지율)."""
    keep = x[x[col] <= T] if invert else x[x[col] >= T]
    out = {}
    for p in PERIODS:
        a, b = x[x.period == p], keep[keep.period == p]
        out[p] = (a["net_pct"].mean(), b["net_pct"].mean() if len(b) else np.nan,
                  len(b) / max(len(a), 1) * 100)
    a, b = x, keep
    out["전체"] = (a["net_pct"].mean(), b["net_pct"].mean(), len(b) / len(a) * 100)
    return out


def g1(x):
    print(f"\n{'=' * 104}\n [G1] 단일 vs 결합 게이트 — 백분위 >= 30 진입 허용 (개인은 <= 70)\n{'=' * 104}")
    print(f"  {'변수':10s} " + "".join(f"{p:>28s}" for p in PERIODS) + f"{'전체':>20s}")
    for col, T, inv, lab in (("fin10", 30, False, "금융투자10"), ("fin20", 30, False, "금융투자20"),
                             ("inst20", 30, False, "기관합계20"), ("inst10", 30, False, "기관합계10"),
                             ("ind10", 70, True, "개인10(역)"), ("ind20", 70, True, "개인20(역)"),
                             ("combo", 30, False, "결합(금융+개인)"), ("combo3", 30, False, "결합3")):
        r = gate_perf(x, col, T, inv)
        line = f"  {lab:10s} "
        for p in PERIODS + ["전체"]:
            base, g, keep = r[p]
            line += f"  {base:+6.2f}→{g:+6.2f}({g - base:+5.2f},{keep:.0f}%)"
        print(line)


def g2(x):
    print(f"\n{'=' * 104}\n [G2] 임계 민감도 — 평평한 고원인가(과적합 아닌가)\n{'=' * 104}")
    for col, inv, lab in (("fin10", False, "금융투자10"), ("combo", False, "결합(금융+개인)")):
        print(f"\n  ▸ {lab}: 컷 T 별 (기간 개선폭 A / B / C, 유지율)")
        for T in range(10, 55, 5):
            r = gate_perf(x, col, T, inv)
            deltas = [r[p][1] - r[p][0] for p in PERIODS]
            ok = all(d > 0 for d in deltas)
            print(f"     T={T:2d}  A {deltas[0]:+6.2f}  B {deltas[1]:+6.2f}  C {deltas[2]:+6.2f}  "
                  f"유지 {r['전체'][2]:.0f}%   {'✔ 3기간 개선' if ok else ''}")


def g3(x, col="fin10", T=30):
    print(f"\n{'=' * 104}\n [G3] 이진 차단 vs 가중 축소 ({col} < {T} 이면 포지션 0.5배)\n{'=' * 104}")
    x = x.copy()
    x["w"] = np.where(x[col] >= T, 1.0, 0.5)
    print(f"  {'기간':14s} {'무게이트':>10s} {'가중0.5':>10s} {'완전차단':>10s} {'매매유지(차단시)':>16s}")
    for p in PERIODS + ["전체"]:
        s = x if p == "전체" else x[x.period == p]
        base = s["net_pct"].mean()
        wavg = (s["net_pct"] * s["w"]).sum() / s["w"].sum()
        cut = s[s[col] >= T]["net_pct"].mean()
        print(f"  {p:14s} {base:+10.3f} {wavg:+10.3f} {cut:+10.3f} {len(s[s[col] >= T]) / len(s) * 100:15.0f}%")


def g4(t, col="fin10", T=30):
    print(f"\n{'=' * 104}\n [G4] 실행 지연 민감도 — 국면 계산에 쓰는 최신 데이터가 며칠 전인가\n{'=' * 104}")
    print(f"  {'지연':6s} " + "".join(f"{p:>22s}" for p in PERIODS))
    for lag in (1, 2, 3):
        reg = build(lag)
        x = t.merge(reg[[col]], left_on="entry_date", right_index=True, how="inner").dropna(subset=[col])
        r = gate_perf(x, col, T)
        print(f"  T+{lag:<4d} " + "".join(f"  {r[p][0]:+6.2f}→{r[p][1]:+6.2f}({r[p][1] - r[p][0]:+5.2f})" for p in PERIODS))


def g5(x, col="fin10", T=30):
    print(f"\n{'=' * 104}\n [G5] 연도별 일관성 + 전략별 일관성 ({col} >= {T})\n{'=' * 104}")
    yr = x.groupby("year").apply(lambda d: pd.Series(dict(
        base=d["net_pct"].mean(), gated=d[d[col] >= T]["net_pct"].mean(),
        n=len(d), keep=(d[col] >= T).mean() * 100)), include_groups=False)
    yr["delta"] = yr["gated"] - yr["base"]
    print("  연도: " + " | ".join(f"{y} {r.delta:+.2f}(유지{r.keep:.0f}%)" for y, r in yr.iterrows()))
    print(f"  연도 개선 {int((yr.delta > 0).sum())}/{len(yr)}")
    st = x.groupby("strategy").apply(lambda d: pd.Series(dict(
        delta=d[d[col] >= T]["net_pct"].mean() - d["net_pct"].mean(), n=len(d))), include_groups=False)
    print("  전략: " + " | ".join(f"{s} {r.delta:+.2f}" for s, r in st.iterrows()))
    print(f"  전략 개선 {int((st.delta > 0).sum())}/{len(st)}")


def g6(x, col="fin10", T=30):
    print(f"\n{'=' * 104}\n [G6] 최종 후보 성적표 — {col} 백분위 >= {T} 일 때만 진입\n{'=' * 104}")
    keep = x[x[col] >= T]
    print(f"  {'기간':14s} {'매매(전)':>10s} {'매매(후)':>10s} {'평균(전)':>10s} {'평균(후)':>10s} {'개선':>8s} {'승률(전→후)':>16s}")
    for p in PERIODS + ["전체"]:
        a = x if p == "전체" else x[x.period == p]
        b = keep if p == "전체" else keep[keep.period == p]
        wa, wb = (a["net_pct"] > 0).mean() * 100, (b["net_pct"] > 0).mean() * 100
        print(f"  {p:14s} {len(a):10,} {len(b):10,} {a['net_pct'].mean():+10.3f} {b['net_pct'].mean():+10.3f} "
              f"{b['net_pct'].mean() - a['net_pct'].mean():+8.3f} {wa:7.1f}→{wb:6.1f}%")
    # 국면 분포: 현재(최근 60일) 어디쯤인가
    print(f"\n  최근 국면(참고): 마지막 10 거래일의 {col} 백분위")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--col", default="fin10")
    ap.add_argument("--T", type=int, default=30)
    a = ap.parse_args()
    reg = build(lag=1)
    t = load_trades()
    x = t.merge(reg, left_on="entry_date", right_index=True, how="inner")
    print(f"라이브 매매 {len(x):,}건 | 기간별 " + ", ".join(f"{p} {n:,}" for p, n in x['period'].value_counts().sort_index().items()))
    g1(x)
    g2(x)
    g3(x, a.col, a.T)
    g4(t, a.col, a.T)
    g5(x, a.col, a.T)
    g6(x, a.col, a.T)
    print("\n  [최근 국면] " + ", ".join(f"{i}:{v:.0f}" for i, v in reg[a.col].dropna().tail(10).items()))


if __name__ == "__main__":
    main()
