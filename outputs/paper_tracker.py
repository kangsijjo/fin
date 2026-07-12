"""
Paper Trading 추적기 — paper_signals.csv 의 모든 신호로 가상 매매 수행.

매일 실행:
  python paper_tracker.py

기능:
  1. paper_signals.csv 로드 (v2: strategy / holding_days 컬럼 포함)
  2. 각 신호(X2 모드, ENTRY_AT_CLOSE=True):
     - entry = 신호일 T 종가 × 1.02 (시간외 매수 가정)
     - exit  = T + holding - 1 거래일 종가 (진입일 T 포함 holding일)
  3. max_concurrent=10 전역 슬롯 cap + 자본 곡선/CAGR/MDD + 전략별 손익

⚠ 해석 주의(2026-07-12 명시 — 라이브 실계좌와의 의도적/구조적 차이):
  ① X2 모드는 T 종가 진입이라 청산일이 라이브(T+1 진입, T+holding 매도)보다
     1거래일 이르다 — 백테스트 검증(X2 walk-forward CAGR +47%p)이 이 규약이므로
     유지. 실계좌와 트레이드 단위 비교엔 paper_audit/실계좌 스냅샷을 쓸 것.
  ② 전략별 슬롯(4/4/2)·강도필터 6.0·강도순 우선순위는 재현하지 않는다 —
     전역 10슬롯 근사(= '신호 풀 전체'의 성과 추적용). 안C 실계좌 구조 재현이
     필요하면 별도 작업(후속 과제).
"""

import os
import sys
import pandas as pd
import config
from strategies.daily_loader import load_macro_daily
from strategies._swing_base import find_corporate_action_dates, _trade_has_corporate_action
from capital_simulator import simulate_capital, build_price_lookup
from strategies.base import StrategyTrade

SIGNALS_CSV = "./paper_signals.csv"
HOLDING_DAYS_DEFAULT = 40   # 구버전 CSV 호환 fallback
MAX_CONCURRENT = 10
INITIAL_CAPITAL = 10_000_000
# 비용 0.33% = 수수료 0.015×2 + 거래세 0.20 + 매도 슬리피지 0.10.
# 매수 슬리피지는 X2 모드의 ENTRY_SLIPPAGE_PCT(+2%)로 별도 반영되므로 여기서 제외.
# (백테스트 default_costs 0.43% 는 매수 슬리피지 0.10 포함 기준 — 불일치 아님)
COST_PCT = 0.330

# X2 모드 — 시간외 매수 가정 (T close × (1+ENTRY_SLIPPAGE_PCT/100))
# walk-forward 검증: CAGR +47%p, Sharpe +0.17 (원본 대비)
ENTRY_AT_CLOSE = True
ENTRY_SLIPPAGE_PCT = 2.0

# AI 통합 — 매매 결정 영향 X (사용자 합의). dashboard 표시만.
# (이전 외부 조언의 AI 거부권 로직은 비활성화. AI 는 dashboard 에서 정보 표시용)
USE_AI = False
AI_MODEL = None

def get_today_market_features():
    feat_path = "./ai_data/historical_features.csv"
    if not os.path.exists(feat_path):
        return None
    df = pd.read_csv(feat_path)
    return df.iloc[[-1]][['kosdaq_return', 'kosdaq_disparity', 'kosdaq_volatility', 
                          'kosdaq_foreign_net', 'kosdaq_inst_net']]

def main():
    if not os.path.exists(SIGNALS_CSV):
        print(f"[error] {SIGNALS_CSV} 없음. 먼저 python live_signal.py 실행.")
        sys.exit(1)

    print(f"=== Paper Trading 추적 ===")
    print(f"운용 전략: 안C 포트폴리오 (high_52w_filt×4 / rsi_reversal×4 / rsi_vol×2)")
    print(f"자본: {INITIAL_CAPITAL:,}원 / max_concurrent={MAX_CONCURRENT}")

    # 1) 신호 로드
    signals = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    signals["signal_date"] = signals["signal_date"].astype(str)
    signals["code"] = signals["code"].astype(str).str.zfill(6)
    # 완전중복 이중집계 방지(2026-07-12) — 20260619 잔재 104행이 승률·MDD 를 2배 가중하던 것
    _n0 = len(signals)
    if "strategy" in signals.columns:
        signals = signals.drop_duplicates(["signal_date", "code", "strategy"], keep="first")
    if len(signals) != _n0:
        print(f"[dedup] 완전중복 신호 {_n0 - len(signals)}건 제외")

    # v2 schema 호환: strategy / holding_days 컬럼 없으면 기본값 채움
    if "strategy" not in signals.columns:
        signals["strategy"] = "high_52w_filt"
    if "holding_days" not in signals.columns:
        signals["holding_days"] = HOLDING_DAYS_DEFAULT
    signals["holding_days"] = signals["holding_days"].fillna(HOLDING_DAYS_DEFAULT).astype(int)

    strat_counts = signals["strategy"].value_counts().to_dict()
    print(f"\n[signals] 누적 {len(signals)} 건  "
          + " / ".join(f"{k}: {v}" for k, v in strat_counts.items()))

    if signals.empty:
        print("아직 신호 없음.")
        return

    # 2) 가격 데이터 로드
    df = load_macro_daily()
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)

    # 종목별 시계열 dict (빠른 lookup)
    code_dates = df.groupby("code")[["date"]].apply(
        lambda x: sorted(x["date"].tolist())).to_dict()
    price_map = df.set_index(["code", "date"])[["open", "close"]].to_dict("index")

    # 기업행위(액면분할/병합) 발생일 — 해당 매매는 가격 왜곡이라 통계에서 제외
    ca_map = find_corporate_action_dates(df)

    # 3) 신호 → 가상 매매 (entry/exit 결정)
    # X2 모드: T 종가 × 1.02 매수 (시간외 가정)
    # 원본 모드: T+1 시가 매수, 진입일 포함 holding_days 영업일째 종가 매도
    # ※ holding_days 는 전략별로 다름 (high_52w_filt: 20, rsi_reversal: 5)
    trades = []
    n_ca_skipped = 0
    for _, sig in signals.iterrows():
        code = sig["code"]
        sig_date = sig["signal_date"]
        holding = int(sig["holding_days"])   # 전략별 보유일

        if code not in code_dates:
            continue
        dates_list = code_dates[code]

        if ENTRY_AT_CLOSE:
            # X2: T 시간외 매수 가정
            if sig_date not in dates_list:
                continue
            entry_date = sig_date
            sig_close = price_map.get((code, sig_date), {}).get("close")
            if not sig_close or sig_close <= 0:
                continue
            entry_p = sig_close * (1 + ENTRY_SLIPPAGE_PCT / 100)
            idx = dates_list.index(sig_date)
            exit_idx = idx + holding - 1  # T+(holding-1) 종가
        else:
            # 원본: T+1 시가 매수
            next_dates = [d for d in dates_list if d > sig_date]
            if not next_dates:
                continue
            entry_date = next_dates[0]
            entry_p = price_map.get((code, entry_date), {}).get("open")
            if not entry_p or entry_p <= 0:
                continue
            idx = dates_list.index(entry_date)
            exit_idx = idx + holding - 1  # 진입일 포함 holding일째 종가
        if exit_idx >= len(dates_list):
            # 아직 청산 시점 도달 못함 — 미실현 보유 중
            exit_date = None
            exit_p = None
            net_pct = None
        else:
            exit_date = dates_list[exit_idx]
            exit_p = price_map.get((code, exit_date), {}).get("close")
            if not exit_p or exit_p <= 0:
                exit_date = None
                exit_p = None
                net_pct = None
            else:
                # 보유 중 기업행위(액면분할 등) 발생 시 무수정주가 왜곡 → 제외
                if _trade_has_corporate_action(ca_map, code, entry_date, exit_date):
                    n_ca_skipped += 1
                    continue
                gross_pct = (exit_p / entry_p - 1) * 100
                net_pct = gross_pct - COST_PCT

        trades.append({
            "code": code,
            "name": sig.get("name", ""),
            "signal_date": sig_date,
            "strategy": str(sig.get("strategy", "high_52w_filt")),
            "holding_days": holding,
            "entry_date": entry_date,
            "entry_price": float(entry_p),
            "exit_date": exit_date,
            "exit_price": float(exit_p) if exit_p else None,
            "net_pct": float(net_pct) if net_pct is not None else None,
            "status": "closed" if exit_date else "open",
        })

    # 4) 출력 — closed vs open 분리
    tdf = pd.DataFrame(trades)
    closed = tdf[tdf["status"] == "closed"].copy()
    open_pos = tdf[tdf["status"] == "open"].copy()

    print(f"\n[positions] 누적 매매: {len(tdf)}, 청산 {len(closed)}, 보유 {len(open_pos)}")
    if n_ca_skipped:
        print(f"  (기업행위 포함 매매 {n_ca_skipped}건 통계 제외)")
    # 전략별 현황
    if "strategy" in tdf.columns:
        for strat in tdf["strategy"].dropna().unique():
            g = tdf[tdf["strategy"] == strat]
            gc = g[g["status"] == "closed"]
            go = g[g["status"] == "open"]
            print(f"  [{strat}]  청산 {len(gc)}건  보유 {len(go)}건")

    # 5) 청산 매매 손익
    if not closed.empty:
        win = (closed["net_pct"] > 0).sum()
        total_n = len(closed)
        win_rate = win / total_n * 100
        avg_net = closed["net_pct"].mean()
        cum_pct = closed["net_pct"].sum()
        print(f"\n[closed] 청산 {total_n} 건  승률 {win_rate:.1f}%  avg_net {avg_net:+.2f}%  누적합 {cum_pct:+.1f}%")

        # 전략별 손익 분리
        if "strategy" in closed.columns:
            print(f"\n  {'전략':<18}  {'건수':>4}  {'승률':>6}  {'avg_net':>8}  {'avg_win':>8}  {'avg_loss':>9}")
            print(f"  {'─'*60}")
            for strat in ["high_52w_filt", "rsi_reversal", "rsi_vol"]:
                g = closed[closed["strategy"] == strat]
                if len(g) == 0:
                    continue
                gw = g[g["net_pct"] > 0]["net_pct"]
                gl = g[g["net_pct"] <= 0]["net_pct"]
                wr = len(gw) / len(g) * 100
                an = g["net_pct"].mean()
                aw = gw.mean() if len(gw) else 0.0
                al = gl.mean() if len(gl) else 0.0
                neg = "❌" if an < 0 else "  "
                print(f"  {neg}{strat:<16}  {len(g):>4}  {wr:>5.1f}%  "
                      f"{an:>+7.2f}%  {aw:>+7.2f}%  {al:>+8.2f}%")

    # 6) 자본 시뮬 (closed 매매만, simulate_capital 호환 형식)
    if not closed.empty:
        sim_trades = [
            StrategyTrade(
                strategy=str(r.get("strategy", "paper")),
                code=r["code"],
                entry_date=r["entry_date"],
                entry_price=r["entry_price"],
                exit_date=r["exit_date"],
                exit_price=r["exit_price"],
                holding_days=int(r.get("holding_days", HOLDING_DAYS_DEFAULT)),
                gross_pct=(r["exit_price"] / r["entry_price"] - 1) * 100,
                cost_pct=COST_PCT,
                net_pct=r["net_pct"],
                exit_reason="hold_exit",
            )
            for _, r in closed.iterrows()
        ]
        # MTM 평가 — 보유 중 평가손익을 equity 에 반영 (MDD/Sharpe 정확)
        price_lookup, trading_dates = build_price_lookup(df)
        cap = simulate_capital(sim_trades,
                                initial_capital=INITIAL_CAPITAL,
                                max_concurrent=MAX_CONCURRENT,
                                price_map=price_lookup,
                                trading_dates=trading_dates)
        if cap:
            print(f"\n[capital] 자본 모델링 결과")
            print(f"  초기:        {cap['initial']:,} 원")
            print(f"  최종:        {int(cap['final']):,} 원")
            print(f"  총 수익률:   {cap['total_ret_pct']:+.2f}%")
            print(f"  CAGR:        {cap['cagr_pct']:+.2f}%/년")
            print(f"  Real MDD:    {cap['real_mdd_pct']:+.2f}%")
            print(f"  Real Sharpe: {cap['real_sharpe']:+.2f}")
            print(f"  Skipped:     {cap['skipped']} 신호 (슬롯 부족)")

    # 7) 보유 중 포지션 (미실현)
    if not open_pos.empty:
        print(f"\n[open positions] {len(open_pos)} 건 (미실현)")
        cols = ["code", "name", "signal_date", "entry_date", "entry_price"]
        print(open_pos[cols].head(20).to_string(index=False))

    # [fix] 이전의 `skipped_by_ai` 출력은 변수 미정의로 USE_AI=True 시 NameError 발생 → 제거.
    # AI 는 dashboard 표시 전용 (매매 결정 영향 없음, 사용자 합의).

if __name__ == "__main__":
    main()