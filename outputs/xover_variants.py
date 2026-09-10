# -*- coding: utf-8 -*-
"""
xover_variants.py — 수급 크로스: 무작위 대조 + 경제논리 변형 검정 (2026-09-09, 사전등록)

사용자 질문: "기관·외국인·개인 수급이 크로스할 때 어떻게 하면 수익을 낼 수 있나."

지금까지 확정된 것(2026-09-08): 원안(스마트머니 20일누적이 개인 위로 역전 → +2% 익절/20일 만기)은
KOSDAQ 65,225건 승률 86.4% / 평균 -0.50%, 손절 24조합 전부 음수, KOSPI +9.4% CAGR 은 베타.

이 스크립트가 새로 가르는 것
  P0 무작위 대조 : 크로스 신호와 **같은 날·같은 개수**의 무작위 유동 종목에 **같은 청산**(+2% 일중고가 익절
                   / 20일 만기)을 적용. 승률·익절비중·만기 꼬리가 같다면 86% 는 신호가 아니라 청산 규칙의 산물.
  V1 역크로스     : 개인 20일누적이 스마트머니 위로 뚫는 종목(외인·기관이 개인에게 던진 종목) → 평균회귀 매수.
                   시장단위에서 확인된 '외국인 극단 매도 → 반등'(-0.25) 의 종목단위 대응물.
  V2 매집형       : 원안 크로스 중 과거 20일 주가수익률 <= 0 ("아직 안 오른" 매집). 늦은 크로스의 꼬리를 거르는가.
  V3 늦은형       : 원안 크로스 중 과거 20일 수익률 > +10% (이미 오른 뒤). 꼬리가 여기 사는지 확인용 — 채택 대상 아님.
  각 변형: 익절 없는 20일 보유(신호의 순수 정보) + 원안 청산(+2% 익절). KOSDAQ·KOSPI. IS/OOS(20240908).
  알파 = 변형 평균 net - 같은 날·같은 개수 무작위 진입의 평균 net (같은 하네스 → 베타·비용 자동 상쇄).

사전등록 판정: 채택 후보 = OOS 평균 > 0 AND 무작위 대비 알파 >= +0.3%p AND IS 알파도 양수. 아니면 기각.

사용: python xover_variants.py [--market kosdaq|kospi|both] [--seeds 2] [--oos 20240908]
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
from strategies._swing_base import _make_trades_for_signals, _make_trades_with_stops
from strategy_engine import DEFAULT_COSTS
from capital_simulator import simulate_capital

HOLD = 20
MIN_TV = 1_000_000_000
DATA_DIRS = {"kosdaq": None, "kospi": "macro_data/daily_kospi"}


class XoverVariant(SupplyCrossoverStrategy):
    """원안 SupplyCrossoverStrategy 의 신호 계산을 변형만 바꿔 재사용(청산 하네스 동일)."""

    def __init__(self, variant="base", **kw):
        super().__init__(**kw)
        self.variant = variant
        self.name = f"xover_{variant}"

    def signal_df(self, df):
        df = df.sort_values(["code", "date"]).copy()
        if "individual_net" not in df.columns or df["individual_net"].notna().sum() == 0:
            df["signal"] = False
            return df
        N = self.accum_days
        gb = df.groupby("code")
        for col, src in (("_ind", "individual_net"), ("_for", "foreign_net"), ("_ins", "inst_net")):
            df[col] = gb[src].transform(lambda s: s.fillna(0.0).rolling(N, min_periods=N).sum())
        df["_mom20"] = gb["close"].transform(lambda s: s / s.shift(20) - 1.0)
        gb = df.groupby("code")
        ind_prev = gb["_ind"].shift(1)

        def _x(col, up):
            prev = gb[col].shift(1)
            if up:      # 전일 스마트 <= 개인 → 당일 스마트 > 개인 (원안)
                return (df[col] > df["_ind"]) & (prev <= ind_prev)
            else:       # 전일 스마트 >= 개인 → 당일 스마트 < 개인 (역크로스: 개인이 위로)
                return (df[col] < df["_ind"]) & (prev >= ind_prev)

        up = self.variant != "down"
        ins_x, for_x = _x("_ins", up), _x("_for", up)
        cross = (ins_x & for_x) if self.smart_mode == "and" else (ins_x | for_x)
        cond = cross & df["_ind"].notna() & df["_for"].notna() & df["_ins"].notna()
        if self.variant == "accum":
            cond = cond & (df["_mom20"] <= 0.0)
        elif self.variant == "late":
            cond = cond & (df["_mom20"] > 0.10)
        if "trading_value" in df.columns:
            cond = cond & (df["trading_value"] >= self.min_tv)
        df["signal"] = cond.fillna(False)
        return df


def random_like(sig_df, seed):
    """sig_df 의 신호와 같은 날·같은 개수의 무작위 유동 종목을 signal 로 갖는 df (같은 자격 조건)."""
    df = sig_df.copy()
    eligible = df["_ind"].notna() & df["_for"].notna() & df["_ins"].notna()
    if "trading_value" in df.columns:
        eligible &= df["trading_value"] >= MIN_TV
    need = df.loc[df["signal"], "date"].value_counts()
    rng = np.random.default_rng(seed)
    cand = df.loc[eligible & df["date"].isin(need.index), ["date"]].copy()
    cand["_k"] = rng.random(len(cand))
    cand = cand.sort_values(["date", "_k"])
    cand["_rank"] = cand.groupby("date").cumcount()
    cand["_need"] = cand["date"].map(need).values
    chosen = cand.index[cand["_rank"] < cand["_need"]]
    df["signal"] = False
    df.loc[chosen, "signal"] = True
    return df


def run_exit(sig_df, tp, name):
    if tp is None:
        return _make_trades_for_signals(sig_df, holding_days=HOLD, strategy_name=name, costs=DEFAULT_COSTS)
    return _make_trades_with_stops(sig_df, holding_days=HOLD, strategy_name=name, costs=DEFAULT_COSTS,
                                   take_profit_pct=tp, tp_fill="high")


def summ(trades, oos, with_cagr=False):
    if not trades:
        return None
    x = pd.DataFrame([t.__dict__ for t in trades])
    x["entry_date"] = x["entry_date"].astype(str).str.replace("-", "").str[:8]
    ins = x[x["entry_date"] < oos]["net_pct"]
    oo = x[x["entry_date"] >= oos]["net_pct"]
    out = dict(n=len(x), win=(x["net_pct"] > 0).mean() * 100, avg=x["net_pct"].mean(),
               is_avg=ins.mean() if len(ins) else np.nan, oos_avg=oo.mean() if len(oo) else np.nan,
               oos_n=len(oo), tp_pct=np.nan, hold_avg=np.nan)
    if "exit_reason" in x.columns:
        mix = x["exit_reason"].value_counts(normalize=True) * 100
        out["tp_pct"] = mix.get("take_profit", np.nan)
        h = x.loc[x["exit_reason"] == "hold_exit", "net_pct"]
        out["hold_avg"] = h.mean() if len(h) else np.nan
    if with_cagr:
        sim = simulate_capital(trades) or {}
        out["cagr"], out["mdd"] = sim.get("cagr_pct"), sim.get("real_mdd_pct")
    return out


def fmt(label, s, alpha=None):
    if s is None:
        return f" {label:22s} (매매 없음)"
    a = "" if alpha is None else f" | 알파(vs무작위) {alpha['avg']:+.3f} IS {alpha['is']:+.3f} OOS {alpha['oos']:+.3f}"
    tp = "" if np.isnan(s["tp_pct"]) else f" | 익절 {s['tp_pct']:.0f}% 만기평균 {s['hold_avg']:+.2f}"
    cg = f" | CAGR {s['cagr']} MDD {s['mdd']}" if "cagr" in s else ""
    return (f" {label:22s} n {s['n']:>6,} 승률 {s['win']:5.1f}% 평균 {s['avg']:+.3f} "
            f"IS {s['is_avg']:+.3f} OOS {s['oos_avg']:+.3f}(n{s['oos_n']:,}){tp}{a}{cg}")


def alpha_of(s, r):
    return dict(avg=s["avg"] - r["avg"], is_=None, **{"is": s["is_avg"] - r["is_avg"], "oos": s["oos_avg"] - r["oos_avg"]})


def run_market(mk, oos, seeds):
    print(f"\n{'=' * 96}\n {mk.upper()}  (IS < {oos} <= OOS) — 비용 포함 net%, 진입 다음날 시가, 보유 {HOLD}일\n{'=' * 96}")
    df = load_macro_daily(start_date="20210101", data_dir=DATA_DIRS[mk]).reset_index(drop=True)
    base_sig = XoverVariant("base", take_profit_pct=2.0).signal_df(df)
    n_sig = int(base_sig["signal"].sum())
    print(f" 원안 크로스 신호 {n_sig:,}건 | 종목 {df['code'].nunique():,} | 일수 {df['date'].nunique():,}")

    # ── P0: 원안(+2% 익절) vs 같은 날·같은 개수 무작위(+2% 익절) ──
    print("\n [P0] 청산 규칙 착시 검정 — 원안 vs 무작위 진입, 청산 동일(+2% 일중고가 익절 / 20일 만기)")
    s_base = summ(run_exit(base_sig, 2.0, "xover_base_tp2"), oos, with_cagr=True)
    print(fmt("원안 크로스 +2%익절", s_base))
    rs = []
    for k in range(seeds):
        r = summ(run_exit(random_like(base_sig, 1000 + k), 2.0, f"random_tp2_{k}"), oos)
        rs.append(r)
        print(fmt(f"무작위 진입 +2%익절 #{k + 1}", r))
    r_mean = {key: np.nanmean([r[key] for r in rs]) for key in ("win", "avg", "is_avg", "oos_avg", "tp_pct", "hold_avg")}
    print(f"   → 원안-무작위 차이: 승률 {s_base['win'] - r_mean['win']:+.1f}%p | 평균 {s_base['avg'] - r_mean['avg']:+.3f}%p "
          f"| 익절비중 {s_base['tp_pct'] - r_mean['tp_pct']:+.1f}%p | 만기꼬리 {s_base['hold_avg'] - r_mean['hold_avg']:+.2f}%p")

    # ── V1~V3: 변형별 (익절 없음 = 순수 정보 / +2% 익절 = 원안 청산), 알파는 매칭 무작위 대비 ──
    print("\n [V] 변형 — 각 행의 알파 = 같은 날·같은 개수 무작위 진입(동일 청산) 대비")
    results = {}
    for variant, label in (("base", "원안(스마트 위로)"), ("down", "V1 역크로스(개인 위로)"),
                           ("accum", "V2 매집형(20일수익<=0)"), ("late", "V3 늦은형(20일수익>10%)")):
        sig = XoverVariant(variant).signal_df(df) if variant != "base" else base_sig
        n = int(sig["signal"].sum())
        print(f"\n  ▸ {label}  신호 {n:,}건")
        for tp, tag in ((None, "20일 보유"), (2.0, "+2% 익절")):
            s = summ(run_exit(sig, tp, f"xover_{variant}_{tag}"), oos, with_cagr=(tp is not None))
            rr = [summ(run_exit(random_like(sig, 2000 + k), tp, "rnd"), oos) for k in range(max(1, seeds - 1))]
            r = {key: np.nanmean([q[key] for q in rr if q]) for key in ("avg", "is_avg", "oos_avg")} if all(rr) else None
            al = dict(avg=s["avg"] - r["avg"], **{"is": s["is_avg"] - r["is_avg"], "oos": s["oos_avg"] - r["oos_avg"]}) if (s and r) else None
            print(fmt(f"  {tag}", s, al))
            results[(variant, tag)] = (s, al)

    # ── 사전등록 판정 ──
    print("\n [판정] 채택 후보 = OOS 평균 > 0 AND 무작위 대비 알파 >= +0.3%p AND IS 알파 > 0")
    for (variant, tag), (s, al) in results.items():
        if variant == "late" or s is None or al is None:
            continue
        ok = (s["oos_avg"] > 0) and (al["avg"] >= 0.3) and (al["is"] > 0)
        print(f"   {variant:6s} {tag:8s} → {'후보 ✔' if ok else '기각'}  (OOS {s['oos_avg']:+.3f}, 알파 {al['avg']:+.3f}, IS알파 {al['is']:+.3f})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="both", choices=["kosdaq", "kospi", "both"])
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--oos", default="20240908")
    a = ap.parse_args()
    for mk in (["kosdaq", "kospi"] if a.market == "both" else [a.market]):
        run_market(mk, a.oos, a.seeds)


if __name__ == "__main__":
    main()
