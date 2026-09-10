# -*- coding: utf-8 -*-
"""
mkt_regime_2018.py — 시장 단위 수급 국면의 **진짜 사전표본(2018~2020) 검정 + 주체 분해** (2026-09-09, 사전등록)

앞선 결과(mkt_flow_gate/gate2): KOSDAQ 시장 단위 '기관 20일 누적 백분위' 상위 국면의 라이브 매매가 OOS(2024-09~)
에서 크게 우수(+15%p 스프레드)하나 IS(2021~24-09)는 약함(월 블록 순위상관 +0.14, 게이트 IS 악화). 판정 '보류'.
2018-07~2020 매매 31,000건은 아직 본 적 없다 — 약세장(2018)·횡보(2019)·COVID 폭락+랠리(2020) 를 포함한 사전표본.

데이터: pykrx get_market_trading_value_by_date(KOSDAQ, detail=True) — 시장 전체 일별 주체별 순매수거래대금(원).
  종목별 수집(backfill_investor_breakdown) 을 기다리지 않고 시장 단위 질문에 바로 답한다. KOSPI 도 병렬 수집(대조).
  ※ 2021~ 구간에서 기존 '유동종목 합산' 시리즈와의 상관으로 정합성 확인.

검정
  S1 정합성: pykrx 시장 전체 기관합계 20일 누적 vs 유동종목 합산(mkt_flow_gate2) 순위상관(2021~).
  S2 기간별(2018-07~2020 / 2021~2024-09 / 2024-09~) × 주체별(기관합계·개인·외국인·금융투자·투신·연기금·사모·기타법인):
     20일 누적 트레일링 백분위(p250, 전일까지) 상위20%-하위20% 매매 net 스프레드, 순위상관, **월 블록 순위상관**.
  S3 10일 창 반복(기관합계·개인) — gate2 R4 에서 IS 가 가장 일관했던 창.
  S4 2018~2020 만의 게이트 반사실: '기관합계 백분위 < 20 금지' 가 그 시기 매매 평균을 개선했나(연도별).

사전등록 판정: 2018~2020 에서 (기관합계 월 블록 순위상관 > +0.15 AND Q5-Q1 스프레드 > 0 AND 게이트 개선 > 0) 이면
  '국면 효과 실재' 로 격상 → 강도점수 피처 후보(IC 재산출 대상). 아니면 '2025-26 특수' 로 최종 보류.

사용: .venv/Scripts/python.exe mkt_regime_2018.py [--refetch]
"""
import os
import sys
import time
import random
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
CACHE = {"KOSDAQ": os.path.join(HERE, "macro_data", "kosdaq_investor_by_date.csv"),
         "KOSPI": os.path.join(HERE, "macro_data", "kospi_investor_by_date.csv")}
INST_DETAIL = ["금융투자", "보험", "투신", "사모", "은행", "기타금융", "연기금"]


def fetch_market(market, start="20180101", end="20260908", refetch=False):
    path = CACHE[market]
    if os.path.exists(path) and not refetch:
        d = pd.read_csv(path, dtype={"date": str}).set_index("date")
        print(f"[{market}] 캐시 {len(d):,}일 {d.index.min()}~{d.index.max()}")
        return d
    import config  # noqa: F401  — KRX 로그인(.env) 이 pykrx import 앞
    from pykrx import stock
    parts = []
    for y in range(int(start[:4]), int(end[:4]) + 1):
        f, t = max(start, f"{y}0101"), min(end, f"{y}1231")
        for attempt in range(3):
            try:
                time.sleep(random.uniform(1.0, 2.0))
                d = stock.get_market_trading_value_by_date(f, t, market, detail=True)
                if d is None or d.empty:
                    raise RuntimeError("빈 응답")
                parts.append(d)
                print(f"  [{market} {y}] {len(d)}일 | 컬럼 {list(d.columns)[:6]}...", flush=True)
                break
            except Exception as e:
                print(f"  [{market} {y}] 오류 {str(e)[:80]} — 15초 대기 ({attempt + 1}/3)", flush=True)
                time.sleep(15)
        else:
            sys.exit(f"[{market} {y}] 수집 실패 — 중단")
    d = pd.concat(parts)
    d.index = pd.to_datetime(d.index).strftime("%Y%m%d")
    d.index.name = "date"
    d = d[~d.index.duplicated()].sort_index()
    if "기관합계" not in d.columns:
        have = [c for c in INST_DETAIL if c in d.columns]
        d["기관합계"] = d[have].sum(axis=1)
    if "외국인합계" not in d.columns and "외국인" in d.columns:
        d["외국인합계"] = d["외국인"] + d.get("기타외국인", 0)
    d.to_csv(path, encoding="utf-8-sig")
    print(f"[{market}] 저장 {len(d):,}일 → {path}")
    return d


def trailing_pct(s, look=250):
    return s.rolling(look, min_periods=120).apply(lambda w: w.rank(pct=True).iloc[-1] * 100.0, raw=False)


def add_regimes(d, investors, windows=(20, 10)):
    out = pd.DataFrame(index=d.index)
    for inv in investors:
        if inv not in d.columns:
            continue
        for w in windows:
            acc = d[inv].astype(float).rolling(w, min_periods=w).sum().shift(1)   # 진입 전일까지
            out[f"{inv}_{w}"] = trailing_pct(acc)
    return out


def load_trades():
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    t["entry_date"] = t["entry_date"].astype(str).str.replace("-", "").str[:8]
    t["year"] = t["entry_date"].str[:4]
    t["month"] = t["entry_date"].str[:6]
    t["period"] = np.select([t["entry_date"] < "20210101", t["entry_date"] < "20240908"],
                            ["A 2018-20 사전표본", "B 2021-24.09 IS"], "C 2024.09- OOS")
    return t


def spread_table(x, cols, label):
    print(f"\n [{label}] 상위20%-하위20% 매매 net 스프레드 / 순위상관 / 월 블록 순위상관 (기간별)")
    print(f"   {'변수':16s}" + "".join(f"{p:>34s}" for p in ("A 2018-20", "B 2021-24.09", "C 2024.09-")))
    for c in cols:
        if c not in x.columns:
            continue
        row = f"   {c:16s}"
        for p in ("A 2018-20 사전표본", "B 2021-24.09 IS", "C 2024.09- OOS"):
            s = x[(x.period == p)].dropna(subset=[c])
            if len(s) < 500:
                row += f"{'(n부족)':>34s}"
                continue
            hi, lo = s[s[c] >= 80]["net_pct"], s[s[c] <= 20]["net_pct"]
            rc = s["net_pct"].rank().corr(s[c].rank())
            m = s.groupby("month").agg(net=("net_pct", "mean"), v=(c, "mean"))
            mrc = m["net"].rank().corr(m["v"].rank())
            row += f"  sp {hi.mean() - lo.mean():+6.2f} rc {rc:+.3f} 월rc {mrc:+.3f}(m{len(m)})"
        print(row)


def gate_2018(x, col="기관합계_20"):
    print(f"\n [S4] 2018-20 사전표본 게이트 반사실 ({col} < T 금지)")
    a = x[(x.period == "A 2018-20 사전표본")].dropna(subset=[col])
    base = a["net_pct"].mean()
    for T in (20, 30, 40):
        k = a[a[col] >= T]
        yr = a.groupby("year").apply(lambda d: d[d[col] >= T]["net_pct"].mean() - d["net_pct"].mean(), include_groups=False)
        print(f"   T={T}: 평균 {base:+.3f}→{k['net_pct'].mean():+.3f} ({k['net_pct'].mean() - base:+.3f}) | 유지 {len(k) / len(a) * 100:.0f}% | "
              "연도별 " + " ".join(f"{y}:{v:+.2f}" for y, v in yr.items()))
    q = pd.cut(a[col], [0, 20, 40, 60, 80, 100.001], labels=["Q1", "Q2", "Q3", "Q4", "Q5"], include_lowest=True)
    print("   5분위 평균: " + " | ".join(f"{k} {v:+.3f}(n{n:,})" for k, v, n in
                                     zip(a.groupby(q, observed=True)["net_pct"].mean().index,
                                         a.groupby(q, observed=True)["net_pct"].mean().values,
                                         a.groupby(q, observed=True)["net_pct"].size().values)))
    # 월 나열: 극단 국면 월의 부호
    m = a.groupby("month").agg(net=("net_pct", "mean"), v=(col, "mean"))
    print("   기관p>=80 월: " + ", ".join(f"{i}:{r.net:+.1f}" for i, r in m[m.v >= 80].iterrows()))
    print("   기관p<=20 월: " + ", ".join(f"{i}:{r.net:+.1f}" for i, r in m[m.v <= 20].iterrows()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true")
    a = ap.parse_args()
    kq = fetch_market("KOSDAQ", refetch=a.refetch)
    kp = fetch_market("KOSPI", refetch=a.refetch)
    investors = ["기관합계", "개인", "외국인합계", "금융투자", "투신", "연기금", "사모", "기타법인", "보험"]
    rq = add_regimes(kq, investors)
    rp = add_regimes(kp, ["기관합계", "개인", "외국인합계"]).add_prefix("KOSPI_")
    t = load_trades()
    x = t.merge(rq, left_on="entry_date", right_index=True, how="inner").merge(rp, left_on="entry_date", right_index=True, how="left")
    print(f"\n라이브 매매 {len(x):,}건 조인 | 기간별 n: " + ", ".join(f"{p} {n:,}" for p, n in x["period"].value_counts().sort_index().items()))

    # S1 정합성: 유동종목 합산 시리즈와 비교(2021~)
    try:
        from strategies.daily_loader import load_macro_daily
        df = load_macro_daily(start_date="20210101").reset_index(drop=True)
        df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
        liq = df[df["trading_value"] >= 1_000_000_000].groupby("date")["inst_net"].sum()
        both = pd.concat([liq.rolling(20).sum(), kq["기관합계"].astype(float).rolling(20).sum()], axis=1, join="inner").dropna()
        both.columns = ["liquid_sum", "pykrx_all"]
        print(f"\n [S1] 정합성: 기관 20일 누적 (유동종목 합산 vs pykrx 시장전체) 순위상관 {both['liquid_sum'].rank().corr(both['pykrx_all'].rank()):+.3f} (일수 {len(both):,})")
    except Exception as e:
        print(f" [S1] 정합성 확인 실패: {e}")

    spread_table(x, [f"{i}_20" for i in investors], "S2 20일 누적 — 주체별")
    spread_table(x, ["기관합계_10", "개인_10", "외국인합계_10", "금융투자_10", "투신_10", "연기금_10", "사모_10", "기타법인_10"], "S3 10일 누적 — 주체별")
    spread_table(x, ["KOSPI_기관합계_20", "KOSPI_개인_20", "KOSPI_외국인합계_20"], "S2b KOSPI 시장 수급(대조)")
    gate_2018(x)
    gate_2018(x, "개인_20")


if __name__ == "__main__":
    main()
