# -*- coding: utf-8 -*-
"""
investor_stock_ic.py — 종목 단위 '주체별' 수급의 정보량 + 프로그램매매 대체경로 검정 (2026-09-10, 사전등록)

배경(확정된 것)
  · 종목 단위 외국인/기관 **합계** 순매수: IC ≈ 0 (형태 4종 × 2시장, 2026-09-08).
  · 종목 단위 **프로그램매매**(키움 ka90013): IC +0.141 (OOS +0.111) — 수급 계열 유일 생존.
  · 시장 단위(2026-09-09): 주체 분해 결과 **금융투자**(증권사 자기매매 = 프로그램/차익 주체)만
    2018-20 / 2021-24 / 2024- **세 기간 전부** 같은 부호(월rc +0.16/+0.32/+0.54).

가설: '기관 합계'가 IC 0 인 것은 정보가 없어서가 아니라 **주체가 상쇄**되기 때문이다.
      기관 = 금융투자(기계적·차익) + 투신(재량) + 연기금(역추세 리밸런싱) + 사모 + 보험/은행.
      분해하면 종목 단위에서도 금융투자만 살아 있을 것이다.

검정 (KOSDAQ 2021-01~2026-09, 일별 횡단면 Spearman IC, IS<20240908<=OOS)
  A. 대체경로: pykrx 금융투자 5일 순매수합 vs 키움 prm_net_5d_raw (같은 종목-일) 상관.
     높으면 키움 ka90013(주 1회·종목당 30초)을 **pykrx 시장 일괄(매일 1콜)** 로 대체/보완 가능.
  B. 주체별 5일 순매수 ÷ 20일 평균거래대금 의 IC / 10분위 스프레드 (연기금·투신·금융투자·사모·기타법인
     + 대조로 외국인·기관합계·개인).
  C. 주체별 20일 누적이 개인 20일 누적을 위로 뚫는 **종목 단위 크로스**(사용자 원안의 주체 분해판):
     신호일 전방 20일 수익 vs 같은 날 유니버스 평균(= 알파). 어느 주체의 크로스에 정보가 있나.
  D. 라이브 매매 조인: 주체별 피처 5분위 × net_pct (IS/OOS) — 실전 매매에서의 확인.

사전등록 판정: 피처 후보 = |IC| >= 0.05 AND IS·OOS 같은 부호 AND 10분위 스프레드 방향 일치.
  크로스 후보 = 알파 >= +0.3%p AND IS·OOS 같은 부호.
"""
import os
import sys
import glob
import argparse

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from strategies.daily_loader import load_macro_daily

HERE = os.path.dirname(os.path.abspath(__file__))
INV_DIR = os.path.join(HERE, "macro_data", "investor_kosdaq")
TRADES = os.path.join(HERE, "trades_history_v3.csv")
OOS = "20240908"
MIN_TV = 1_000_000_000
HOLD = 20
ACTORS = ["fininv", "trust", "pension", "private", "corp"]
KOR = {"fininv": "금융투자", "trust": "투신", "pension": "연기금", "private": "사모", "corp": "기타법인",
       "foreign": "외국인", "inst": "기관합계", "indiv": "개인"}


def load_panel():
    print("일봉 로드...", flush=True)
    df = load_macro_daily(start_date="20210101").reset_index(drop=True)
    df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
    df["code"] = df["code"].astype(str).str.zfill(6)

    print(f"주체별 수급 로드 ({len(glob.glob(os.path.join(INV_DIR, '*.csv'))):,}일)...", flush=True)
    parts = []
    for f in sorted(glob.glob(os.path.join(INV_DIR, "*.csv"))):
        d = pd.read_csv(f, dtype={"code": str, "date": str},
                        usecols=["code", "date"] + [f"{a}_net" for a in ACTORS])
        parts.append(d)
    inv = pd.concat(parts, ignore_index=True)
    inv["code"] = inv["code"].str.zfill(6)
    print(f"  주체 패널 {len(inv):,}행", flush=True)

    m = df.merge(inv, on=["code", "date"], how="left")
    print(f"조인 {len(m):,}행 | 주체 결측률 " +
          ", ".join(f"{KOR[a]} {m[f'{a}_net'].isna().mean() * 100:.0f}%" for a in ACTORS), flush=True)
    return m.sort_values(["code", "date"]).reset_index(drop=True)


def add_features(df):
    g = df.groupby("code", group_keys=False)
    nxt_open = g["open"].shift(-1).where(lambda s: s > 0)
    df["fwd20"] = (g["close"].shift(-HOLD).where(lambda s: s > 0) / nxt_open - 1.0) * 100.0
    tv20 = g["trading_value"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    tv20 = tv20.where(tv20 > 0)

    cols = {a: f"{a}_net" for a in ACTORS}
    cols["foreign"] = "foreign_net"
    cols["inst"] = "inst_net"
    if "individual_net" in df.columns:
        cols["indiv"] = "individual_net"
    for key, src in cols.items():
        s5 = g[src].transform(lambda s: s.fillna(0.0).rolling(5, min_periods=5).sum())
        df[f"{key}_5d"] = s5 / (tv20 * 5)
        df[f"{key}_20acc"] = g[src].transform(lambda s: s.fillna(0.0).rolling(20, min_periods=20).sum())
    df["seg"] = np.where(df["date"] >= OOS, "OOS", "IS")
    return df


def daily_ic(df, feat, mask):
    sub = df.loc[mask & df[feat].notna() & df["fwd20"].notna(), ["date", feat, "fwd20"]]
    ics = [d[feat].rank().corr(d["fwd20"].rank()) for _, d in sub.groupby("date") if len(d) >= 30]
    ics = pd.Series(ics).dropna()
    if len(ics) == 0:
        return None
    return dict(ic=ics.mean(), t=ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))), n=len(ics))


def decile(df, feat, mask):
    sub = df.loc[mask & df[feat].notna() & df["fwd20"].notna(), ["date", feat, "fwd20"]].copy()
    sub["d"] = sub.groupby("date")[feat].transform(
        lambda s: pd.qcut(s.rank(method="first"), 10, labels=False) if len(s) >= 30 else np.nan)
    sub = sub.dropna(subset=["d"])
    return sub[sub.d == 9]["fwd20"].mean(), sub[sub.d == 0]["fwd20"].mean()


def part_a(df):
    print(f"\n{'=' * 92}\n [A] 대체경로: pykrx 금융투자 5일합  vs  키움 프로그램매매 5일합(prm_net_5d_raw)\n{'=' * 92}")
    if not os.path.exists(TRADES):
        print("  trades_history_v3.csv 없음 — 생략")
        return
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str},
                    usecols=["date", "code", "prm_net_5d_raw", "prm_net_5d_ratio"])
    t["date"] = t["date"].astype(str).str.replace("-", "").str[:8]
    t["code"] = t["code"].str.zfill(6)
    t = t[t["prm_net_5d_raw"].notna()].drop_duplicates(["code", "date"])
    g = df.groupby("code", group_keys=False)
    tmp = df[["code", "date"]].copy()
    tmp["fin5_raw"] = g["fininv_net"].transform(lambda s: s.fillna(0.0).rolling(5, min_periods=5).sum())
    tmp["fin5_clip"] = g["fininv_net"].transform(
        lambda s: s.fillna(0.0).clip(lower=0).rolling(5, min_periods=5).sum())
    x = t.merge(tmp, on=["code", "date"], how="inner").dropna(subset=["fin5_raw"])
    if len(x) < 100:
        print(f"  대조 표본 부족 ({len(x)})")
        return
    x["kw_won"] = x["prm_net_5d_raw"] * 1e6          # 키움 백만원 → 원
    print(f"  대조 표본 {len(x):,}건 ({x['date'].min()}~{x['date'].max()})")
    for a, b, lab in (("kw_won", "fin5_clip", "절단합 vs 절단합"), ("kw_won", "fin5_raw", "절단합 vs 부호합")):
        sp = x[a].rank().corr(x[b].rank())
        pe = x[a].corr(x[b])
        print(f"   {lab:20s} 순위상관 {sp:+.4f} | 피어슨 {pe:+.4f} | 중앙비율(fin/kw) "
              f"{(x[b] / x[a].replace(0, np.nan)).median():.3f}")
    print("   → 순위상관 0.8+ 면 pykrx(매일 시장 일괄 1콜)로 키움 ka90013(주1회·종목당 30초) 대체 가능")


def part_b(df):
    print(f"\n{'=' * 92}\n [B] 주체별 5일 순매수÷20일평균거래대금 → 전방20일 IC (KOSDAQ)\n{'=' * 92}")
    liq = (df["trading_value"] >= MIN_TV) & df["fwd20"].notna()
    print(f"  유효 종목-일 {int(liq.sum()):,}")
    print(f"  {'주체':10s} {'구간':5s} {'IC':>9s} {'t':>7s} {'일수':>6s} {'상위10%':>9s} {'하위10%':>9s} {'스프레드':>9s}")
    keys = ACTORS + ["foreign", "inst"] + (["indiv"] if "indiv_5d" in df.columns else [])
    verdict = {}
    for k in keys:
        f = f"{k}_5d"
        row = {}
        for seg in ("IS", "OOS"):
            m = liq & (df["seg"] == seg)
            r = daily_ic(df, f, m)
            if r is None:
                continue
            top, bot = decile(df, f, m)
            row[seg] = r["ic"]
            print(f"  {KOR[k]:10s} {seg:5s} {r['ic']:+9.4f} {r['t']:+7.2f} {r['n']:6,} "
                  f"{top:+9.3f} {bot:+9.3f} {top - bot:+9.3f}")
        if len(row) == 2:
            ok = (abs(row['IS']) >= 0.05) and (np.sign(row['IS']) == np.sign(row['OOS']))
            verdict[k] = (row, ok)
    print("\n  [판정] 피처 후보 = |IC(IS)| >= 0.05 AND IS·OOS 같은 부호")
    for k, (row, ok) in verdict.items():
        print(f"   {KOR[k]:10s} IS {row['IS']:+.4f} OOS {row['OOS']:+.4f} → {'후보 ✔' if ok else '기각'}")


def part_c(df):
    print(f"\n{'=' * 92}\n [C] 종목 단위 크로스의 주체 분해 — '{'개인'}' 20일누적을 위로 뚫는 주체별 신호\n{'=' * 92}")
    if "indiv_20acc" not in df.columns:
        print("  개인 데이터 없음 — 생략")
        return
    liq = (df["trading_value"] >= MIN_TV) & df["fwd20"].notna()
    g = df.groupby("code")
    ind_prev = g["indiv_20acc"].shift(1)
    print(f"  {'주체':10s} {'구간':5s} {'신호수':>8s} {'신호 전방20':>12s} {'유니버스':>10s} {'알파':>9s}")
    for k in ACTORS + ["foreign", "inst"]:
        col = f"{k}_20acc"
        cross = (df[col] > df["indiv_20acc"]) & (g[col].shift(1) <= ind_prev) & df[col].notna()
        for seg in ("IS", "OOS"):
            m = liq & (df["seg"] == seg)
            sig = m & cross
            n = int(sig.sum())
            if n < 200:
                continue
            a = df.loc[sig, "fwd20"].mean()
            b = df.loc[m, "fwd20"].mean()
            print(f"  {KOR[k]:10s} {seg:5s} {n:8,} {a:+12.3f} {b:+10.3f} {a - b:+9.3f}")


def part_d(df):
    print(f"\n{'=' * 92}\n [D] 라이브 매매(trades_history_v3) 조인 — 주체별 피처 5분위 × 실제 net%\n{'=' * 92}")
    if not os.path.exists(TRADES):
        return
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    t["entry_date"] = t["entry_date"].astype(str).str.replace("-", "").str[:8]
    t["code"] = t["code"].str.zfill(6)
    t = t[t["entry_date"] >= "20210201"]
    feats = [f"{k}_5d" for k in ACTORS]
    x = t.merge(df[["code", "date"] + feats], left_on=["code", "entry_date"], right_on=["code", "date"], how="inner")
    x["seg"] = np.where(x["entry_date"] >= OOS, "OOS", "IS")
    print(f"  조인 {len(x):,}건")
    for f in feats:
        z = x.dropna(subset=[f]).copy()
        if len(z) < 1000:
            continue
        z["q"] = z.groupby("entry_date")[f].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if len(s) >= 10 else np.nan)
        z = z.dropna(subset=["q"])
        r = z.pivot_table(index="q", columns="seg", values="net_pct", aggfunc="mean")
        line = " | ".join(f"Q{int(q) + 1} IS {row.get('IS', np.nan):+6.2f} OOS {row.get('OOS', np.nan):+6.2f}"
                          for q, row in r.iterrows())
        print(f"  {KOR[f.replace('_5d', '')]:8s} {line}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="건너뛸 파트(예: acd)")
    a = ap.parse_args()
    df = add_features(load_panel())
    if "a" not in a.skip:
        part_a(df)
    if "b" not in a.skip:
        part_b(df)
    if "c" not in a.skip:
        part_c(df)
    if "d" not in a.skip:
        part_d(df)


if __name__ == "__main__":
    main()
