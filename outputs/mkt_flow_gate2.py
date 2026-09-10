# -*- coding: utf-8 -*-
"""
mkt_flow_gate2.py — 시장 단위 기관/개인 수급 국면의 **견고성** 검정 (2026-09-09, mkt_flow_gate.py 후속·사전등록)

1차(mkt_flow_gate.py) 결과: KOSDAQ 시장 단위 기관 20일 누적 백분위 상위20% 에 진입한 라이브 매매 OOS +6.47%,
하위20% -8.94%; 개인은 거울상(상위20% -5.13%). 9전략 전부 같은 방향, 6년 중 5년 일관, kospi_ret_20d 통제 후 잔존.
하지만 매매 7만 건의 실제 독립 표본은 ~67개월이고, 통제 변수가 KOSPI 였다(코스닥 매매의 진짜 모멘텀은 KOSDAQ 20일).

여기서 가르는 것
  R1 KOSDAQ 20일 시장수익률(진입 전일까지, 유동종목 등가중) 통제 — 기관 백분위가 '코스닥이 오르는 중' 의 다른 이름인가?
     2원표 + 세 변수 동시 순위회귀(기관 백분위, kosdaq_ret20, kospi_ret_20d) + 변수 간 상관.
  R2 월 블록 — 진입월별 평균 net 과 월평균 기관 백분위의 순위상관(IS/OOS/전체), 그리고 kosdaq_ret20 을 뺀 편상관.
     표본 = 월 수(≈67). 부호 일관성(기관 상위 국면 월이 하위 국면 월을 이긴 비율).
  R3 올바른 방향의 게이트 반사실 — '기관 백분위 < T 면 진입 금지'(T=20/30/40) / '개인 백분위 >= T 금지'(T=60/70/80).
     IS/OOS 평균 개선, 매매 유지율, 연도별 개선 부호, 전략별 개선.
  R4 창 민감도 — 누적 10/20/40일, 백분위 룩백 250(트레일링) vs 120: 상위20%-하위20% 스프레드(IS/OOS). 20/250 이 우연인지.
  R5 사용자 원안(종목 크로스, 20일 보유·익절 없음)의 매매를 같은 국면으로 나누면 — 기관 국면 상위에서는 원안이 살아나는가.

사전등록 판정: 게이트 채택 후보 = (a) 월 블록 순위상관 > +0.20 이고 kosdaq_ret20 편상관 후에도 > +0.10,
  (b) 올바른 방향 게이트가 IS·OOS 모두 개선, (c) 연도별 개선 부호 4/6 이상, (d) 창 10/20/40 모두 같은 부호.
  하나라도 깨지면 '국면 특수(2025-26 랠리) 가능성' 으로 보류.
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
from strategies.supply_reversal import SupplyCrossoverStrategy
from strategies._swing_base import _make_trades_for_signals
from strategy_engine import DEFAULT_COSTS

MIN_TV = 1_000_000_000
TRADES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history_v3.csv")


def trailing_pct(s, look):
    return s.rolling(look, min_periods=max(60, look // 2)).apply(lambda w: w.rank(pct=True).iloc[-1] * 100.0, raw=False)


def build_day(df, windows=(10, 20, 40), looks=(250, 120)):
    df = df.copy()
    df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
    df = df.sort_values(["code", "date"])
    g = df.groupby("code", group_keys=False)
    df["ret1"] = g["close"].pct_change()
    liquid = df["trading_value"] >= MIN_TV
    day = df[liquid].groupby("date").agg(ins=("inst_net", "sum"), ind=("individual_net", "sum"),
                                         for_=("foreign_net", "sum"), r1=("ret1", "mean")).sort_index()
    # 진입 전일까지 아는 값만 쓴다(shift 1)
    day["kosdaq_ret20"] = ((1 + day["r1"]).rolling(20).apply(np.prod, raw=True) - 1).shift(1) * 100.0
    for w in windows:
        for c in ("ins", "ind", "for_"):
            acc = day[c].rolling(w, min_periods=w).sum().shift(1)
            for lk in looks:
                day[f"{c}{w}_p{lk}"] = trailing_pct(acc, lk)
    return day


def load_trades(oos):
    t = pd.read_csv(TRADES, low_memory=False, dtype={"code": str})
    t["entry_date"] = t["entry_date"].astype(str).str.replace("-", "").str[:8]
    t = t[t["entry_date"] >= "20210201"].copy()
    t["seg"] = np.where(t["entry_date"] >= oos, "OOS", "IS")
    t["year"] = t["entry_date"].str[:4]
    t["month"] = t["entry_date"].str[:6]
    return t


def rank_partial(y, x, controls):
    """y 를 x 와 controls 의 순위로 동시회귀 → x 의 표준화 계수(순위 편상관 대용)."""
    ry = y.rank()
    cols = [x.rank()] + [c.rank() for c in controls]
    X = np.column_stack([np.ones(len(ry))] + cols)
    b = np.linalg.lstsq(X, ry, rcond=None)[0]
    return b[1] * cols[0].std() / ry.std()


def r1_control(x, oos):
    print("\n [R1] KOSDAQ 20일 시장수익률 통제 (변수: 기관 20일 백분위 p250)")
    z = x.dropna(subset=["ins20_p250", "kosdaq_ret20", "kospi_ret_20d"]).copy()
    print(f"   변수 간 순위상관: 기관p vs kosdaq_ret20 {z['ins20_p250'].rank().corr(z['kosdaq_ret20'].rank()):+.3f} | "
          f"기관p vs kospi_ret_20d {z['ins20_p250'].rank().corr(z['kospi_ret_20d'].rank()):+.3f} | "
          f"kosdaq vs kospi {z['kosdaq_ret20'].rank().corr(z['kospi_ret_20d'].rank()):+.3f}")
    z["m3"] = pd.qcut(z["kosdaq_ret20"].rank(method="first"), 3, labels=["코스닥 하락", "중간", "코스닥 상승"])
    z["i3"] = pd.cut(z["ins20_p250"], [0, 33.3, 66.7, 100.001], labels=["기관 매도", "중간", "기관 매수"], include_lowest=True)
    for seg in ("IS", "OOS", "전체"):
        s = z if seg == "전체" else z[z.seg == seg]
        two = s.pivot_table(index="m3", columns="i3", values="net_pct", aggfunc="mean", observed=True)
        cnt = s.pivot_table(index="m3", columns="i3", values="net_pct", aggfunc="size", observed=True)
        print(f"   [{seg}] 2원표 평균 net% (행=kosdaq_ret20 3분위, 열=기관 백분위 3분위) / 괄호 n")
        for m, row in two.iterrows():
            print("      " + f"{str(m):7s} " + "  ".join(f"{row[c]:+7.2f}({int(cnt.loc[m, c]):,})" for c in two.columns))
        print(f"      단독 순위상관 기관p {s['net_pct'].rank().corr(s['ins20_p250'].rank()):+.4f} | "
              f"kosdaq_ret20 {s['net_pct'].rank().corr(s['kosdaq_ret20'].rank()):+.4f} | "
              f"기관p 편계수(kosdaq·kospi 통제) {rank_partial(s['net_pct'], s['ins20_p250'], [s['kosdaq_ret20'], s['kospi_ret_20d']]):+.4f}")


def r2_monthly(x, oos):
    print("\n [R2] 월 블록 (독립 표본 ≈ 월 수)")
    m = x.dropna(subset=["ins20_p250", "kosdaq_ret20"]).groupby("month").agg(
        net=("net_pct", "mean"), n=("net_pct", "size"), insp=("ins20_p250", "mean"),
        indp=("ind20_p250", "mean"), kq=("kosdaq_ret20", "mean")).reset_index()
    m["seg"] = np.where(m["month"] >= oos[:6], "OOS", "IS")
    for seg in ("IS", "OOS", "전체"):
        s = m if seg == "전체" else m[m.seg == seg]
        rc = s["net"].rank().corr(s["insp"].rank())
        pc = rank_partial(s["net"], s["insp"], [s["kq"]])
        rc_ind = s["net"].rank().corr(s["indp"].rank())
        hi = s[s["insp"] >= s["insp"].median()]["net"]; lo = s[s["insp"] < s["insp"].median()]["net"]
        print(f"   [{seg}] 월 {len(s)} | 월평균net vs 월평균 기관p 순위상관 {rc:+.3f} (kosdaq_ret20 편계수 {pc:+.3f}) | "
              f"vs 개인p {rc_ind:+.3f} | 기관p 상위절반 월 평균 {hi.mean():+.2f} vs 하위절반 {lo.mean():+.2f}")
    # 월별 시계열(요약): 상위20% 국면 월과 하위20% 국면 월 나열
    hi = m[m["insp"] >= 80]; lo = m[m["insp"] <= 20]
    print("   기관p>=80 월: " + ", ".join(f"{r.month}:{r.net:+.1f}" for r in hi.itertuples()))
    print("   기관p<=20 월: " + ", ".join(f"{r.month}:{r.net:+.1f}" for r in lo.itertuples()))


def r3_gates(x, oos):
    print("\n [R3] 올바른 방향 게이트 반사실")
    base = {s: x[x.seg == s]["net_pct"].mean() for s in ("IS", "OOS")}
    rows = [("기관p250 < T 금지", "ins20_p250", "lt", (20, 30, 40)), ("개인p250 >= T 금지", "ind20_p250", "ge", (60, 70, 80))]
    for name, col, op, Ts in rows:
        for T in Ts:
            keep = x[x[col] >= T] if op == "lt" else x[x[col] < T]
            g = {s: keep[keep.seg == s]["net_pct"].mean() for s in ("IS", "OOS")}
            yr = x.groupby("year").apply(lambda d: (d[(d[col] >= T) if op == "lt" else (d[col] < T)]["net_pct"].mean() - d["net_pct"].mean()),
                                         include_groups=False)
            st = x.groupby("strategy").apply(lambda d: (d[(d[col] >= T) if op == "lt" else (d[col] < T)]["net_pct"].mean() - d["net_pct"].mean()),
                                             include_groups=False)
            print(f"   {name} T={T}: IS {base['IS']:+.3f}→{g['IS']:+.3f} ({g['IS'] - base['IS']:+.3f}) | "
                  f"OOS {base['OOS']:+.3f}→{g['OOS']:+.3f} ({g['OOS'] - base['OOS']:+.3f}) | 유지 {len(keep) / len(x) * 100:.0f}% | "
                  f"연도 개선 {int((yr > 0).sum())}/{len(yr)} | 전략 개선 {int((st > 0).sum())}/{len(st)}")


def r4_windows(x, oos):
    print("\n [R4] 창 민감도 — 상위20% - 하위20% 스프레드 (기관 / 개인)")
    print(f"   {'변수':12s} {'IS 스프레드':>12s} {'OOS 스프레드':>12s} {'IS n(상/하)':>16s}")
    for c, lab in (("ins", "기관"), ("ind", "개인"), ("for_", "외국인")):
        for w in (10, 20, 40):
            for lk in (250, 120):
                col = f"{c}{w}_p{lk}"
                z = x.dropna(subset=[col])
                out = []
                for seg in ("IS", "OOS"):
                    s = z[z.seg == seg]
                    hi, lo = s[s[col] >= 80]["net_pct"], s[s[col] <= 20]["net_pct"]
                    out.append((hi.mean() - lo.mean(), len(hi), len(lo)))
                print(f"   {lab}{w}일 p{lk:<4d} {out[0][0]:+12.3f} {out[1][0]:+12.3f} {out[0][1]:>8,}/{out[0][2]:<7,}")


def r5_user_rule(df, day, oos):
    print("\n [R5] 사용자 원안(종목 크로스, 20일 보유·익절 없음) 을 시장 기관 국면으로 나누면")
    sig = SupplyCrossoverStrategy(take_profit_pct=None).signal_df(df)
    trades = _make_trades_for_signals(sig, holding_days=20, strategy_name="xover", costs=DEFAULT_COSTS)
    t = pd.DataFrame([tr.__dict__ for tr in trades])
    t["entry_date"] = t["entry_date"].astype(str).str.replace("-", "").str[:8]
    t = t.merge(day[["ins20_p250", "ind20_p250"]], left_on="entry_date", right_index=True, how="inner").dropna(subset=["ins20_p250"])
    t["seg"] = np.where(t["entry_date"] >= oos, "OOS", "IS")
    t["q"] = pd.cut(t["ins20_p250"], [0, 20, 40, 60, 80, 100.001], labels=["Q1", "Q2", "Q3", "Q4", "Q5"], include_lowest=True)
    tab = t.groupby(["q", "seg"], observed=True)["net_pct"].agg(["mean", "count"]).unstack("seg")
    print(f"   원안 매매 {len(t):,}건 | 무조건 IS {t[t.seg == 'IS']['net_pct'].mean():+.3f} OOS {t[t.seg == 'OOS']['net_pct'].mean():+.3f}")
    for q, r in tab.iterrows():
        print(f"   기관국면 {q}: IS {r[('mean', 'IS')]:+.3f} (n{int(r[('count', 'IS')]):,}) | OOS {r[('mean', 'OOS')]:+.3f} (n{int(r[('count', 'OOS')]):,})")
    k = t[t["ins20_p250"] >= 60]
    print(f"   기관p>=60 에서만 원안 실행: IS {k[k.seg == 'IS']['net_pct'].mean():+.3f} / OOS {k[k.seg == 'OOS']['net_pct'].mean():+.3f} (n{len(k):,})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos", default="20240908")
    a = ap.parse_args()
    df = load_macro_daily(start_date="20210101").reset_index(drop=True)
    day = build_day(df)
    t = load_trades(a.oos)
    cols = [c for c in day.columns if "_p" in c] + ["kosdaq_ret20"]
    x = t.merge(day[cols], left_on="entry_date", right_index=True, how="inner")
    print(f"라이브 매매 {len(x):,}건 조인 | IS<{a.oos}<=OOS")
    r1_control(x, a.oos)
    r2_monthly(x, a.oos)
    r3_gates(x, a.oos)
    r4_windows(x, a.oos)
    r5_user_rule(df, day, a.oos)


if __name__ == "__main__":
    main()
