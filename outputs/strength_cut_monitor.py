# -*- coding: utf-8 -*-
"""
strength_cut_monitor.py — 전략별 임계 '모니터 전용' 집계 (Phase 1) (2026-09-18)

무엇을 하나
  현행 규칙(강도 5.7 단일)으로 매매는 **그대로** 하면서, "전략별 임계였다면 무엇을
  샀을 것이고 얼마를 벌었을 것인가"만 **기록**한다. 실거래에는 일절 개입하지 않는다.
  → 매매 코드(kiwoom_trader/kis_trader)를 건드리지 않는다. 이 스크립트는
     db/signal_strength_log.csv(이미 쌓이는 기록)와 일봉만 읽는 **순수 집계**다.

왜 (§39)
  단일 절대 임계 5.7 은 전략마다 전혀 다른 의미다 — 학습분포 상위 30% 컷이
  high_52w_filt 3.16 ~ rsi_reversal 6.83 으로 3.7점 벌어져 있다. 그래서 5.7 하나는
  모멘텀 4전략을 통과율 0% 로 끄면서 rsi_reversal 은 오히려 느슨하게 통과시킨다.
  백테스트에선 전략별 임계가 무작위 대비 일관되게 우위였으나(현행 대비 p=0.006),
  **특정 상위 X% 를 고를 근거는 없고** 시스템의 성격 자체가 바뀌는 변경이라
  후보수 게이트·손절 때와 동일하게 **모니터 전용 2~4주 → 비교 후 결정** 절차를 밟는다.

세 규칙을 동시에 추적한다
  A 현행     : score_ic >= 5.7 (전 전략 공통)
  B 상위40%  : 전략별 고정 임계(학습분포 상위 40% 지점)
  C 상위50%  : 전략별 고정 임계(상위 50% 지점)

산출: db/strength_cut_monitor.csv  (규칙 x 매매 1행. 재실행 멱등 — 통째로 다시 만든다)
사용: python strength_cut_monitor.py          # 매일 장 마감 후 1회
"""
import os
import sys
import glob
import argparse
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

LOG = "./db/signal_strength_log.csv"
OUT = "./db/strength_cut_monitor.csv"
DAILY = "./macro_data/daily"

# ── 규칙 정의 ────────────────────────────────────────────────────────────────
# 전략별 임계는 trades_history_v3 의 **2026년 이전** 매매로만 산출했다(워크포워드).
# 재산출: python strength_per_strategy_cut.py  (fixed_cuts(40) / fixed_cuts(50))
CUR_TH = 5.7
CUTS_40 = {"high_52w_filt": 3.14, "rsi_reversal": 6.65, "rsi_vol": 5.99,
           "h52w_for3d_mkt": 3.53, "for_high20_mkt": 3.75, "gc_for3d": 4.47}
CUTS_50 = {"high_52w_filt": 2.94, "rsi_reversal": 6.40, "rsi_vol": 5.69,
           "h52w_for3d_mkt": 3.34, "for_high20_mkt": 3.57, "gc_for3d": 4.25}
RULES = {"현행5.7": None, "상위40%": CUTS_40, "상위50%": CUTS_50}

# 라이브 구성 (kiwoom_trader / kis_trader 상수와 일치해야 한다)
CAPS = {"kiwoom": {"high_52w_filt": 4, "rsi_reversal": 4, "rsi_vol": 2},
        "kis":    {"h52w_for3d_mkt": 4, "for_high20_mkt": 4, "gc_for3d": 2}}
PRIORITY = {"kiwoom": ["high_52w_filt", "rsi_reversal", "rsi_vol"],
            "kis":    ["h52w_for3d_mkt", "for_high20_mkt", "gc_for3d"]}
HOLD = {"high_52w_filt": 20, "rsi_reversal": 5, "rsi_vol": 7,
        "h52w_for3d_mkt": 20, "for_high20_mkt": 20, "gc_for3d": 15}
STOP = {"h52w_for3d_mkt": -0.15, "for_high20_mkt": -0.10, "gc_for3d": -0.26}
TAKE = {"high_52w_filt": 0.50, "rsi_vol": 0.20}
COST = 0.245            # 수수료+세금+슬리피지 (DEFAULT_COSTS.total_pct)


def load_panel(since):
    """일봉 CSV → {code: (dates, open, high, low, close)}. since 이후만."""
    files = sorted(glob.glob(os.path.join(DAILY, "*.csv")))
    files = [f for f in files if os.path.basename(f)[:8] >= since]
    parts = []
    for f in files:
        try:
            d = pd.read_csv(f, dtype={"code": str})
        except Exception:
            continue
        need = {"code", "open", "high", "low", "close"}
        if not need <= set(d.columns):
            continue
        d = d[list(need)].copy()
        d["date"] = os.path.basename(f)[:8]
        parts.append(d)
    if not parts:
        return {}, []
    df = pd.concat(parts, ignore_index=True)
    df["code"] = df["code"].astype(str).str.zfill(6)
    panel = {}
    for code, g in df.sort_values(["code", "date"]).groupby("code"):
        panel[code] = (g["date"].to_numpy(), g["open"].to_numpy(float),
                       g["high"].to_numpy(float), g["low"].to_numpy(float),
                       g["close"].to_numpy(float))
    return panel, sorted(df["date"].unique())


def exit_of(panel, code, entry_date, strat):
    """진입일 시가 매수 → 손절/익절/만기. 미완료면 (None, 사유, None, 평가손익)."""
    p = panel.get(code)
    if p is None:
        return None
    dates, op, hi, lo, cl = p
    i = np.searchsorted(dates, entry_date)
    if i >= len(dates) or dates[i] != entry_date or not op[i] or op[i] <= 0:
        return None
    ep = float(op[i])
    hold = HOLD[strat]
    sp = ep * (1 + STOP[strat]) if strat in STOP else None
    tp = ep * (1 + TAKE[strat]) if strat in TAKE else None
    last = i + hold - 1
    for j in range(i, min(last, len(dates) - 1) + 1):
        if sp is not None and lo[j] <= sp:
            return ep, (min(sp, op[j]) / ep - 1) * 100 - COST, "손절", dates[j], True
        if tp is not None and hi[j] >= tp:
            return ep, (max(tp, op[j]) / ep - 1) * 100 - COST, "익절", dates[j], True
    if last < len(dates):                       # 만기 도달
        return ep, (cl[last] / ep - 1) * 100 - COST, "만기", dates[last], True
    # 아직 보유 중 — 마지막 종가로 평가
    return ep, (cl[-1] / ep - 1) * 100 - COST, "보유중", dates[-1], False


def simulate(sig, panel, tdates, cuts, rule):
    """규칙 하나로 라이브 매수 규칙을 재현 — 슬롯 상한·중복종목 스킵·강도 내림차순."""
    rows = []
    # 보유 원장은 **계좌별로 따로** 둔다 — 라이브에서 키움 안C 와 KIS 안D 는 독립
    # 계좌라 같은 종목을 양쪽이 동시에 보유할 수 있고, 슬롯도 각자 10개다.
    books = {acct: {} for acct in CAPS}     # acct -> {code: (exit_date|None, strategy)}
    for sd in sorted(sig["signal_date"].unique()):
        # 진입은 신호 다음 거래일 시가 (라이브 09:00 매수와 동일)
        k = np.searchsorted(tdates, sd)
        if k + 1 >= len(tdates):
            continue
        ed = tdates[k + 1]
        day = sig[sig["signal_date"] == sd]
        for acct, caps in CAPS.items():
            held = books[acct]
            for code, v in list(held.items()):  # 청산일이 지난 보유는 슬롯 반납
                if v[0] and v[0] < ed:
                    held.pop(code, None)
            d2 = day[day["acct"] == acct]
            if not len(d2):
                continue
            # 보유 중인 전략별 슬롯 점유 수 (held: code -> (exit_date|None, strategy))
            used = {s: sum(1 for v in held.values() if v[1] == s) for s in caps}
            for strat in PRIORITY[acct]:
                cand = d2[d2["strategy"] == strat].sort_values("sc", ascending=False)
                for r in cand.itertuples():
                    th = CUR_TH if cuts is None else cuts.get(strat)
                    if th is None or r.sc < th:
                        continue
                    if r.code in held:
                        continue
                    if used[strat] >= caps[strat]:
                        break
                    e = exit_of(panel, r.code, ed, strat)
                    if e is None:
                        continue
                    ep, net, why, xd, done = e
                    held[r.code] = (xd if done else None, strat)
                    used[strat] += 1
                    rows.append({"rule": rule, "signal_date": sd, "entry_date": ed,
                                 "acct": acct, "strategy": strat, "code": r.code,
                                 "name": r.name_, "score": round(float(r.sc), 2),
                                 "entry_price": round(ep), "exit_date": xd,
                                 "net_pct": round(net, 2), "why": why,
                                 "done": int(done)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="20260623", help="집계 시작 신호일")
    a = ap.parse_args()

    if not os.path.exists(LOG):
        sys.exit(f"강도 기록이 없습니다: {LOG}")
    L = pd.read_csv(LOG, dtype={"signal_date": str, "code": str})
    L["sc"] = pd.to_numeric(L["score_ic"], errors="coerce")
    L = L[L["sc"].notna() & (L["signal_date"] >= a.since)].copy()
    L["code"] = L["code"].astype(str).str.zfill(6)
    L["acct"] = L["account"].map(lambda x: "kiwoom" if str(x).startswith("kiwoom") else "kis")
    L = L.rename(columns={"name": "name_"})
    if not len(L):
        sys.exit("집계할 신호가 없습니다.")
    print(f"[모니터] 신호 {len(L):,}건 | {L.signal_date.min()}~{L.signal_date.max()}")

    panel, tdates = load_panel(a.since)
    print(f"[모니터] 일봉 {len(panel):,}종목 / 거래일 {len(tdates)}일")
    if not tdates:
        sys.exit("일봉 데이터가 없습니다 — macro_data/daily 확인")

    allrows = []
    for rule, cuts in RULES.items():
        rows = simulate(L, panel, tdates, cuts, rule)
        allrows += rows
        done = [r for r in rows if r["done"]]
        m = np.mean([r["net_pct"] for r in done]) if done else 0.0
        print(f"  {rule:>8s}: 매수 {len(rows):3d}건 (완료 {len(done):3d}) 평균 {m:+.2f}%")

    df = pd.DataFrame(allrows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n저장 → {OUT} ({len(df):,}행)")
    print("  ※ 모니터 전용 — 실거래에는 일절 개입하지 않는다(집계만).")


if __name__ == "__main__":
    main()
