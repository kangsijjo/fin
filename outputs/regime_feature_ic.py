# -*- coding: utf-8 -*-
"""
regime_feature_ic.py — 시장 국면 변수를 **강도점수 피처**로 넣었을 때의 IC 순위·견고성·부작용 (2026-09-10)

여기까지의 결론
  · 종목 단위 크로스: 사망 확정. 무작위 진입 대조에서 승률 86.4% vs 86.1~86.3%(차이 +0.1%p),
    평균은 오히려 -0.073%p 열위. 주체 분해(금융투자·투신·연기금·사모·기타법인) 후에도 IC ≤ 0.034,
    크로스 알파 전 주체 음수(-0.17~-0.47). → '종목 선택' 정보는 없다.
  · 시장 단위 국면(KOSDAQ 금융투자 10일 누적 백분위 fin10): 세 기간 전부 생존
    (월 순위상관 +0.16 / +0.32 / +0.54), KOSPI 대조군 ≈0, kospi_ret_20d 통제 후 표준화계수 +0.0997 잔존.
  · 그러나 **이진 게이트로는 실패**: 평균 net% 는 +0.97%p 개선되지만 10슬롯 자본곡선에서 CAGR 이
    C 구간 +48.5% → -18.3% 로 붕괴(매매 27% 소실 → 자본 유휴·집중). 연속가중은 자본곡선 소폭 개선.
    → 켜고 끄는 스위치가 아니라 **연속 피처**로 강도점수에 녹여야 한다.

이 스크립트가 답하는 것
  F1 factor_scorer 와 **동일한 잣대**(라이브 6전략, 풀링 Spearman, net_pct)로 국면 변수들의 IC 를 재고,
     기존 IC_FEATURES 20 개와 같은 표에 세워 순위·IC 질량 비중을 낸다.
  F2 견고성: 기간 A(2018-20, 진짜 사전표본) / B(2021-24.09) / C(2024.09-) 별 IC 부호·크기.
  F3 중복도: 기존 피처들과의 순위상관 — 특히 kospi_ret_20d, for_5d, ins_5d, prm_net_5d_ratio.
  F4 부작용 추정: 국면 피처를 넣으면 강도점수가 **현재 국면(fin10=4 백분위)** 에서 얼마나 내려가나.
     2026-08 이미 매수 0 건(강도 최대 5.63 < 임계 5.7)인 상황에 추가 하방 압력이 얼마인지 — 배포 전 필수 확인.

판정: 피처 채택 후보 = |IC| >= 0.05 (기존 상위권 대비) AND 세 기간 부호 일치 AND 기존 피처와 |상관| < 0.5.
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

from factor_scorer import IC_FEATURES, LIVE_STRATEGY_NAMES, FEATURE_META

HERE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(HERE, "trades_history_v3.csv")
CACHE = os.path.join(HERE, "macro_data", "kosdaq_investor_by_date.csv")
PERIODS = {"A 2018-20": ("20180101", "20210101"), "B 2021-24.09": ("20210101", "20240908"),
           "C 2024.09-": ("20240908", "20990101")}


def build_regimes(lag=1):
    d = pd.read_csv(CACHE, dtype={"date": str}).set_index("date").sort_index()
    if "기관합계" not in d.columns:
        d["기관합계"] = d[[c for c in ("금융투자", "보험", "투신", "사모", "은행", "기타금융", "연기금") if c in d.columns]].sum(axis=1)
    out = pd.DataFrame(index=d.index)
    specs = {"mkt_fin_10d": ("금융투자", 10), "mkt_fin_20d": ("금융투자", 20),
             "mkt_inst_20d": ("기관합계", 20), "mkt_indiv_10d": ("개인", 10)}
    for name, (col, w) in specs.items():
        acc = d[col].astype(float).rolling(w, min_periods=w).sum().shift(lag)
        out[name] = acc.rolling(250, min_periods=120).apply(lambda s: s.rank(pct=True).iloc[-1] * 100.0, raw=False)
    out["mkt_regime"] = (out["mkt_fin_10d"] + (100 - out["mkt_indiv_10d"])) / 2
    return out


def pooled_ic(x, y):
    m = x.notna() & y.notna()
    if m.sum() < 100:
        return np.nan, 0
    ic, _ = spearmanr(x[m], y[m])
    return (np.nan if np.isnan(ic) else float(ic)), int(m.sum())


def main():
    reg = build_regimes()
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    t["entry_date"] = t["entry_date"].astype(str).str.replace("-", "").str[:8]
    live = t[t["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)].copy()
    print(f"라이브 6전략 {len(live):,}건 / 전체 {len(t):,}건 (factor_scorer 와 동일 기준)")
    x = live.merge(reg, left_on="entry_date", right_index=True, how="left")
    y = x["net_pct"].astype(float)
    new_feats = list(reg.columns)

    # ── F1: 기존 20 피처 + 국면 변수 동일 잣대 IC ──
    print(f"\n{'=' * 92}\n [F1] factor_scorer 동일 잣대(라이브 6전략·풀링 Spearman) IC 순위\n{'=' * 92}")
    rows = []
    for f in IC_FEATURES + new_feats:
        col = f
        if f == "crd_remn_rt" and "crd_remn_rt_y" in x.columns:
            col = "crd_remn_rt_y"
        if col not in x.columns:
            continue
        ic, n = pooled_ic(x[col].astype(float), y)
        if np.isnan(ic):
            continue
        rows.append(dict(feat=f, ic=ic, n=n, new=f in new_feats))
    r = pd.DataFrame(rows)
    r["mass"] = r["ic"].abs() / r["ic"].abs().sum() * 100
    r = r.sort_values("ic", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)
    print(f"  {'순위':>4s} {'피처':22s} {'IC':>9s} {'질량%':>7s} {'표본':>9s}")
    for i, row in r.iterrows():
        tag = " ★신규" if row["new"] else ""
        label = FEATURE_META.get(row["feat"], (row["feat"], 0))[0] if not row["new"] else row["feat"]
        print(f"  {i + 1:4d} {label:22s} {row['ic']:+9.4f} {row['mass']:7.1f} {row['n']:9,}{tag}")

    # ── F2: 기간별 견고성 ──
    print(f"\n{'=' * 92}\n [F2] 기간별 IC (A=진짜 사전표본) — 부호가 뒤집히면 국면 특수\n{'=' * 92}")
    ref = ["kospi_ret_20d", "prm_net_5d_ratio", "for_5d", "ins_5d", "rsi14"]
    print(f"  {'피처':22s} " + "".join(f"{p:>16s}" for p in PERIODS) + f"{'전체':>12s}")
    for f in new_feats + ref:
        col = "crd_remn_rt_y" if f == "crd_remn_rt" else f
        if col not in x.columns:
            continue
        line = f"  {f:22s} "
        for p, (lo, hi) in PERIODS.items():
            s = x[(x.entry_date >= lo) & (x.entry_date < hi)]
            ic, n = pooled_ic(s[col].astype(float), s["net_pct"].astype(float))
            line += f"  {ic:+7.4f}(n{n // 1000}k)" if not np.isnan(ic) else f"{'-':>16s}"
        ic, n = pooled_ic(x[col].astype(float), y)
        line += f"  {ic:+10.4f}"
        print(line)

    # ── F3: 기존 피처와의 중복도 ──
    print(f"\n{'=' * 92}\n [F3] 중복도 — 국면 변수 vs 기존 피처 순위상관 (|r| >= 0.5 면 중복)\n{'=' * 92}")
    cmp = ["kospi_ret_20d", "vix", "sox_ret_5d", "for_5d", "ins_5d", "prm_net_5d_ratio", "score_tv", "rsi14"]
    print(f"  {'':22s} " + "".join(f"{c[:12]:>13s}" for c in cmp))
    for f in new_feats:
        line = f"  {f:22s} "
        for c in cmp:
            if c not in x.columns:
                line += f"{'-':>13s}"
                continue
            m = x[f].notna() & x[c].notna()
            rr = spearmanr(x.loc[m, f], x.loc[m, c])[0] if m.sum() > 100 else np.nan
            line += f"{rr:+13.3f}" if not np.isnan(rr) else f"{'-':>13s}"
        print(line)

    # ── F4: 현재 국면에서의 하방 압력 ──
    print(f"\n{'=' * 92}\n [F4] 부작용 — 지금 국면(fin10 최근값)에서 강도점수에 얼마나 하방 압력인가\n{'=' * 92}")
    best = r[r["new"]].iloc[0]["feat"]
    cur = reg[best].dropna()
    ic_best = float(r[r.feat == best]["ic"].iloc[0])
    mass = float(r[r.feat == best]["mass"].iloc[0])
    print(f"  최고 국면 피처: {best} (IC {ic_best:+.4f}, 질량 {mass:.1f}%)")
    print(f"  최근값 {cur.iloc[-1]:.0f} 백분위 → 이 피처의 백분위 점수 자체가 {cur.iloc[-1]:.0f}/100")
    print(f"  강도점수는 피처 백분위의 IC 가중 평균(0~10) — 이 피처가 질량 {mass:.1f}% 를 가지면")
    print(f"    현재 국면 기여 ≈ {mass / 100 * (cur.iloc[-1] / 100) * 10:.2f}점 (중립 기여 {mass / 100 * 0.5 * 10:.2f}점)")
    print(f"    → 중립 대비 {mass / 100 * (cur.iloc[-1] / 100 - 0.5) * 10:+.2f}점 하방 압력")
    print(f"  ⚠ 2026-08 실측: 강도 최대 5.63 < 임계 5.7 로 매수 0 건. 추가 하방이면 매매 정지가 길어진다.")
    print(f"  최근 60 거래일 {best} 분포: 최소 {cur.tail(60).min():.0f} / 중앙 {cur.tail(60).median():.0f} / 최대 {cur.tail(60).max():.0f}")


if __name__ == "__main__":
    main()
