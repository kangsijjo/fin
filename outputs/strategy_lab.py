"""
전략 실험실 (forward paper lab) — 2026-06-19 신설.

strategies/ 의 모든 후보 전략(strategy_engine.ALL_STRATEGIES)을 매일 backtest 돌려,
LAB_START 이후 진입(entry_date >= LAB_START)한 트레이드만 모아
"지금부터의 진짜 forward(OOS) 성과"를 전략별로 집계한다.

핵심: 전략은 결정론적·look-ahead 없음 → 매일 재실행하면 새로 '완료'된 forward 트레이드가
자동 누적된다(보유기간 만료될 때마다 표본 +1). 백테스트 OOS와 달리 낙관편향 0.

두 모드 (사용자 결정: 둘 다 기록):
  * unlimited : 신호 뜬 트레이드 전부(슬롯 경쟁 없음) → 전략의 '순수 엣지'.
  * slot      : capital_simulator 로 N슬롯 자본제약 → '현실 운용' 수익.

출력:
  results/strategy_lab_{today}.csv         전략별 1행 (두 모드 지표)
  results/strategy_lab_trades_{today}.csv  forward 트레이드 원장 (대시보드/검증용)

수동 실행: python strategy_lab.py
주의: 초기(06-20 직후)엔 보유기간이 안 끝나 표본이 0~소수다. 시간이 지나며 쌓인다.
"""

import os
import sys
from datetime import datetime

import pandas as pd

# 같은 폴더 모듈 import 보장
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_engine import ALL_STRATEGIES, DEFAULT_COSTS   # noqa: E402
from strategies.daily_loader import load_macro_daily          # noqa: E402
from capital_simulator import simulate_capital                # noqa: E402

# ── 설정 ──────────────────────────────────────────────────────────────────────
LAB_START = "20260620"   # clean forward 시작일. 이날 이후 '진입'한 트레이드만 집계.
SLOT_N    = 10           # slot 모드의 동시보유 슬롯 수 (현실 운용 가정)


def _unlimited_stats(trades):
    """슬롯 제약 없는 전 트레이드 집계 — 전략의 순수 엣지."""
    nets = [float(t.net_pct) for t in trades]
    if not nets:
        return {"n": 0, "avg_net": 0.0, "win_rate": 0.0, "sum_net": 0.0}
    wins = [x for x in nets if x > 0]
    return {
        "n":        len(nets),
        "avg_net":  round(sum(nets) / len(nets), 3),
        "win_rate": round(100 * len(wins) / len(nets), 1),
        "sum_net":  round(sum(nets), 2),
    }


def main():
    today = datetime.today().strftime("%Y%m%d")
    # 검증용: python strategy_lab.py 20250101  → 과거 날짜로 표본 채워 로직 확인.
    # 인자 없으면 실제 운영값(LAB_START, clean forward) 사용.
    lab_start = sys.argv[1] if len(sys.argv) > 1 else LAB_START
    print(f"=== 전략 랩 (forward, LAB_START={lab_start}, {SLOT_N}슬롯) ===")

    df = load_macro_daily().reset_index(drop=True)
    print(f"[data] {df['code'].nunique()}종목, 최신 {df['date'].max()}")

    rows = []
    trade_log = []
    for strat in ALL_STRATEGIES:
        try:
            trades = strat.backtest(df.copy(), DEFAULT_COSTS)
        except Exception as e:
            print(f"  {strat.name:<24} ERROR: {str(e)[:50]}")
            rows.append({"strategy": strat.name, "fwd_n": 0, "error": str(e)[:60]})
            continue

        # forward 만: 진입일이 lab_start 이상 (YYYYMMDD 문자열 비교 = 시간순)
        fwd = [t for t in trades if str(t.entry_date) >= lab_start]
        u = _unlimited_stats(fwd)
        sim = simulate_capital(fwd, max_concurrent=SLOT_N) if fwd else None

        rows.append({
            "strategy":       strat.name,
            "fwd_n":          u["n"],            # forward 완료 트레이드 수
            "fwd_avg_net":    u["avg_net"],      # 무제한 평균 순수익률(%)
            "fwd_win_rate":   u["win_rate"],     # 무제한 승률(%)
            "fwd_sum_net":    u["sum_net"],      # 무제한 누적 순수익(%) 합
            "slot_total_ret": (sim or {}).get("total_ret_pct", 0.0),  # 슬롯제한 총수익(%)
            "slot_cagr":      (sim or {}).get("cagr_pct", 0.0),
            "slot_mdd":       (sim or {}).get("real_mdd_pct", 0.0),
            "slot_sharpe":    (sim or {}).get("real_sharpe", 0.0),
            "slot_executed":  (sim or {}).get("n_trades", 0),         # 슬롯에 실제 체결된 수
        })
        for t in fwd:
            trade_log.append({
                "strategy":     t.strategy,
                "code":         t.code,
                "entry_date":   t.entry_date,
                "exit_date":    t.exit_date,
                "net_pct":      round(float(t.net_pct), 3),
                "holding_days": t.holding_days,
            })
        print(f"  {strat.name:<24} forward {u['n']:>3}건  avg {u['avg_net']:>6}%  승률 {u['win_rate']:>5}%")

    os.makedirs("results", exist_ok=True)
    out = f"results/strategy_lab_{today}.csv"
    df_res = pd.DataFrame(rows)
    if "fwd_sum_net" in df_res.columns:
        df_res = df_res.sort_values("fwd_sum_net", ascending=False)
    df_res.to_csv(out, index=False, encoding="utf-8-sig")
    if trade_log:
        pd.DataFrame(trade_log).to_csv(
            f"results/strategy_lab_trades_{today}.csv", index=False, encoding="utf-8-sig")

    print(f"\n저장: {out}  ({len(rows)} 전략)")
    n_with = sum(1 for r in rows if r.get("fwd_n", 0) > 0)
    print(f"forward 표본 있는 전략: {n_with}/{len(rows)}  "
          f"(LAB_START={lab_start} 이후 진입·만료된 트레이드만 — 초기엔 적고 점차 누적)")


if __name__ == "__main__":
    main()
