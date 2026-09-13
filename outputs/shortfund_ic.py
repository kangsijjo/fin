# -*- coding: utf-8 -*-
"""
shortfund_ic.py — 공매도 비중·재무지표(PER/PBR/배당)의 5년 예측력 검정 (2026-09-13)

배경: korea_indicators 의 `short_ratio` 는 컬럼만 있고 값이 전부 0 이었다(고유값 1). 재무지표는 아예 없었다.
      pykrx 로 2021~2026 (1,396 거래일) 신규 수집 후, **종목 선택력**을 일별 횡단면 IC 로 검정한다.
      최근 1년(230일) 파일럿에서 공매도 5일평균 IC **+0.106**(t 14.7, 양수일 85%)이 나왔으나,
      §31.7 의 교훈(소표본 IC 과대추정: prm_net_5d_ratio 가 3,552건 +0.141 → 6만건 +0.081)에 따라
      기간을 5배로 늘려 재측정한다.

검정
  A. 전체·연도별 일별 횡단면 Spearman IC (자기상관 때문에 t 값은 참고용, **연도 일관성**이 핵심)
  B. 10분위 스프레드(상위10% − 하위10% 전방 20일 수익) — 방향과 크기
  C. 공매도 데이터 가용성 — 한국은 금지 기간이 길었다(2020-03~2021-05 등). 값이 있는 날/종목 비율.
     **금지 기간에만 죽는 피처면 규제 재도입 시 통째로 무력화**되므로 기간별 가용성을 반드시 본다.
  D. 기존 IC 상위 피처(rsi·bb·macd·거래대금비율)와의 일별 횡단면 중복도 — |r| ≥ 0.5 면 새 정보 아님.

판정: 채택 후보 = 5년 IC |·| ≥ 0.05 AND 연도별 부호 일관(5년 중 4년 이상) AND 기존 피처와 |r| < 0.5.
      통과해도 배포 전 '동일 통과율 기준 강도점수 개선'(§31.8)을 따로 확인해야 한다.
"""
import os
import sys
import glob

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from strategies.daily_loader import load_macro_daily
from fin_paths import STOCK_DB

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_TV = 1_000_000_000
FEATS = ["short_ratio", "short_5d", "per", "pbr", "div"]


def load_panel():
    files = sorted(glob.glob(os.path.join(HERE, "macro_data", "shortfund_kosdaq", "*.csv")))
    sf = pd.concat([pd.read_csv(f, dtype={"code": str, "date": str}) for f in files], ignore_index=True)
    sf["code"] = sf["code"].str.zfill(6)
    sf = sf.sort_values(["code", "date"])
    sf["short_5d"] = sf.groupby("code")["short_ratio"].transform(lambda s: s.rolling(5, min_periods=3).mean())
    sf["per"] = pd.to_numeric(sf["PER"], errors="coerce").where(lambda s: s > 0)
    sf["pbr"] = pd.to_numeric(sf["PBR"], errors="coerce").where(lambda s: s > 0)
    sf["div"] = pd.to_numeric(sf["DIV"], errors="coerce")
    print(f"수집 패널 {len(sf):,}행 | {sf.date.min()}~{sf.date.max()} | 거래일 {sf.date.nunique():,} | 종목 {sf.code.nunique():,}")

    df = load_macro_daily(start_date="20210101").reset_index(drop=True)
    df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df.sort_values(["code", "date"])
    g = df.groupby("code", group_keys=False)
    nxt = g["open"].shift(-1).where(lambda s: s > 0)
    df["fwd20"] = (g["close"].shift(-20).where(lambda s: s > 0) / nxt - 1) * 100
    df["tv_ratio"] = df["trading_value"] / g["trading_value"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=10).mean()).replace(0, np.nan)
    p = df[["code", "date", "fwd20", "trading_value", "tv_ratio"]].merge(
        sf[["code", "date"] + FEATS], on=["code", "date"], how="inner")
    p = p[(p.trading_value >= MIN_TV) & p.fwd20.notna()].copy()
    p["year"] = p["date"].str[:4]
    print(f"유효 종목-일 {len(p):,} (유동성·전방수익 필터 후)")
    return p


def daily_ic(p, col, mask=None):
    sub = p if mask is None else p[mask]
    ics = []
    for _, d in sub.groupby("date"):
        v, f = d[col], d["fwd20"]
        m = v.notna() & f.notna()
        if m.sum() < 30:
            continue
        r = spearmanr(v[m], f[m])[0]
        if not np.isnan(r):
            ics.append(r)
    s = pd.Series(ics)
    if len(s) < 20:
        return None
    return dict(ic=s.mean(), t=s.mean() / (s.std(ddof=1) / np.sqrt(len(s))),
                pos=(s > 0).mean() * 100, n=len(s))


def decile(p, col, mask=None):
    sub = (p if mask is None else p[mask])[["date", col, "fwd20"]].dropna()
    q = sub.groupby("date")[col].transform(
        lambda x: pd.qcut(x.rank(method="first"), 10, labels=False) if len(x) >= 30 else np.nan)
    return sub[q == 9]["fwd20"].mean(), sub[q == 0]["fwd20"].mean()


def main():
    p = load_panel()

    print(f"\n{'=' * 96}\n [C] 공매도 데이터 가용성 — 금지 기간에만 죽는 피처인가\n{'=' * 96}")
    print(f"  {'연도':6s}{'거래일':>8s}{'공매도 비0 종목비율':>20s}{'평균 비중%':>12s}{'PER 유효%':>11s}")
    for y, d in p.groupby("year"):
        nz = (pd.to_numeric(d["short_ratio"], errors="coerce").fillna(0) > 0).mean() * 100
        print(f"  {y:6s}{d.date.nunique():8,}{nz:19.1f}%{d['short_ratio'].mean():12.2f}{d['per'].notna().mean() * 100:10.1f}%")

    print(f"\n{'=' * 96}\n [A-B] 5년 전체 일별 횡단면 IC + 10분위 스프레드\n{'=' * 96}")
    print(f"  {'피처':14s}{'IC':>10s}{'t값':>8s}{'양수일%':>9s}{'일수':>7s}{'상위10%':>10s}{'하위10%':>10s}{'스프레드':>10s}")
    keep = {}
    for c in FEATS:
        r = daily_ic(p, c)
        if r is None:
            continue
        top, bot = decile(p, c)
        keep[c] = r["ic"]
        print(f"  {c:14s}{r['ic']:+10.4f}{r['t']:+8.2f}{r['pos']:8.1f}%{r['n']:7d}{top:+10.3f}{bot:+10.3f}{top - bot:+10.3f}")

    print(f"\n{'=' * 96}\n [A] 연도별 IC — 부호 일관성(핵심)\n{'=' * 96}")
    years = sorted(p.year.unique())
    print(f"  {'피처':14s}" + "".join(f"{y:>11s}" for y in years))
    for c in FEATS:
        line = f"  {c:14s}"
        signs = []
        for y in years:
            r = daily_ic(p, c, p.year == y)
            if r is None:
                line += f"{'-':>11s}"
                continue
            line += f"{r['ic']:+11.4f}"
            signs.append(np.sign(r["ic"]))
        cons = max(signs.count(1), signs.count(-1)) if signs else 0
        print(line + f"   부호일관 {cons}/{len(signs)}")

    print(f"\n{'=' * 96}\n [D] 기존 피처와 일별 횡단면 중복도\n{'=' * 96}")
    import sqlite3
    con = sqlite3.connect(f"file:{STOCK_DB}?mode=ro", uri=True)
    ki = pd.read_sql("SELECT ticker AS code, date, rsi, macd_hist, bb_upper, bb_lower, Close "
                     "FROM korea_indicators WHERE date >= '2021-01-01'", con)
    con.close()
    ki["code"] = ki["code"].astype(str).str.zfill(6)
    ki["date"] = ki["date"].astype(str).str.replace("-", "").str[:8]
    rng = (ki["bb_upper"] - ki["bb_lower"]).replace(0, np.nan)
    ki["bb_pct"] = (ki["Close"] - ki["bb_lower"]) / rng
    q = p.merge(ki[["code", "date", "rsi", "macd_hist", "bb_pct"]], on=["code", "date"], how="left")
    base = ["rsi", "bb_pct", "macd_hist", "tv_ratio"]
    print(f"  {'피처':14s}" + "".join(f"{b:>12s}" for b in base) + "   판정")
    for c in [k for k, v in keep.items() if abs(v) >= 0.05]:
        line = f"  {c:14s}"
        worst = 0.0
        for b in base:
            rs = []
            for _, d in q.groupby("date"):
                m = d[c].notna() & d[b].notna()
                if m.sum() < 30:
                    continue
                r = spearmanr(d.loc[m, c], d.loc[m, b])[0]
                if not np.isnan(r):
                    rs.append(r)
            mr = float(np.mean(rs)) if rs else np.nan
            line += f"{mr:+12.3f}" if not np.isnan(mr) else f"{'-':>12s}"
            if not np.isnan(mr) and abs(mr) > abs(worst):
                worst = mr
        print(line + f"   {'**독립**' if abs(worst) < 0.5 else '중복'}")


if __name__ == "__main__":
    main()
