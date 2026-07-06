"""
slippage_report.py — 실체결 슬리피지 리포트 (매수: 브로커 매입평균 vs 신호일 종가).

kiwoom_trader.cmd_status 가 매 실행마다 db/kiwoom/slippage_log.csv 에 (code, buy_date)
멱등 누적한 기록을 집계한다. 백테스트/페이퍼의 진입 가정 검증용:
  - paper_tracker X2 모드 가정: 신호일 종가 × 1.02 (즉 +2.0% 슬리피지 가정)
  - paper_audit 비용 가정: 슬리피지 편도 0.15%
  - 실측이 이 가정들과 얼마나 다른지 → 백테스트 신뢰도의 실측 근거.

사용: python slippage_report.py
"""

import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import pandas as pd  # noqa: E402

LOG = "./db/kiwoom/slippage_log.csv"


def main():
    if not os.path.exists(LOG):
        print(f"기록 없음: {LOG}")
        print("kiwoom_trader.py status 실행 시 보유종목의 매수 슬리피지가 자동 누적됩니다.")
        return
    df = pd.read_csv(LOG, dtype={"code": str})
    df["slip_pct"] = pd.to_numeric(df["slip_pct"], errors="coerce")
    df = df.dropna(subset=["slip_pct"])
    if df.empty:
        print("유효 기록 없음")
        return

    print("=" * 66)
    print(f"  실체결 매수 슬리피지 — 표본 {len(df)}건  ({df['buy_date'].min()} ~ {df['buy_date'].max()})")
    print("  (+ = 신호일 종가보다 비싸게 체결 / 가정: X2=+2.0%, audit=+0.15%)")
    print("=" * 66)
    s = df["slip_pct"]
    print(f"  전체    : 평균 {s.mean():+.3f}%  중앙값 {s.median():+.3f}%  "
          f"표준편차 {s.std():.3f}  최소 {s.min():+.2f}  최대 {s.max():+.2f}")
    for strat, g in df.groupby("strategy"):
        print(f"  {strat:<16}: {len(g):3d}건  평균 {g['slip_pct'].mean():+.3f}%  "
              f"중앙값 {g['slip_pct'].median():+.3f}%")
    print()
    print("  최근 10건:")
    for _, r in df.tail(10).iloc[::-1].iterrows():
        print(f"    {r['buy_date']} {r['code']} {str(r['name'])[:8]:<8} "
              f"[{r['strategy']}] 신호종가 {int(r['signal_close']):,} → "
              f"체결평균 {int(r['fill_avg']):,}  ({r['slip_pct']:+.2f}%)")
    print()
    avg = s.mean()
    if abs(avg - 2.0) > 1.0:
        print(f"  ※ 실측 평균({avg:+.2f}%)이 X2 가정(+2.0%)과 1%p 이상 다름 — "
              f"paper_tracker ENTRY_SLIPPAGE 재보정 검토 근거.")


if __name__ == "__main__":
    main()
