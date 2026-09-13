# -*- coding: utf-8 -*-
"""
unused_feature_scan.py — 이미 저장돼 있으나 강도점수에 **안 쓰는** 컬럼들의 예측력 일괄 검정 (2026-09-13)

사용자 질문: "필요한 데이터가 있으면 지금 모아볼까? 프로그램매매 같은."
→ 이번 주 교훈(§32): **새로 모으기 전에 이미 있는 것부터.** 지표 커버리지 복구(계산 10분)가
   사흘 걸린 프로그램매매 백필보다 4.5배 컸다. korea_indicators 는 컬럼이 41개인데 IC_FEATURES 는 20개뿐이고,
   채움률 100% 인데 한 번도 쓰지 않은 컬럼이 여럿 있다(short_ratio·lending_chg_5d·return_20d 등).

검정 (factor_scorer 와 **동일 잣대**: 라이브 6전략·풀링 Spearman·net_pct)
  1. 값 실재 확인 — 채움률만 100% 이고 전부 0/상수면 무의미하므로 표준편차·고유값 수를 함께 본다.
  2. IC·표본 수 — §31.5 규약대로 표본을 반드시 함께 본다(소표본 IC 는 부풀어진다).
  3. 기존 IC_FEATURES 와의 중복도 — |순위상관| ≥ 0.5 면 새 정보가 아니다.
  4. 판정: 채택 후보 = |IC| ≥ 0.05 AND 표본 ≥ 20,000 AND 기존 피처와 |상관| < 0.5.

주의: 이 스캔은 **탐색**이다. 여기서 고른 후보는 그대로 배포하지 않고, 기존 강도점수에 넣었을 때
동일 통과율 기준으로 개선되는지(§31.8 방식)를 따로 확인해야 한다 — 다중비교로 우연히 뽑힐 수 있다.
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

from fin_paths import STOCK_DB
from factor_scorer import IC_FEATURES, LIVE_STRATEGY_NAMES

HERE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(HERE, "trades_history_v3.csv")
# korea_indicators 에 있으나 IC_FEATURES 에 없는 컬럼(파생 원본·중복 제외)
CAND = ["short_ratio", "lending_chg_5d", "vol_spike", "volatility",
        "return_1d", "return_5d", "return_20d",
        "ma5_ratio", "ma20_ratio", "ma60_ratio",
        "macd", "macd_signal", "nasdaq_chg", "kospi_chg"]


def main():
    import sqlite3
    con = sqlite3.connect(f"file:{STOCK_DB}?mode=ro", uri=True)
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    t["date"] = t["date"].astype(str).str.replace("-", "").str[:8]
    t["code"] = t["code"].str.zfill(6)
    live = t[t["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)].copy()
    print(f"라이브 6전략 {len(live):,}건 (factor_scorer 동일 기준)")

    cols = ", ".join(f"[{c}]" for c in CAND)
    ki = pd.read_sql(f"SELECT ticker AS code, date, {cols} FROM korea_indicators WHERE date >= '2018-01-01'", con)
    ki["code"] = ki["code"].astype(str).str.zfill(6)
    ki["date"] = ki["date"].astype(str).str.replace("-", "").str[:8]
    ki = ki.drop_duplicates(["code", "date"])
    print(f"korea_indicators {len(ki):,}행 조인 중...")
    x = live.merge(ki, on=["code", "date"], how="left")
    y = x["net_pct"].astype(float)

    print(f"\n{'=' * 104}\n [1-2] 값 실재성 + IC (라이브6 풀링 Spearman)\n{'=' * 104}")
    print(f"  {'컬럼':16s}{'표본':>9s}{'채움률':>8s}{'표준편차':>12s}{'고유값':>8s}{'IC':>10s}{'판정':>8s}")
    res = {}
    for c in CAND:
        v = pd.to_numeric(x[c], errors="coerce")
        m = v.notna() & y.notna()
        n = int(m.sum())
        if n < 100:
            print(f"  {c:16s}{n:9,}{v.notna().mean() * 100:7.1f}%{'-':>12s}{'-':>8s}{'-':>10s}{'표본부족':>8s}")
            continue
        sd = float(v[m].std())
        nun = int(v[m].nunique())
        if sd == 0 or nun <= 2:
            print(f"  {c:16s}{n:9,}{v.notna().mean() * 100:7.1f}%{sd:12.4g}{nun:8,}{'-':>10s}{'상수/무의미':>8s}")
            continue
        ic, _ = spearmanr(v[m], y[m])
        res[c] = (float(ic), n, v)
        ok = abs(ic) >= 0.05 and n >= 20000
        print(f"  {c:16s}{n:9,}{v.notna().mean() * 100:7.1f}%{sd:12.4g}{nun:8,}{ic:+10.4f}{'후보' if ok else '':>8s}")

    print(f"\n{'=' * 104}\n [3] 후보의 기존 IC_FEATURES 와 중복도 (|r| ≥ 0.5 면 새 정보 아님)\n{'=' * 104}")
    base = [f for f in IC_FEATURES if f in x.columns]
    cands = [c for c, (ic, n, _) in res.items() if abs(ic) >= 0.05 and n >= 20000]
    if not cands:
        print("  |IC| ≥ 0.05 이고 표본 20,000 이상인 후보 없음")
    else:
        print(f"  {'후보':16s}" + "".join(f"{b[:11]:>12s}" for b in base[:8]))
        for c in cands:
            v = res[c][2]
            line = f"  {c:16s}"
            worst, wb = 0.0, ""
            for b in base:
                bv = pd.to_numeric(x[b], errors="coerce")
                m = v.notna() & bv.notna()
                r = spearmanr(v[m], bv[m])[0] if m.sum() > 500 else np.nan
                if not np.isnan(r) and abs(r) > abs(worst):
                    worst, wb = r, b
                if b in base[:8]:
                    line += f"{r:+12.3f}" if not np.isnan(r) else f"{'-':>12s}"
            print(line)
            print(f"  {'':16s}→ 최대 중복: {wb} {worst:+.3f}  "
                  f"{'**독립 정보**' if abs(worst) < 0.5 else '중복 — 채택 부적합'}")
    con.close()


if __name__ == "__main__":
    main()
