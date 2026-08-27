"""
breaker_sweep.py — 포트폴리오 드로다운 서킷브레이커 + 일일 신규진입 상한 스윕 (Phase 0).

목적: 슬롯 MDD −55~60% 의 원인(동시보유 동반하락)에 대한 '포트폴리오 레벨' 개입 효과를
백테스트로 측정. stop_sweep/profit_sweep 컨벤션 — **표만 출력, '최적' 자동선택 없음**.

개입(신규 매수만 조절 — 매도·만기·익절 불가침):
  브레이커 X : 계좌 equity(전일 MTM)가 고점 대비 −X% 이하 → 신규매수 중단(HALT),
               −(X−5)% 로 회복해야 재개(히스테리시스). 라이브와 동일하게 '전일' equity 로
               판단(당일 종가 사용 안 함 — look-ahead 배제).
  일일상한 N : 하루 신규 진입 ≤ N 종목 (같은 날 코호트 동반피해 완화).

계좌 구성 = 라이브 그대로:
  kiwoom(10슬롯) = star_high_52w_20_filt + rsi_reversal + rsi_vol   (손절 없음)
  kis   (10슬롯) = h52w_for3d_mkt + for_high20_mkt + gc_for3d       (기본 만기청산 풀)
동일 종목 중복보유 스킵(라이브 cmd_buy 와 동일). MTM=일별 종가 평가(capital_simulator 방식).

사용: python breaker_sweep.py [--fwd-start YYYYMMDD]
출력: breaker_sweep_result.csv + 콘솔 표 (full / fwd 두 구간)
"""

import argparse
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import pandas as pd  # noqa: E402

from strategy_engine import ALL_STRATEGIES, DEFAULT_COSTS      # noqa: E402
from strategies.daily_loader import load_macro_daily            # noqa: E402
from capital_simulator import build_price_lookup                # noqa: E402

# 전략별 슬롯 상한 — 라이브(STRATEGY_MAX_SLOTS)와 동일. 공용 10슬롯으로 풀링하면
# 후보 수가 압도적인 rsi_reversal(5.4만 건)이 포트폴리오를 점령해 라이브와 전혀 다른
# 구성이 됨(첫 스윕에서 -98% 왜곡 확인) — 전략별 캡이 시뮬 신뢰성의 핵심.
ACCOUNTS = {
    "kiwoom": {"star_high_52w_20_filt": 4, "rsi_reversal": 4, "rsi_vol": 2},
    "kis":    {"h52w_for3d_mkt": 4, "for_high20_mkt": 4, "gc_for3d": 2},
}
PRIORITY = {   # 라이브 STRATEGY_PRIORITY — 슬롯 경쟁 시 우선순위
    "kiwoom": ["star_high_52w_20_filt", "rsi_reversal", "rsi_vol"],
    "kis":    ["h52w_for3d_mkt", "for_high20_mkt", "gc_for3d"],
}
_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
SLOTS = 10
CAPITAL0 = 10_000_000
BREAKERS = [None, 10.0, 15.0, 20.0, 25.0]   # X (%); 재개 = X-5
DAILY_CAPS = [None, 3]                       # N (하루 신규진입 상한)

ap = argparse.ArgumentParser()
ap.add_argument("--fwd-start", default=None, help="forward 시작일 YYYYMMDD (기본: 거래일 70% 지점)")
ap.add_argument("--min-strength", type=float, default=5.7,
                help="강도(score_ic) 필터. 0 이면 미적용(2026-07 구버전 동작 재현)")
args = ap.parse_args()



# ── 강도(score_ic) 조인 ──────────────────────────────────────────────────────
# [2026-08-27] 이 도구의 무차단 기준선이 -99.3% 로 나와 7월부터 해석 불가 상태였다.
#   원인은 코드 버그가 아니라 **낡은 기준선**이었다 — 강도 필터가 아예 없어서
#   '강도 도입 이전의 시스템'을 재현하고 있었다(라이브는 2026-07 부터 >=5.7 적용).
#   무필터 시스템은 실제로 파산한다: 슬롯 실현 평균 약 -0.93% x 약 3,100 라운드트립
#   -> (1+0.1*-0.0093)^3100 ~ 0.045 = -95%. 즉 -99.3% 는 산술적으로 맞는 값이었고,
#   파산하는 기준선 위에서는 어떤 차단기도 '개선'으로 보인다(과대평가).
# 강도는 trades_history_v3 기반 워크포워드 채점본에서 (진입일, 종목, 전략)으로 조인한다.
#   실측 매칭률: high_52w_filt 99.9% / rsi_reversal 91.3% / rsi_vol 90.7%.
#   무기록은 **매수하지 않는다(fail-closed)** — 라이브 verify_strength 와 동일 규약.
_ENGINE_TO_LIVE = {"star_high_52w_20_filt": "high_52w_filt"}
_SCORED_PKL = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "Temp", "claude",
    "C--fin", "_scored_walkforward.pkl")


def _load_scores():
    """{(entry_date, code, live_strategy): score_ic} 또는 None(파일 없음)."""
    import pickle
    for p in (_SCORED_PKL, os.path.join(_HERE_DIR, "ai_data", "scored_walkforward.pkl")):
        if p and os.path.exists(p):
            try:
                A = pickle.load(open(p, "rb"))
            except Exception:
                continue
            A = A.reset_index(drop=True)
            A["code"] = A["code"].astype(str).str.zfill(6)
            # itertuples 는 밑줄로 시작하는 컬럼명을 _1/_2 로 바꿔버린다 → zip 사용
            return dict(zip(zip(A["_sd"], A["code"], A["strategy"]), A["_s"]))
    return None


def apply_strength(tdf, scores, min_strength):
    """강도 필터 적용 + 커버리지 보고. 반환: (필터된 df, 리포트 문자열)."""
    if min_strength <= 0 or scores is None:
        why = "미적용(--min-strength 0)" if min_strength <= 0 else "채점 데이터 없음 — 미적용"
        return tdf, f"강도 필터 {why}  ※ 기준선이 '강도 도입 이전 시스템'이 됨"
    key = tdf.apply(lambda r: (r["entry_date"], r["code"],
                               _ENGINE_TO_LIVE.get(r["strategy"], r["strategy"])), axis=1)
    sc = key.map(scores)
    matched = sc.notna().sum()
    keep = tdf[sc >= min_strength]
    return keep, (f"강도 >= {min_strength} 적용 — 채점매칭 {matched:,}/{len(tdf):,}"
                  f" ({matched/max(1,len(tdf))*100:.1f}%), 통과 {len(keep):,}건"
                  f" (무기록은 fail-closed 로 제외)")


def simulate(trades_df, price_map, trading_dates, slot_caps, priority,
             breaker_x=None, daily_cap=None):
    """라이브 cmd_buy 규칙의 MTM 슬롯 시뮬 + 브레이커/일일상한.

    trades_df: entry_date/exit_date/code/strategy/entry_price/net_pct/score
    slot_caps: {strategy: max_slots} (합=SLOTS, 라이브 STRATEGY_MAX_SLOTS)
    priority : 전략 우선순위 리스트(라이브 STRATEGY_PRIORITY) — 전략별로 tv 내림차순 배정.
    브레이커 판단은 '전일 기록 equity'(라이브의 15:40 스냅샷→익일 09시 게이트와 동일 시점).
    """
    df = trades_df.sort_values("entry_date").reset_index(drop=True)
    if df.empty:
        return None
    first_entry, last_exit = df["entry_date"].min(), df["exit_date"].max()
    all_dates = [d for d in trading_dates if first_entry <= d <= last_exit]

    resume_y = (breaker_x - 5.0) if breaker_x is not None else None
    cash = float(CAPITAL0)
    positions = []
    eq_curve = []
    peak = float(CAPITAL0)
    prev_equity = float(CAPITAL0)
    halted = False
    halt_days = 0
    halt_episodes = 0
    skip_breaker = skip_cap = skip_slot = skip_dup = 0
    by_date = {d: g for d, g in df.groupby("entry_date")}

    for date in all_dates:
        # 1) 만기 청산
        remaining = []
        for p in positions:
            if p["exit_date"] <= date:
                cash += p["invested_value"] * (1 + p["net_pct"] / 100)
            else:
                remaining.append(p)
        positions = remaining

        # 2) 브레이커 상태 갱신 — '전일' equity 기준(당일 종가 미사용)
        if breaker_x is not None:
            dd = (prev_equity / peak - 1) * 100
            if not halted and dd <= -breaker_x:
                halted = True
                halt_episodes += 1
            elif halted and dd >= -resume_y:
                halted = False
        if halted:
            halt_days += 1

        # 3) 신규 진입 — 라이브 cmd_buy 규칙: 전략 우선순위 → 전략별 tv 내림차순,
        #    전략별 슬롯 상한(4/4/2) + 전체 상한(10) + 균등예산(현금/남은 전체 빈슬롯)
        todays = by_date.get(date)
        entered_today = 0
        if todays is not None:
            held = {p["code"] for p in positions}
            used_by = {}
            for p in positions:
                used_by[p["strategy"]] = used_by.get(p["strategy"], 0) + 1
            for strat in priority:
                cand = todays[todays["strategy"] == strat]
                if cand.empty:
                    continue
                for _, row in cand.sort_values("score", ascending=False).iterrows():
                    if row["code"] in held:
                        skip_dup += 1
                        continue
                    if halted:
                        skip_breaker += 1
                        continue
                    if daily_cap is not None and entered_today >= daily_cap:
                        skip_cap += 1
                        continue
                    if used_by.get(strat, 0) >= slot_caps.get(strat, 0):
                        skip_slot += 1          # 전략별 슬롯 상한 도달
                        continue
                    slots_left = SLOTS - len(positions)
                    if slots_left <= 0:
                        skip_slot += 1
                        continue
                    invest = cash / slots_left
                    if invest <= 0:
                        skip_slot += 1
                        continue
                    positions.append({"code": row["code"], "exit_date": row["exit_date"],
                                      "strategy": strat,
                                      "invested_value": invest,
                                      "entry_price": row["entry_price"],
                                      "net_pct": row["net_pct"]})
                    held.add(row["code"])
                    used_by[strat] = used_by.get(strat, 0) + 1
                    cash -= invest
                    entered_today += 1

        # 4) 일별 MTM equity
        invested = 0.0
        for p in positions:
            close = price_map.get((p["code"], date))
            invested += p["invested_value"] * (close / p["entry_price"]
                                               if close and p["entry_price"] > 0 else 1.0)
        equity = cash + invested
        eq_curve.append(equity)
        prev_equity = equity
        peak = max(peak, equity)

    eq = pd.Series(eq_curve)
    ret = (eq.iloc[-1] / CAPITAL0 - 1) * 100
    mdd = float(((eq - eq.cummax()) / eq.cummax() * 100).min())
    daily_r = eq.pct_change().dropna()
    sharpe = float(daily_r.mean() / daily_r.std() * (252 ** 0.5)) if daily_r.std() > 0 else 0.0
    n_exec = len(df) - (skip_breaker + skip_cap + skip_slot + skip_dup)
    return {
        "ret": round(ret, 1), "mdd": round(mdd, 1), "sharpe": round(sharpe, 2),
        "n_exec": n_exec, "skip_breaker": skip_breaker, "skip_cap": skip_cap,
        "halt_days": halt_days, "halt_ep": halt_episodes,
    }


def main():
    print("=" * 78)
    print("  서킷브레이커/일일상한 스윕 — 표만 출력, 자동선택 없음(full/fwd 일관 + plateau 로 판단)")
    print("=" * 78)
    print("\n[1/3] 데이터 로딩...")
    df = load_macro_daily()
    df = df.reset_index(drop=True)
    df["code"] = df["code"].astype(str).str.zfill(6)
    mkt = df.groupby("date")["change_pct"].mean()
    df["mkt_strong"] = df["date"].map((mkt.rolling(60, min_periods=60).mean() > 0).to_dict()).fillna(False)
    price_map, trading_dates = build_price_lookup(df)
    fwd_start = args.fwd_start or trading_dates[int(len(trading_dates) * 0.7)]
    print(f"  거래일 {len(trading_dates)}일 / forward: {fwd_start}~")

    print("[2/3] 라이브 6전략 백테스트...")
    live = {s.name: s for s in ALL_STRATEGIES
            if s.name in {n for caps in ACCOUNTS.values() for n in caps}}
    trades_by_acct = {}
    for acct, caps in ACCOUNTS.items():
        rows = []
        for nm in caps:
            try:
                ts = live[nm].backtest(df.copy(), DEFAULT_COSTS)
            except Exception as e:
                print(f"  [{nm}] ERROR: {e}")
                continue
            for t in ts:
                ed, xd = str(t.entry_date), str(t.exit_date)
                if len(ed) == 8 and len(xd) == 8 and t.entry_price > 0:
                    rows.append({"entry_date": ed, "exit_date": xd,
                                 "code": str(t.code).zfill(6),
                                 "strategy": nm,
                                 "entry_price": float(t.entry_price),
                                 "net_pct": float(t.net_pct),
                                 "score": float(getattr(t, "score", 0) or 0)})
            print(f"  [{acct}/{nm}] {len(ts):,}건 (슬롯상한 {caps[nm]})")
        trades_by_acct[acct] = pd.DataFrame(rows)

    print("[3/3] 스윕...")
    _scores = _load_scores()
    if _scores is None and args.min_strength > 0:
        print("  [warn] 워크포워드 채점 데이터를 찾지 못했습니다 — 강도 필터 미적용으로 진행합니다.")
        print("         기준선이 '강도 도입 이전 시스템'이 되어 차단기 효과가 과대평가됩니다.")
    out_rows = []
    for acct, tdf in trades_by_acct.items():
        tdf, _rep = apply_strength(tdf, _scores, args.min_strength)
        print(f"  [{acct}] {_rep}")
        tdf_fwd = tdf[tdf["entry_date"] >= fwd_start]
        for bx in BREAKERS:
            for cap in DAILY_CAPS:
                full = simulate(tdf, price_map, trading_dates,
                                ACCOUNTS[acct], PRIORITY[acct], bx, cap)
                fwd = simulate(tdf_fwd, price_map, trading_dates,
                               ACCOUNTS[acct], PRIORITY[acct], bx, cap)
                if full is None:
                    continue
                out_rows.append({
                    "account": acct,
                    "breaker": (f"-{bx:.0f}%" if bx else "없음"),
                    "daily_cap": (cap if cap else "없음"),
                    "ret": full["ret"], "mdd": full["mdd"], "sharpe": full["sharpe"],
                    "n_exec": full["n_exec"], "skip_brk": full["skip_breaker"],
                    "skip_cap": full["skip_cap"], "halt_days": full["halt_days"],
                    "halt_ep": full["halt_ep"],
                    "fwd_ret": (fwd or {}).get("ret"), "fwd_mdd": (fwd or {}).get("mdd"),
                    "fwd_sharpe": (fwd or {}).get("sharpe"),
                })

    res = pd.DataFrame(out_rows)
    res.to_csv("breaker_sweep_result.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장: breaker_sweep_result.csv ({len(res)}행)\n")
    for acct in ACCOUNTS:
        sub = res[res["account"] == acct]
        print(f"─── {acct} (10슬롯) " + "─" * 55)
        print(f"  {'브레이커':>7} {'일상한':>5} | {'수익%':>8} {'MDD%':>7} {'샤프':>5} "
              f"{'중단일':>5} {'차단':>5} | {'fwd수익%':>8} {'fwdMDD%':>8}")
        for _, r in sub.iterrows():
            print(f"  {r['breaker']:>7} {str(r['daily_cap']):>5} | {r['ret']:>8} {r['mdd']:>7} "
                  f"{r['sharpe']:>5} {r['halt_days']:>5} {r['skip_brk']:>5} | "
                  f"{str(r['fwd_ret']):>8} {str(r['fwd_mdd']):>8}")
        print()
    print("※ 판단 기준: '없음' 행 대비 MDD 개선폭 vs 수익 훼손폭 — full/fwd 방향 일치 + 이웃 X plateau.")


if __name__ == "__main__":
    main()
