# -*- coding: utf-8 -*-
"""
strength_cut_monitor.py — 강도 기준 '모니터 전용' 집계 — **v2 기준(지금 설정)** (2026-09-18 신설, 2026-10-30 v2 로 재작성 §82)

무엇을 하나
  매매는 **지금 설정 그대로** 두고, "강도 기준을 조금 낮췄다면/높였다면/아예 안 걸었다면 무엇을 사서 얼마였을까"를
  매일 기록한다. 실거래에는 일절 개입하지 않는다 — db/signal_strength_log.csv(신호마다 쌓이는 강도 기록)와 일봉만 읽는 집계.

왜 다시 만들었나(§82)
  종전 판은 v1 점수(score_ic)·단일 5.7·옛 6전략(rsi_reversal 등)의 상위 40/50% 컷으로 계산해서, 강도 v2·전략별 기준으로
  매매하는 지금 계좌를 전혀 재현하지 못했다. 이제 계좌마다 설정의 강도 방식(v2 면 score_v2, 모델 없는 전략만 v1 대체 —
  트레이더와 같은 규칙)·전략별 기준·면제·칸(전략별/칸 없음)·손절·익절을 그대로 쓴다.

규칙(전략별 기준에 똑같이 더함, 면제 전략은 언제나 통과)
  현행(설정) / 기준 −0.5 / 기준 +0.5 / 강도 필터 없음(강도 높은 순으로만)
  각 규칙을 두 가지로: ① 계좌 재현(칸·중복 금지 — 실제로 샀을 것) ② [신호 전체](칸 무시 — 기준을 넘은 신호 전부의 결과).
  칸이 꽉 차 있으면 ①은 규칙이 달라도 같은 종목을 사서 차이가 안 난다 → 기준 자체의 효과는 ②로 본다.
기간: v2 점수가 기록되기 시작한 날(2026-10-22)부터 — 표본이 쌓일수록 의미가 생긴다(몇 주~몇 달).
산출: db/strength_cut_monitor.csv  (규칙 x 매매 1행. 재실행 멱등 — 통째로 다시 만든다)
사용: python strength_cut_monitor.py          # 매일 20:00 run_recheck 에서
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

# ── 규칙 정의 — 지금 설정(live_config) 기준 ────────────────────────────────────────
import live_config as _LC
_KW, _KI = _LC.account("kiwoom"), _LC.account("kis")
_ACC = {"kiwoom": _KW, "kis": _KI}
CUR_TH = {"kiwoom": _KW["min_strength"], "kis": _KI["min_strength"]}
CUR_TH_BY = {"kiwoom": dict(_KW["min_strength_by"]), "kis": dict(_KI["min_strength_by"])}   # [§71] 전략별 기준
VERSION = {"kiwoom": _KW.get("strength_version", "v2"), "kis": _KI.get("strength_version", "v2")}
POOL = {a: A.get("slot_mode") == "pool" for a, A in _ACC.items()}
# 규칙: 전략별 기준에 더할 값(None = 강도 필터 없음)
RULES = {"현행(설정)": 0.0, "기준 −0.5": -0.5, "기준 +0.5": +0.5, "강도 필터 없음": None}
V2_SINCE = "20261022"       # score_v2 기록 시작일

CAPS = {"kiwoom": dict(_KW["slots"]), "kis": dict(_KI["slots"])}
PRIORITY = {"kiwoom": list(_KW["priority"]), "kis": list(_KI["priority"])}
HOLD = {**_KW["holding"], **_KI["holding"]}
STOP = {**_KW["stop"], **_KI["stop"]}
TAKE = {**_KW["take"], **_KI["take"]}
EXEMPT = _KW["exempt"] | _KI["exempt"]          # = kis ∪ kiwoom STRENGTH_EXEMPT
EXEMPT_BY = {"kiwoom": set(_KW["exempt"]), "kis": set(_KI["exempt"])}
SKIP_LU = {"kiwoom": set(_KW.get("skip_limitup") or ()), "kis": set(_KI.get("skip_limitup") or ())}   # [§91]
MAX_POS = 10


# [2026-10-23] 비용 = 백테스트 엔진과 같은 함수(매도 연도의 실제 거래세 + 수수료·슬리피지 왕복, §64). 종전 0.245 고정.
def COST_AT(exit_date):
    from strategy_engine import DEFAULT_COSTS
    from strategies._swing_base import trade_cost_pct
    return trade_cost_pct(DEFAULT_COSTS, exit_date)


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
            return ep, (min(sp, op[j]) / ep - 1) * 100 - COST_AT(dates[j]), "손절", dates[j], True
        if tp is not None and hi[j] >= tp:
            return ep, (max(tp, op[j]) / ep - 1) * 100 - COST_AT(dates[j]), "익절", dates[j], True
    if last < len(dates):                       # 만기 도달
        return ep, (cl[last] / ep - 1) * 100 - COST_AT(dates[last]), "만기", dates[last], True
    # 아직 보유 중 — 마지막 종가로 평가
    return ep, (cl[-1] / ep - 1) * 100 - COST_AT(dates[-1]), "보유중", dates[-1], False


def _skip_lu(acct, strat, r):
    """[§91] 그 계좌가 이 전략에 '상한가 마감 신호 제외'를 켰고 신호일 상한가 마감이면 True(라이브 cmd_buy 와 같음)."""
    return strat in SKIP_LU.get(acct, ()) and bool(getattr(r, "limitup", False))


def _passes(acct, strat, sc, offset):
    """규칙 통과 여부 — 면제는 항상, 필터 없음(None)은 항상, 그 외 전략별 기준+offset."""
    if strat in EXEMPT_BY[acct] or offset is None:
        return True
    th = CUR_TH_BY[acct].get(strat, CUR_TH[acct]) + offset
    return sc == sc and sc >= th


def simulate(sig, panel, tdates, offset, rule):
    """규칙 하나로 라이브 매수 규칙을 재현 — 계좌별 원장, 전략별 칸(또는 칸 없음 전체 10), 같은 종목 중복 금지, 강도 높은 순."""
    rows = []
    books = {acct: {} for acct in CAPS}     # acct -> {code: (exit_date|None, strategy)}
    for sd in sorted(sig["signal_date"].unique()):
        k = np.searchsorted(tdates, sd)     # 진입은 신호 다음 거래일 시가 (라이브 09:00 매수와 동일)
        if k + 1 >= len(tdates):
            continue
        ed = tdates[k + 1]
        day = sig[sig["signal_date"] == sd]
        for acct, caps in CAPS.items():
            held = books[acct]
            for code, v in list(held.items()):  # 청산일이 지난 보유는 칸 반납
                if v[0] and v[0] < ed:
                    held.pop(code, None)
            d2 = day[(day["acct"] == acct) & day["strategy"].isin(PRIORITY[acct])]
            if not len(d2):
                continue
            if POOL[acct]:                       # 칸 없음: 전략 구분 없이 강도 높은 순, 전체 10
                order = [d2.sort_values("sc", ascending=False, na_position="last")]
            else:                                # 전략별 칸: 설정 순서대로, 전략 안에서 강도 높은 순
                order = [d2[d2["strategy"] == st].sort_values("sc", ascending=False, na_position="last")
                         for st in PRIORITY[acct]]
            used = {s: sum(1 for v in held.values() if v[1] == s) for s in caps}
            for cand in order:
                for r in cand.itertuples():
                    strat = r.strategy
                    if _skip_lu(acct, strat, r) or not _passes(acct, strat, r.sc, offset):
                        continue
                    if r.code in held or len(held) >= MAX_POS:
                        continue
                    if not POOL[acct] and used.get(strat, 0) >= caps.get(strat, 0):
                        break
                    e = exit_of(panel, r.code, ed, strat)
                    if e is None:
                        continue
                    ep, net, why, xd, done = e
                    held[r.code] = (xd if done else None, strat)
                    used[strat] = used.get(strat, 0) + 1
                    rows.append({"rule": rule, "signal_date": sd, "entry_date": ed,
                                 "acct": acct, "strategy": strat, "code": r.code,
                                 "name": r.name_, "score": round(float(r.sc), 2) if r.sc == r.sc else None,
                                 "entry_price": round(ep), "exit_date": xd,
                                 "net_pct": round(net, 2), "why": why,
                                 "done": int(done)})
    return rows


def simulate_signals(sig, panel, tdates, offset, rule):
    """칸 무시 — 규칙을 통과한 **모든** 신호를 다음날 시가에 샀다고 보고 결과(같은 계좌 같은 종목·같은 날은 1번).
    칸이 꽉 차면 위 simulate 는 규칙이 달라도 같은 종목만 사서 차이가 안 보인다 → '기준이 좋은 신호를 골라내는가'를 따로 본다."""
    rows = []
    for r in sig.itertuples():
        if (r.strategy not in PRIORITY.get(r.acct, []) or _skip_lu(r.acct, r.strategy, r)
                or not _passes(r.acct, r.strategy, r.sc, offset)):
            continue
        k = np.searchsorted(tdates, r.signal_date)
        if k + 1 >= len(tdates):
            continue
        ed = tdates[k + 1]
        e = exit_of(panel, r.code, ed, r.strategy)
        if e is None:
            continue
        ep, net, why, xd, done = e
        rows.append({"rule": rule, "signal_date": r.signal_date, "entry_date": ed, "acct": r.acct,
                     "strategy": r.strategy, "code": r.code, "name": r.name_,
                     "score": round(float(r.sc), 2) if r.sc == r.sc else None, "entry_price": round(ep),
                     "exit_date": xd, "net_pct": round(net, 2), "why": why, "done": int(done)})
    return rows


def load_scores(path=LOG, since=V2_SINCE):
    """강도 기록 → 계좌별 설정 방식의 점수(sc). v2 면 score_v2, 비면 score_ic(트레이더와 같은 대체 규칙)."""
    L = pd.read_csv(path, dtype={"signal_date": str, "code": str})
    L["acct"] = L["account"].map(lambda x: "kiwoom" if str(x).startswith("kiwoom") else
                                 ("kis" if str(x).lower().startswith("kis") else "other"))
    L = L[L["acct"].isin(["kiwoom", "kis"])].copy()
    v1 = pd.to_numeric(L["score_ic"], errors="coerce")
    v2 = pd.to_numeric(L["score_v2"], errors="coerce") if "score_v2" in L.columns else pd.Series(np.nan, index=L.index)
    use_v2 = L["acct"].map(lambda a: VERSION[a] == "v2")
    L["sc"] = np.where(use_v2 & v2.notna(), v2, v1)
    L["signal_date"] = L["signal_date"].astype(str).str.replace("-", "", regex=False)
    L = L[L["signal_date"] >= since].copy()
    L["code"] = L["code"].astype(str).str.zfill(6)
    L = L.drop_duplicates(["signal_date", "acct", "strategy", "code"], keep="last")
    try:                                          # [§91] 신호일 상한가 마감 여부(설정에서 켠 전략은 라이브처럼 뺀다)
        import signal_filters as _SF
        _lu = {d: (_SF.limitup_codes(d) or set()) for d in L["signal_date"].unique()}
        L["limitup"] = [c in _lu.get(d, set()) for c, d in zip(L["code"], L["signal_date"])]
    except Exception:
        L["limitup"] = False
    return L.rename(columns={"name": "name_"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=V2_SINCE, help="집계 시작 신호일(기본: v2 점수 기록 시작일)")
    a = ap.parse_args()

    if not os.path.exists(LOG):
        sys.exit(f"강도 기록이 없습니다: {LOG}")
    L = load_scores(LOG, a.since)
    if not len(L):
        sys.exit("집계할 신호가 없습니다.")
    print(f"[모니터] 신호 {len(L):,}건 | {L.signal_date.min()}~{L.signal_date.max()} | 강도 방식 {VERSION}")

    panel, tdates = load_panel(a.since)
    print(f"[모니터] 일봉 {len(panel):,}종목 / 거래일 {len(tdates)}일")
    if not tdates:
        sys.exit("일봉 데이터가 없습니다 — macro_data/daily 확인")

    allrows = []
    for rule, off in RULES.items():
        for lab, fn in ((rule, simulate), (f"[신호 전체] {rule}", simulate_signals)):
            rows = fn(L, panel, tdates, off, lab)
            allrows += rows
            done = [r for r in rows if r["done"]]
            m = np.mean([r["net_pct"] for r in done]) if done else 0.0
            ev = np.mean([r["net_pct"] for r in rows]) if rows else 0.0
            print(f"  {lab:>22s}: 매수 {len(rows):4d}건 (완료 {len(done):3d}) 완료평균 {m:+.2f}% / 평가포함 {ev:+.2f}%")

    df = pd.DataFrame(allrows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n저장 → {OUT} ({len(df):,}행)")
    print("  ※ 모니터 전용 — 실거래에는 일절 개입하지 않는다(집계만).")


if __name__ == "__main__":
    main()
