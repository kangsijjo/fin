"""
Paper vs 백테스트 Drift 감사 (paper_audit.py)

paper_signals.csv 의 실제 결과를 집계하여 백테스트 기대치와 비교.

목적:
  - "백테스트에서 좋았던 전략이 실전에서 왜 다르게 나오는가" 추적
  - 전략별 승률·평균수익·big-win 비율 drift 출력
  - 신호 수 차이 (백테 vs paper) 비교

실행: python paper_audit.py
출력: paper_audit_result.csv  (신호별 실제 수익률 포함)
"""

import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Tuple

# ── 경로 ────────────────────────────────────────────────────────────────────
PAPER_CSV   = "./paper_signals.csv"
HISTORY_CSV = "./trades_history_v3.csv"
CSV_DIR     = "./macro_data/daily"
OUT_CSV     = "./paper_audit_result.csv"

BIG_WIN_PCT  = 10.0   # big-win 기준 (ai_trainer_v4 와 동일)
SLIP_PCT     = 0.15   # 슬리피지 (매수·매도 각 0.15%)
COMMISSION   = 0.015  # 수수료 편도


# ── 1. 청산가 조회 ──────────────────────────────────────────────────────────
def _close_price(date_str: str, code_str: str) -> Optional[float]:
    """pykrx CSV 에서 특정 날짜·종목의 종가 반환. 없으면 None."""
    p = f"{CSV_DIR}/{date_str}.csv"
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_csv(p, dtype={"code": str})
        df["code"] = df["code"].str.zfill(6)
        row = df[df["code"] == code_str.zfill(6)]
        if len(row) == 0:
            return None
        return float(row["close"].iloc[0])
    except Exception:
        return None


def _next_trading_close(date_str: str, code_str: str, max_days: int = 5) -> Tuple[Optional[str], Optional[float]]:
    """date_str 이후 최초 거래일의 종가 반환 (최대 max_days 영업일 탐색)."""
    dt = datetime.strptime(date_str, "%Y%m%d")
    for i in range(max_days + 1):
        d = (dt + timedelta(days=i)).strftime("%Y%m%d")
        px = _close_price(d, code_str)
        if px is not None and px > 0:
            return d, px
    return None, None


# ── 2. paper 신호 실제 결과 계산 ─────────────────────────────────────────────
def compute_paper_returns(paper: pd.DataFrame) -> pd.DataFrame:
    rows = []
    total = len(paper)
    today_str = datetime.today().strftime("%Y%m%d")

    for idx, (_, r) in enumerate(paper.iterrows(), 1):
        code = str(r["code"]).zfill(6)
        entry_px = float(r["entry_price_close"])
        # target_exit_date 는 NaN(만기 미산출) 가능 — int 변환 전 가드(과거 NaN 행에서 ValueError 크래시, 2026-06-30 수정)
        exit_date_str = str(int(r["target_exit_date"])) if pd.notna(r["target_exit_date"]) else ""
        signal_date   = str(int(r["signal_date"]))

        # 만기 미도래(또는 만기일 미산출=pending)
        if (not exit_date_str) or exit_date_str > today_str:
            status = "pending"
            actual_exit_date = None
            exit_px = None
            net_pct = None
        else:
            actual_exit_date, exit_px = _next_trading_close(exit_date_str, code)
            if exit_px is None or entry_px <= 0:
                status = "no_data"
                net_pct = None
            else:
                gross_pct = (exit_px / entry_px - 1) * 100
                cost = (SLIP_PCT + COMMISSION) * 2   # 왕복
                net_pct = gross_pct - cost
                status = "done"

        rows.append({
            "signal_date":      signal_date,
            "code":             code,
            "name":             r.get("name", ""),
            "entry_price":      entry_px,
            "target_exit_date": exit_date_str,
            "actual_exit_date": actual_exit_date,
            "exit_price":       exit_px,
            "net_pct":          round(net_pct, 4) if net_pct is not None else None,
            "status":           status,
            "lookback_high":    r.get("lookback_high", ""),
            "market_strong":    r.get("market_strong", ""),
        })

        # 진행 게이지
        if sys.stdout.isatty():
            pct = idx / total * 100
            print(f"\r[{pct:5.1f}%] {idx}/{total} | {code} {exit_date_str}", end="", flush=True)
        elif idx % 100 == 0 or idx == total:
            print(f"  {idx}/{total}", flush=True)

    if sys.stdout.isatty():
        print()
    return pd.DataFrame(rows)


# ── 3. 비교 리포트 ───────────────────────────────────────────────────────────
def report(paper_result: pd.DataFrame, history: pd.DataFrame):
    done = paper_result[paper_result["status"] == "done"].copy()
    pending = (paper_result["status"] == "pending").sum()
    no_data = (paper_result["status"] == "no_data").sum()

    print(f"\n{'='*60}")
    print(f"  Paper Audit 결과 ({datetime.today().strftime('%Y-%m-%d')})")
    print(f"{'='*60}")
    print(f"  총 신호: {len(paper_result):,}건")
    print(f"    완료(done): {len(done):,}건")
    print(f"    대기(pending): {pending:,}건  |  데이터없음: {no_data:,}건")
    print()

    if len(done) == 0:
        print("  완료된 신호가 없습니다.")
        return

    # ── Paper 실제 성과 ──
    wr_paper   = (done["net_pct"] > 0).mean()
    avg_paper  = done["net_pct"].mean()
    med_paper  = done["net_pct"].median()
    bw_paper   = (done["net_pct"] >= BIG_WIN_PCT).mean()

    print(f"  [Paper 실제]")
    print(f"    승률:    {wr_paper:.1%}")
    print(f"    평균:    {avg_paper:+.2f}%")
    print(f"    중앙값:  {med_paper:+.2f}%")
    print(f"    big-win: {bw_paper:.1%}  (>={BIG_WIN_PCT}%)")
    print()

    # ── 백테스트 동기간 기대치 (paper 신호 기간과 겹치는 구간) ──
    p_start = done["signal_date"].min()
    p_end   = done["signal_date"].max()
    hist_sub = history[
        (history["date"].astype(str) >= p_start) &
        (history["date"].astype(str) <= p_end)
    ]

    if len(hist_sub) == 0:
        print("  백테스트에 동기간 데이터 없음")
    else:
        wr_bt  = (hist_sub["net_pct"] > 0).mean()
        avg_bt = hist_sub["net_pct"].mean()
        med_bt = hist_sub["net_pct"].median()
        bw_bt  = (hist_sub["net_pct"] >= BIG_WIN_PCT).mean()

        print(f"  [백테스트 동기간 {p_start}~{p_end}]")
        print(f"    신호 수: {len(hist_sub):,}건  (백테) vs {len(done):,}건  (paper)")
        print(f"    승률:    {wr_bt:.1%}     ← 기대치")
        print(f"    평균:    {avg_bt:+.2f}%")
        print(f"    중앙값:  {med_bt:+.2f}%")
        print(f"    big-win: {bw_bt:.1%}")
        print()

        # ── Drift ──
        print(f"  [Drift (paper - 백테)]")
        print(f"    승률:    {(wr_paper - wr_bt):+.1%}")
        print(f"    평균:    {(avg_paper - avg_bt):+.2f}%p")
        print(f"    big-win: {(bw_paper - bw_bt):+.1%}")
        print()

        # ── 신호 수 차이 원인 분석 ──
        if len(done) < len(hist_sub) * 0.5:
            print("  ⚠  paper 신호 수가 백테 대비 50% 미만")
            print("     → 유니버스 필터, market_strong 조건, 슬롯 수 점검 필요")
        elif len(done) > len(hist_sub) * 2:
            print("  ⚠  paper 신호 수가 백테 대비 2배 이상")
            print("     → 중복 신호 또는 청산 미처리 건 누적 점검 필요")

    # ── 수익률 분포 ──
    print(f"  [수익률 구간별 비율]")
    bins   = [-999, -20, -10, -5, 0, 5, 10, 20, 999]
    labels = ["<-20%", "-20~-10%", "-10~-5%", "-5~0%", "0~5%", "5~10%", "10~20%", ">20%"]
    cuts   = pd.cut(done["net_pct"], bins=bins, labels=labels)
    dist   = cuts.value_counts().sort_index()
    for label, cnt in dist.items():
        bar = "█" * int(cnt / max(dist) * 20)
        print(f"    {label:>10}  {bar:<20}  {cnt:4}건  ({cnt/len(done):.1%})")

    # ── 최악/최선 5종목 ──
    print(f"\n  [최선 5종목]")
    top5 = done.nlargest(5, "net_pct")[["signal_date","code","name","net_pct","actual_exit_date"]]
    for _, r2 in top5.iterrows():
        print(f"    {r2['signal_date']} {r2['code']} {r2['name']:12s}  {r2['net_pct']:+.1f}%")

    print(f"\n  [최악 5종목]")
    bot5 = done.nsmallest(5, "net_pct")[["signal_date","code","name","net_pct","actual_exit_date"]]
    for _, r2 in bot5.iterrows():
        print(f"    {r2['signal_date']} {r2['code']} {r2['name']:12s}  {r2['net_pct']:+.1f}%")

    print(f"\n{'='*60}\n")


# ── main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not os.path.exists(PAPER_CSV):
        print(f"[오류] {PAPER_CSV} 없음")
        sys.exit(1)

    paper   = pd.read_csv(PAPER_CSV)
    history = pd.read_csv(HISTORY_CSV) if os.path.exists(HISTORY_CSV) else pd.DataFrame()

    print(f"paper_signals: {len(paper):,}건 로드")
    print("실제 결과 계산 중 (pykrx CSV 조회)...")

    result = compute_paper_returns(paper)
    result.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"→ {OUT_CSV} 저장 완료")

    report(result, history)
