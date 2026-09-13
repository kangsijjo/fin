# -*- coding: utf-8 -*-
"""
xover_exit_matrix.py — 수급 크로스 청산 규칙 **전수 그리드**(익절 3~15% × 손절 −5~−20%, 0.5% 단위) (2026-09-13)

사용자 요청: "익절 3~15% 를 0.5% 단위로, 각 익절마다 손절 −5~−20% 를 0.5% 단위로 전부."
  → 25(익절) × 31(손절) = **775 조합**. 기존 `_make_trades_with_stops` 는 신호마다 파이썬 루프라
    조합당 수십 초 = 전체 수 시간. 여기서는 동일 규칙을 **행렬 연산으로 재현**해 조합당 수십 밀리초로 만든다.

재현하는 규칙 (`_swing_base._make_trades_with_stops` 와 1:1)
  · 진입: 신호 다음날 시가(entry_p = open[t+1]), 슬리피지 별도 미적용.
  · **진입 당일은 익절만** 검사(고가 ≥ 목표면 목표가 체결). 손절은 다음날부터.
  · 다음날~만기: 손절(저가 ≤ 손절가) 우선, 그다음 익절(고가 ≥ 목표가). **같은 날 둘 다면 손절**(보수적).
  · 갭 보정: 손절 체결가 = min(손절가, 당일시가) / 익절 체결가 = max(목표가, 당일시가).
  · 만기: 보유 20일째 종가. net% = (청산가/진입가 − 1)×100 − 비용(0.245%).
  · 기업행위(액면분할 등) 포함 매매 제외 — 원 엔진과 동일한 `find_corporate_action_dates` 사용.

검증: `--verify` 로 몇 개 조합을 원 엔진과 대조(매매수·평균·승률 일치 확인). 기본으로 항상 수행.

무작위 대조: 같은 날·같은 개수의 무작위 진입에 **동일 청산**을 적용해 알파를 낸다. 승률이 무작위와 같으면
그 승률은 신호가 아니라 청산 규칙이 만든 것이다(2026-09-09 실측: +2% 익절에서 크로스 86.4% vs 무작위 86.2%).

사용: .venv/Scripts/python.exe xover_exit_matrix.py [--market kosdaq] [--seeds 1]
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
from strategies._swing_base import find_corporate_action_dates, _trade_has_corporate_action, _make_trades_with_stops
from strategy_engine import DEFAULT_COSTS
from xover_variants import XoverVariant, random_like

HOLD = 20
OOS = "20240908"
COST = DEFAULT_COSTS["total_pct"]
DATA_DIRS = {"kosdaq": None, "kospi": "macro_data/daily_kospi"}
def _seq(lo, hi, step=0.5, sign=1):
    n = int(round((hi - lo) / step)) + 1
    return [round(sign * (lo + step * i), 1) for i in range(n)]


TPS = _seq(3.0, 15.0)                 # 기본 익절 3.0 ~ 15.0 (--tp-from/--tp-to 로 변경)
SLS = _seq(5.0, 20.0, sign=-1)        # 기본 손절 -5.0 ~ -20.0 (--sl-from/--sl-to 는 절댓값으로 준다)


class ExitMatrix:
    """신호 집합을 받아 (진입일 기준 20일 경로) 행렬을 한 번만 만들고, 청산 조합을 벡터로 평가."""

    def __init__(self, df, sig_mask):
        d = df.sort_values(["code", "date"]).reset_index(drop=True)
        self.d = d
        code = d["code"].to_numpy()
        first = np.r_[True, code[1:] != code[:-1]]
        grp = np.cumsum(first) - 1
        last_idx = np.zeros(len(d), dtype=np.int64)
        ends = np.r_[np.flatnonzero(first)[1:] - 1, len(d) - 1]
        last_idx = ends[grp]                                   # 각 행이 속한 종목의 마지막 인덱스

        self.O = d["open"].to_numpy(dtype=float)
        self.H = d["high"].to_numpy(dtype=float)
        self.L = d["low"].to_numpy(dtype=float)
        self.C = d["close"].to_numpy(dtype=float)
        self.date = d["date"].astype(str).to_numpy()
        self.code = code

        sig_idx = np.flatnonzero(sig_mask.to_numpy() if hasattr(sig_mask, "to_numpy") else sig_mask)
        entry = sig_idx + 1
        ok = (entry <= last_idx[sig_idx]) & (self.O[np.minimum(entry, len(d) - 1)] > 0)
        entry = entry[ok]
        self.entry = entry
        self.last = last_idx[entry]
        self.entry_p = self.O[entry]

        rows = entry[:, None] + np.arange(HOLD)[None, :]
        self.valid = rows <= self.last[:, None]
        rows = np.minimum(rows, self.last[:, None])
        self.rows = rows
        self.Hm, self.Lm, self.Om = self.H[rows], self.L[rows], self.O[rows]
        self.exit_row = np.minimum(entry + HOLD - 1, self.last)   # 만기 행
        self.exp_close = self.C[self.exit_row]
        self.entry_date = self.date[entry]
        self.exit_date_exp = self.date[self.exit_row]

    def set_ca_filter(self, ca_map):
        """기업행위 포함 매매 마스크(원 엔진과 동일 판정). 청산일이 조합마다 달라 최장(만기) 기준으로 잡으면
        과도 제외가 되므로, 조합별 청산일로 판정할 수 있게 코드·날짜 배열만 보관한다."""
        self.ca_map = ca_map

    def evaluate(self, tp, sl):
        """(tp, sl) 조합의 net% 배열과 청산 사유 배열을 반환."""
        n = len(self.entry)
        ep = self.entry_p
        tp_price = ep * (1 + tp / 100.0) if tp is not None else None
        sl_price = ep * (1 + sl / 100.0) if sl is not None else None
        BIG = HOLD + 10

        # 익절 도달일 (진입 당일 d=0 포함)
        if tp_price is not None:
            hit_tp = self.valid & (self.Hm > 0) & (self.Hm >= tp_price[:, None])
            d_tp = np.where(hit_tp.any(1), hit_tp.argmax(1), BIG)
        else:
            d_tp = np.full(n, BIG)
        # 손절 도달일 (d>=1 부터 — 진입 당일은 원 엔진도 손절을 보지 않는다)
        if sl_price is not None:
            hit_sl = self.valid & (self.Lm > 0) & (self.Lm <= sl_price[:, None])
            hit_sl[:, 0] = False
            d_sl = np.where(hit_sl.any(1), hit_sl.argmax(1), BIG)
        else:
            d_sl = np.full(n, BIG)

        use_sl = d_sl <= d_tp                      # 같은 날이면 손절 우선
        d_exit = np.minimum(d_tp, d_sl)
        hit_any = d_exit < BIG

        exit_p = self.exp_close.astype(float).copy()
        reason = np.full(n, 2, dtype=np.int8)      # 0=tp, 1=sl, 2=hold
        idx = np.arange(n)

        m_sl = hit_any & use_sl
        if m_sl.any():
            dd = d_exit[m_sl]
            op = self.Om[idx[m_sl], dd]
            px = np.where(op > 0, np.minimum(sl_price[m_sl], op), sl_price[m_sl])
            exit_p[m_sl] = px
            reason[m_sl] = 1
        m_tp = hit_any & ~use_sl
        if m_tp.any():
            dd = d_exit[m_tp]
            op = self.Om[idx[m_tp], dd]
            # 진입 당일(d=0)은 시가=진입가라 갭 보정 없음 → 목표가 체결
            px = np.where((dd > 0) & (op > 0), np.maximum(tp_price[m_tp], op), tp_price[m_tp])
            exit_p[m_tp] = px
            reason[m_tp] = 0

        exit_row = np.where(hit_any, self.rows[idx, np.minimum(d_exit, HOLD - 1)], self.exit_row)
        net = (exit_p / self.entry_p - 1.0) * 100.0 - COST
        good = exit_p > 0
        return net, reason, exit_row, good


def ca_mask(em):
    """조합 무관하게 '진입일~만기' 구간에 기업행위가 있는 매매를 제외(원 엔진보다 약간 보수적 —
    조기청산 매매도 제외될 수 있으나 전체의 1% 미만이라 비교에 영향 없음)."""
    keep = np.ones(len(em.entry), dtype=bool)
    for i in range(len(em.entry)):
        if _trade_has_corporate_action(em.ca_map, em.code[em.entry[i]], em.entry_date[i], em.exit_date_exp[i]):
            keep[i] = False
    return keep


def stats(net, reason, keep, entry_date):
    v = net[keep]
    r = reason[keep]
    if len(v) == 0:
        return None
    oos = v[entry_date[keep] >= OOS]
    out = dict(n=len(v), win=float((v > 0).mean() * 100), avg=float(v.mean()), med=float(np.median(v)),
               oos=float(oos.mean()) if len(oos) else np.nan)
    for k, tag in ((0, "tp"), (1, "sl"), (2, "ho")):
        m = r == k
        out[tag + "_pct"] = float(m.mean() * 100)
        out[tag + "_avg"] = float(v[m].mean()) if m.any() else np.nan
    return out


def verify(df, sig, em, keep, combos):
    print(f"\n [검증] 벡터 엔진 vs 원 엔진(_make_trades_with_stops) — 매매수·승률·평균 대조")
    print(f"   {'조합':16s}{'원엔진 n':>10s}{'벡터 n':>9s}{'원 승률':>9s}{'벡터 승률':>10s}{'원 평균':>9s}{'벡터 평균':>10s}{'차이':>8s}")
    for tp, sl in combos:
        tr = _make_trades_with_stops(sig, holding_days=HOLD, strategy_name="v", costs=DEFAULT_COSTS,
                                     take_profit_pct=tp, tp_fill="high", stop_loss_pct=sl)
        x = pd.DataFrame([t.__dict__ for t in tr])
        net, reason, _, good = em.evaluate(tp, sl)
        k = keep & good
        s = stats(net, reason, k, em.entry_date)
        lab = f"익절{tp}/손절{sl}"
        print(f"   {lab:16s}{len(x):10,}{s['n']:9,}{(x.net_pct > 0).mean() * 100:8.1f}%{s['win']:9.1f}%"
              f"{x.net_pct.mean():+9.3f}{s['avg']:+10.3f}{s['avg'] - x.net_pct.mean():+8.3f}")


def heat(title, grid, fmt="{:5.1f}", every=2):
    print(f"\n{title}")
    cols = SLS[::every]
    print("  익절\\손절 " + "".join(f"{c:>6.1f}" for c in cols))
    for tp in TPS[::every]:
        print(f"  {tp:7.1f} " + "".join(fmt.format(grid[(tp, sl)]) + " " for sl in cols))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="kosdaq", choices=["kosdaq", "kospi", "both"])
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--tp-from", type=float, default=3.0)
    ap.add_argument("--tp-to", type=float, default=15.0)
    ap.add_argument("--sl-from", type=float, default=5.0, help="손절 하한(절댓값). 2.0 이면 -2.0%%")
    ap.add_argument("--sl-to", type=float, default=20.0, help="손절 상한(절댓값)")
    ap.add_argument("--step", type=float, default=0.5)
    a = ap.parse_args()
    global TPS, SLS
    TPS = _seq(a.tp_from, a.tp_to, a.step)
    SLS = _seq(a.sl_from, a.sl_to, a.step, sign=-1)
    for mk in (["kosdaq", "kospi"] if a.market == "both" else [a.market]):
        print(f"\n{'=' * 110}\n {mk.upper()} — 익절 {TPS[0]}~{TPS[-1]}% × 손절 {SLS[0]}~{SLS[-1]}% "
              f"({len(TPS)}×{len(SLS)} = {len(TPS) * len(SLS):,} 조합)\n{'=' * 110}")
        df = load_macro_daily(start_date="20210101", data_dir=DATA_DIRS[mk]).reset_index(drop=True)
        sig = XoverVariant("base").signal_df(df)
        ca = find_corporate_action_dates(df)

        em = ExitMatrix(sig, sig["signal"])
        em.set_ca_filter(ca)
        keep0 = ca_mask(em)
        print(f" 크로스 신호 {int(sig['signal'].sum()):,} → 유효 매매 {int(keep0.sum()):,} (기업행위 제외 {int((~keep0).sum()):,})")

        verify(df, sig, em, keep0, [(5.0, -8.0), (2.0, None), (3.0, -5.0)])

        rnd_em = []
        for k in range(max(1, a.seeds)):
            rs = random_like(sig, 7000 + k)
            e = ExitMatrix(rs, rs["signal"])
            e.set_ca_filter(ca)
            rnd_em.append((e, ca_mask(e)))

        W, A, ALPHA, SLP = {}, {}, {}, {}
        best = []
        for tp in TPS:
            for sl in SLS:
                net, reason, _, good = em.evaluate(tp, sl)
                s = stats(net, reason, keep0 & good, em.entry_date)
                rw, ra = [], []
                for e, kk in rnd_em:
                    n2, r2, _, g2 = e.evaluate(tp, sl)
                    s2 = stats(n2, r2, kk & g2, e.entry_date)
                    if s2:
                        rw.append(s2["win"]); ra.append(s2["avg"])
                W[(tp, sl)] = s["win"]
                A[(tp, sl)] = s["avg"]
                ALPHA[(tp, sl)] = s["avg"] - float(np.mean(ra)) if ra else np.nan
                SLP[(tp, sl)] = s["sl_pct"]
                best.append((s["avg"], tp, sl, s["win"], ALPHA[(tp, sl)], s["oos"], s["n"],
                             s["tp_pct"], s["tp_avg"], s["sl_pct"], s["sl_avg"], s["ho_pct"]))

        heat(f" [승률 %] 행=익절, 열=손절 (1.0% 간격 표시)", W)
        heat(f" [평균 net %] 행=익절, 열=손절", A, fmt="{:5.2f}")

        best.sort(reverse=True)
        print(f"\n [평균 net 상위 10 조합]  (손절평균 = 실제 체결 손실 — 갭 때문에 손절선보다 나쁠 수 있다)")
        print(f"   {'익절':>6s}{'손절':>7s}{'승률':>8s}{'평균net':>9s}{'OOS':>8s}{'무작위대비':>10s}"
              f"{'익절%':>7s}{'익절평균':>9s}{'손절%':>7s}{'손절평균':>9s}{'만기%':>7s}")
        for avg, tp, sl, w, al, oo, n, tpp, tpa, slp, sla, hop in best[:10]:
            print(f"   {tp:6.1f}{sl:7.1f}{w:7.1f}%{avg:+9.3f}{oo:+8.3f}{al:+10.3f}"
                  f"{tpp:7.1f}{tpa:+9.2f}{slp:7.1f}{sla:+9.2f}{hop:7.1f}")

        pos = sum(1 for v in A.values() if v > 0)
        pos_a = sum(1 for v in ALPHA.values() if v > 0.3)
        ncomb = len(TPS) * len(SLS)
        print(f"\n [요약] {ncomb:,}조합 중 평균 net > 0: **{pos}개** | 무작위 대비 알파 ≥ +0.3%p: **{pos_a}개**")
        print(f"   최고 평균 net {best[0][0]:+.3f}% (익절 {best[0][1]}/손절 {best[0][2]}) | "
              f"최저 {best[-1][0]:+.3f}% (익절 {best[-1][1]}/손절 {best[-1][2]})")
        print(f"   무작위 대비 알파: 최대 {max(ALPHA.values()):+.3f}%p / 최소 {min(ALPHA.values()):+.3f}%p "
              f"/ 평균 {np.mean(list(ALPHA.values())):+.3f}%p")
        for u in [(5.0, -8.0), (5.0, -3.0), (5.0, -2.0)]:
            if u in W:
                print(f"   익절+{u[0]:g}%/손절{u[1]:g}%: 승률 {W[u]:.1f}% | 평균 {A[u]:+.3f}% | 무작위 대비 {ALPHA[u]:+.3f}%p")
        # 손절을 조일수록 '신호 영향'이 사라지는지 — 손절선별 알파 절댓값 평균
        print(f"\n [손절선별] 손절이 타이트할수록 무작위와 구별이 사라지는가")
        print(f"   {'손절':>7s}{'평균net(익절 전구간)':>22s}{'|알파| 평균':>14s}{'손절체결 비중':>14s}")
        for sl in SLS:
            vals = [A[(tp, sl)] for tp in TPS]
            als = [abs(ALPHA[(tp, sl)]) for tp in TPS]
            slps = [SLP[(tp, sl)] for tp in TPS]
            print(f"   {sl:7.1f}{np.mean(vals):22.3f}{np.mean(als):14.3f}{np.mean(slps):13.1f}%")


if __name__ == "__main__":
    main()
