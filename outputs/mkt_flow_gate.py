# -*- coding: utf-8 -*-
"""
mkt_flow_gate.py — 시장 단위 수급 극단(크로스)을 라이브 전략의 진입 게이트로 쓸 수 있나 (2026-09-09, 사전등록)

근거: flow_form_diagnostic F5(2026-09-08) — KOSDAQ 전종목 합산 외국인 20일 누적 5분위 → 시장 전방 20일:
      Q5(최대 순매수) -4.04% / Q1(최대 순매도) +1.07%, 순위상관 -0.25 (KOSPI 는 +0.03 으로 미재현).
질문: ① 이 '시장 상태'가 라이브 전략의 **개별 매매 수익**을 가르는가
      ② 이미 IC 최대가중인 kospi_ret_20d(-0.17, 시장 평균회귀)와 **독립**인가 — 아니면 같은 것의 다른 이름
      ③ 시장 단위 **크로스 이벤트**(개인 누적이 외국인 누적을 위로: 외인 투매→개인 매수) 뒤 시장 수익은?

설계(look-ahead 없음):
  1) 시장 시리즈: KOSDAQ 유동종목(거래대금>=10억) 합산 외국인/기관/개인 일별 순매수 → 20일 누적 → **전일 값**
     (진입일 아침에 알 수 있는 것) → 과거 250거래일 트레일링 백분위(0~100).
  2) trades_history_v3(9전략, 2018~) 중 2021-02~ 매매를 진입일로 조인 → 백분위 5분위별 평균 net_pct, IS/OOS(20240908),
     전략별 Q5 열위, 연도별 일관성.
  3) 독립성: kospi_ret_20d(매매 자체의 피처) 3분위 × 외국인 백분위 3분위 2원표 + 순위 부분상관(두 순위로 회귀).
  4) 게이트 반사실: '외국인 백분위 >= T 면 진입 금지'(T=70/80/90) 적용 시 평균 net_pct·매매수 변화, IS/OOS.
  5) 시장 크로스 이벤트 뒤 시장(등가중) 전방 20/40일 vs 무조건. KOSPI 시리즈도 게이트 변수로 병렬 검정.

사전등록 판정: 게이트 채택 후보 = OOS 평균 net 개선 >= +0.3%p AND IS 도 개선 AND kospi_ret_20d 통제 후에도
              외국인 상위 3분위 열위가 남음. 하나라도 깨지면 기각(= kospi_ret_20d 가 이미 잡고 있는 것).
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

from strategies.daily_loader import load_macro_daily

MIN_TV = 1_000_000_000
TRADES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history_v3.csv")


def market_series(data_dir, label):
    df = load_macro_daily(start_date="20210101", data_dir=data_dir).reset_index(drop=True)
    df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
    df = df.sort_values(["code", "date"])
    g = df.groupby("code", group_keys=False)
    nxt_open = g["open"].shift(-1).where(lambda s: s > 0)
    df["fwd20"] = (g["close"].shift(-20).where(lambda s: s > 0) / nxt_open - 1.0) * 100.0
    df["fwd40"] = (g["close"].shift(-40).where(lambda s: s > 0) / nxt_open - 1.0) * 100.0
    liquid = df["trading_value"] >= MIN_TV
    has_ind = "individual_net" in df.columns
    agg = dict(for_=("foreign_net", "sum"), ins_=("inst_net", "sum"), fwd20=("fwd20", "mean"), fwd40=("fwd40", "mean"))
    if has_ind:
        agg["ind_"] = ("individual_net", "sum")
    day = df[liquid].groupby("date").agg(**agg).sort_index()
    for c in ("for_", "ins_") + (("ind_",) if has_ind else ()):
        day[c + "20"] = day[c].rolling(20, min_periods=20).sum()
        known = day[c + "20"].shift(1)                         # 진입일 아침에 아는 값 = 전일 누적
        day[c + "pct"] = known.rolling(250, min_periods=120).apply(
            lambda s: s.rank(pct=True).iloc[-1] * 100.0, raw=False)
    if has_ind:
        # 시장 크로스: 개인 20일누적이 외국인 20일누적을 위로 (외인 투매→개인 매수) / 반대 방향
        up = (day["ind_20"] > day["for_20"]) & (day["ind_20"].shift(1) <= day["for_20"].shift(1))
        dn = (day["ind_20"] < day["for_20"]) & (day["ind_20"].shift(1) >= day["for_20"].shift(1))
        day["x_ind_up"], day["x_ind_dn"] = up, dn
    day.attrs["label"] = label
    return day


def load_trades(oos):
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    t["entry_date"] = t["entry_date"].astype(str).str.replace("-", "").str[:8]
    t = t[t["entry_date"] >= "20210201"].copy()
    t["seg"] = np.where(t["entry_date"] >= oos, "OOS", "IS")
    t["year"] = t["entry_date"].str[:4]
    return t


def gate_report(t, day, var, name):
    col = var + "pct"
    x = t.merge(day[[col]], left_on="entry_date", right_index=True, how="inner").dropna(subset=[col])
    x["q"] = pd.cut(x[col], [0, 20, 40, 60, 80, 100.001], labels=["Q1 최대매도", "Q2", "Q3", "Q4", "Q5 최대매수"], include_lowest=True)
    print(f"\n ── {name}: 백분위 5분위별 라이브 매매 평균 net% (매매 {len(x):,}건) ──")
    tab = x.groupby(["q", "seg"], observed=True)["net_pct"].agg(["mean", "count"]).unstack("seg")
    print(f"   {'분위':10s} {'IS평균':>8s} {'IS n':>7s} {'OOS평균':>8s} {'OOS n':>7s}")
    for q, r in tab.iterrows():
        print(f"   {str(q):10s} {r[('mean', 'IS')]:+8.3f} {int(r[('count', 'IS')]):7,} {r[('mean', 'OOS')]:+8.3f} {int(r[('count', 'OOS')]):7,}")
    top = x[x[col] >= 80]["net_pct"]; rest = x[x[col] < 80]["net_pct"]
    print(f"   전체: 상위20%(Q5) {top.mean():+.3f} (n{len(top):,}) vs 나머지 {rest.mean():+.3f} (n{len(rest):,}) → 차이 {top.mean() - rest.mean():+.3f}%p")
    # 연도별 Q5-나머지 일관성
    yr = x.groupby("year").apply(lambda g: pd.Series(dict(
        q5=g.loc[g[col] >= 80, "net_pct"].mean(), rest=g.loc[g[col] < 80, "net_pct"].mean(),
        n5=(g[col] >= 80).sum())), include_groups=False)
    print("   연도별 Q5-나머지: " + " | ".join(f"{y} {r['q5'] - r['rest']:+.2f}(n{int(r['n5'])})" for y, r in yr.iterrows() if r["n5"] >= 30))
    # 전략별
    st = x.groupby("strategy").apply(lambda g: pd.Series(dict(
        d=g.loc[g[col] >= 80, "net_pct"].mean() - g.loc[g[col] < 80, "net_pct"].mean(), n5=(g[col] >= 80).sum())), include_groups=False)
    print("   전략별 Q5-나머지: " + " | ".join(f"{s} {r['d']:+.2f}" for s, r in st.iterrows() if r["n5"] >= 100))
    # 독립성: kospi_ret_20d 통제
    if "kospi_ret_20d" in x.columns and x["kospi_ret_20d"].notna().sum() > 1000:
        z = x.dropna(subset=["kospi_ret_20d"]).copy()
        z["k3"] = pd.qcut(z["kospi_ret_20d"].rank(method="first"), 3, labels=["시장 하락", "중간", "시장 상승"])
        z["f3"] = pd.cut(z[col], [0, 33.3, 66.7, 100.001], labels=["외인 매도", "중간", "외인 매수"], include_lowest=True)
        two = z.pivot_table(index="k3", columns="f3", values="net_pct", aggfunc="mean", observed=True)
        print("   2원표(행=kospi_ret_20d 3분위, 열=외국인 백분위 3분위) 평균 net%:")
        print("      " + two.round(3).to_string().replace("\n", "\n      "))
        rk_y, rk_f, rk_k = z["net_pct"].rank(), z[col].rank(), z["kospi_ret_20d"].rank()
        X = np.column_stack([np.ones(len(z)), rk_f, rk_k])
        beta = np.linalg.lstsq(X, rk_y, rcond=None)[0]
        r_f_only = rk_y.corr(rk_f)
        r_k_only = rk_y.corr(rk_k)
        # 표준화 편회귀계수(순위): 외국인 백분위의 독립 기여
        sd = rk_y.std()
        print(f"   순위상관 단독: 외국인 {r_f_only:+.4f} / kospi_ret_20d {r_k_only:+.4f} | "
              f"동시회귀 표준화계수: 외국인 {beta[1] * rk_f.std() / sd:+.4f} / kospi {beta[2] * rk_k.std() / sd:+.4f}")
    # 게이트 반사실
    print("   게이트 반사실(백분위 >= T 진입 금지):")
    base_is, base_oos = x[x.seg == "IS"]["net_pct"].mean(), x[x.seg == "OOS"]["net_pct"].mean()
    for T in (70, 80, 90):
        k = x[x[col] < T]
        gi, go = k[k.seg == "IS"]["net_pct"].mean(), k[k.seg == "OOS"]["net_pct"].mean()
        print(f"      T={T}: IS {base_is:+.3f}→{gi:+.3f} ({gi - base_is:+.3f}) | OOS {base_oos:+.3f}→{go:+.3f} ({go - base_oos:+.3f}) "
              f"| 매매 유지 {len(k) / len(x) * 100:.0f}%")
    return x, col


def crossover_events(day):
    if "x_ind_up" not in day.columns:
        return
    d = day.dropna(subset=["fwd20"])
    print(f"\n ── 시장 크로스 이벤트 ({day.attrs['label']}) → 시장(등가중) 전방 수익 ──")
    print(f"   무조건 평균: 전방20일 {d['fwd20'].mean():+.3f}% / 전방40일 {d['fwd40'].mean():+.3f}%  (일수 {len(d):,})")
    for flag, name in (("x_ind_up", "개인이 외국인 위로(외인 투매→개인 매수)"), ("x_ind_dn", "외국인이 개인 위로(외인 유입)")):
        e = d[d[flag] == True]  # noqa: E712
        if len(e) == 0:
            continue
        print(f"   {name}: 이벤트 {len(e)}회 | 전방20일 {e['fwd20'].mean():+.3f}% (양수 {(e['fwd20'] > 0).mean() * 100:.0f}%) "
              f"| 전방40일 {e['fwd40'].mean():+.3f}%")
        print("      이벤트 일자/전방20: " + ", ".join(f"{i[:6]}:{v:+.1f}" for i, v in e["fwd20"].items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos", default="20240908")
    a = ap.parse_args()
    t = load_trades(a.oos)
    print(f"라이브 매매 {len(t):,}건 (2021-02~, 전략 {t['strategy'].nunique()}) | IS<{a.oos}<=OOS | 무게이트 평균 "
          f"IS {t[t.seg == 'IS']['net_pct'].mean():+.3f} / OOS {t[t.seg == 'OOS']['net_pct'].mean():+.3f}")
    kq = market_series(None, "KOSDAQ")
    kp = market_series("macro_data/daily_kospi", "KOSPI")
    gate_report(t, kq, "for_", "KOSDAQ 외국인 20일누적")
    gate_report(t, kq, "ins_", "KOSDAQ 기관 20일누적")
    if "ind_pct" in kq.columns:
        gate_report(t, kq, "ind_", "KOSDAQ 개인 20일누적")
    gate_report(t, kp, "for_", "KOSPI 외국인 20일누적(위험선호 대리)")
    crossover_events(kq)
    crossover_events(kp)


if __name__ == "__main__":
    main()
