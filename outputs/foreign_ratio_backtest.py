# -*- coding: utf-8 -*-
"""
foreign_ratio_backtest.py — 외국인 '지분율 상승' vs '순매수 플로우' A/B 백테스트
(2026-07-21 신설)

목적: 신규 ForeignRatioHighStrategy(지분율 상승 + 신고가)를 기존 외국인 순매수
      3전략(baseline)과 같은 유니버스·비용·기간에서 비교해 '실제로 더 나은지'를
      데이터로 판정. 엣지 확인 전에는 trades_history_v3(AI/IC 학습셋)에 넣지 않는다
      (검증 전 학습 오염 방지). 통과 시에만 STRATEGIES 편입 + 라이브 배포 논의.

실행: cd C:/fin/outputs ; .venv/Scripts/python.exe foreign_ratio_backtest.py
"""
import os
import sys
import sqlite3
import statistics as st

import pandas as pd

# 콘솔 인코딩 방탄 — cp949 콘솔에서 박스문자/한글 print 크래시 방지(타 파일과 동일).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from strategies.daily_loader import load_macro_daily, filter_universe, default_costs
from strategies.high52w_foreign import High52wForeignStrategy
from strategies.foreign_high import ForeignHighStrategy
from strategies.gc_foreign import GcForeignStrategy
from strategies.foreign_ratio_high import ForeignRatioHighStrategy
from strategies.foreign_inst_flow_ratio import ForeignInstFlowRatioStrategy

STOCK_DB_CANDIDATES = [
    os.getenv("STOCK_DB", ""),
    "../Stock_AI_Project/data/stock.db",
    "C:/fin/Stock_AI_Project/data/stock.db",
]


def merge_foreign_ratio(df):
    """foreign_ratio 테이블 → df 에 'foreign_holding_ratio' 컬럼 병합(코드·날짜 키)."""
    path = next((p for p in STOCK_DB_CANDIDATES if p and os.path.exists(p)), None)
    if not path:
        print("[foreign_ratio] stock.db 없음 — 지분율 미병합"); return df
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        fr = pd.read_sql("SELECT date, ticker, holding_ratio FROM foreign_ratio", con)
        con.close()
    except Exception as e:
        print(f"[foreign_ratio] 로드 실패({e}) — 미병합"); return df
    if not len(fr):
        print("[foreign_ratio] 데이터 0행 — 미병합"); return df
    fr["date"] = fr["date"].astype(str).str.replace("-", "").str[:8]
    fr["code"] = fr["ticker"].astype(str).str.zfill(6)
    fr = fr.rename(columns={"holding_ratio": "foreign_holding_ratio"})
    fr = fr[["code", "date", "foreign_holding_ratio"]]
    df = df.merge(fr, on=["code", "date"], how="left")
    cov = df["foreign_holding_ratio"].notna().mean()
    print(f"[foreign_ratio] 병합: {fr['date'].nunique():,}일 / 커버리지 {cov:.1%} "
          f"(범위 {fr['date'].min()}~{fr['date'].max()})")
    return df


def metrics(trades):
    """StrategyTrade 리스트 → 요약 지표."""
    nets = [float(t.net_pct) for t in trades]
    n = len(nets)
    if n == 0:
        return dict(n=0, win=0.0, avg=0.0, med=0.0, best=0.0, worst=0.0)
    wins = sum(1 for x in nets if x > 0)
    return dict(
        n=n,
        win=wins / n * 100,
        avg=sum(nets) / n,
        med=st.median(nets),
        best=max(nets),
        worst=min(nets),
    )


def main():
    print("=== 외국인 지분율 상승 vs 순매수 플로우 A/B 백테스트 ===\n")
    df = load_macro_daily()
    df = filter_universe(df)
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = merge_foreign_ratio(df)
    if "foreign_holding_ratio" not in df.columns or df["foreign_holding_ratio"].notna().sum() == 0:
        print("\n[중단] 지분율 데이터가 없습니다. 백필 완료 후 다시 실행하세요.")
        return
    costs = default_costs()

    # baseline = 기존 외국인 순매수 3전략 / 후보 = 지분율 상승 변형들
    runs = [
        ("[flow] h52w_for3d_mkt", High52wForeignStrategy(
            foreign_n=3, holding_days=20, use_market_filter=True, name="h52w_for3d_mkt")),
        ("[flow] for_high20_mkt", ForeignHighStrategy(
            n_days=3, high_period=20, holding_days=20, use_market_filter=True, name="for_high20_mkt")),
        ("[flow] gc_for3d", GcForeignStrategy(
            fast_ma=20, slow_ma=60, foreign_n=3, holding_days=15, name="gc_for3d")),
        # ── 지분율 상승 변형 (신고가 창·상승폭·시장게이트 조합) ──
        ("[ratio] rn5 d0 h20", ForeignRatioHighStrategy(
            ratio_n=5, ratio_delta_min=0.0, lookback_days=20, holding_days=20)),
        ("[ratio] rn5 d0 h20 MKT", ForeignRatioHighStrategy(
            ratio_n=5, ratio_delta_min=0.0, lookback_days=20, holding_days=20, use_market_filter=True)),
        ("[ratio] rn20 d0.1 h20", ForeignRatioHighStrategy(
            ratio_n=20, ratio_delta_min=0.1, lookback_days=20, holding_days=20)),
        ("[ratio] rn20 d0.2 h20 MKT", ForeignRatioHighStrategy(
            ratio_n=20, ratio_delta_min=0.2, lookback_days=20, holding_days=20, use_market_filter=True)),
        ("[ratio] rn10 d0.1 h252", ForeignRatioHighStrategy(
            ratio_n=10, ratio_delta_min=0.1, lookback_days=252, holding_days=20)),
        # ── 2단 전략(사용자 설계): 외국인+기관 순매수 top100 N일연속 → 지분율 2~3주 상승 ──
        ("[2stage] t100 c5 up15 d0", ForeignInstFlowRatioStrategy(
            top_n=100, flow_consec=5, ratio_up_days=15, ratio_delta_min=0.0, holding_days=20)),
        ("[2stage] t100 c5 up15 d0.1", ForeignInstFlowRatioStrategy(
            top_n=100, flow_consec=5, ratio_up_days=15, ratio_delta_min=0.1, holding_days=20)),
        ("[2stage] t100 c5 up10 d0 both", ForeignInstFlowRatioStrategy(
            top_n=100, flow_consec=5, ratio_up_days=10, ratio_delta_min=0.0,
            both_positive=True, holding_days=20)),
        ("[2stage] t50 c3 up15 d0.1", ForeignInstFlowRatioStrategy(
            top_n=50, flow_consec=3, ratio_up_days=15, ratio_delta_min=0.1, holding_days=20)),
        ("[2stage] t100 c5 up15 d0.1 MKT", ForeignInstFlowRatioStrategy(
            top_n=100, flow_consec=5, ratio_up_days=15, ratio_delta_min=0.1,
            use_market_filter=True, holding_days=20)),
    ]

    print(f"\n{'전략':28s} {'매매수':>7s} {'승률%':>7s} {'평균%':>7s} {'중앙%':>7s} {'최고%':>7s} {'최악%':>8s}")
    print("-" * 80)
    rows = []
    for label, strat in runs:
        m = metrics(strat.backtest(df, costs))
        rows.append((label, m))
        print(f"{label:28s} {m['n']:>7,} {m['win']:>7.1f} {m['avg']:>7.2f} "
              f"{m['med']:>7.2f} {m['best']:>7.1f} {m['worst']:>8.1f}")

    # 요약: 최고 flow vs 최고 후보(ratio·2stage 통합)
    flows = [(l, m) for l, m in rows if l.startswith("[flow]") and m["n"] > 0]
    ratios = [(l, m) for l, m in rows
              if (l.startswith("[ratio]") or l.startswith("[2stage]")) and m["n"] > 0]
    if flows and ratios:
        # 공정 비교: 최고 ratio vs 최고 flow(‘평균 flow’와 비교하면 ratio 쪽에 유리하게 기울음).
        # 배포 가치 판정은 '최고 flow 를 유의미하게(+0.3%p) 상회 + 표본 충분' 일 때만.
        best_flow = max(flows, key=lambda x: x[1]["avg"])
        best_ratio = max(ratios, key=lambda x: x[1]["avg"])
        bf, br = best_flow[1], best_ratio[1]
        print("\n── 요약(공정: 최고 vs 최고) ──")
        print(f"  최고 flow ({best_flow[0]}): 승률 {bf['win']:.1f}% / 평균 {bf['avg']:+.2f}% / 중앙 {bf['med']:+.2f}% (n {bf['n']:,})")
        print(f"  최고 ratio({best_ratio[0]}): 승률 {br['win']:.1f}% / 평균 {br['avg']:+.2f}% / 중앙 {br['med']:+.2f}% (n {br['n']:,})")
        beats = br["avg"] > bf["avg"] + 0.3 and br["n"] >= 100
        print(f"  판정: {'지분율이 최고 flow 를 유의미하게 상회 — 배포 검토' if beats else '지분율이 최고 flow 를 유의미하게 넘지 못함 — 배포 보류(기존 순매수 전략이 이미 동등 이상)'}")


if __name__ == "__main__":
    main()
