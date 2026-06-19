"""
자본 사이즈 + 동시보유 모델링.

매매당 평균 수익률 → 진짜 자본 수익률로 변환.

설계:
- initial_capital: 시작 자본
- max_concurrent: 매일 최대 보유 종목 수
- 매매 진입 시점에 가용 현금 / 빈 슬롯 수 만큼 자본 분배
- 시그널 N > 빈 슬롯이면 entry_date 순서로 처리 (먼저 온 거 우선, 초과분 skip)

[2026-06-11 교정]
- min_gross_pct 기본값 -30 → None.
  이전의 -30% 일괄 컷오프는 "그 이하는 99% 액면분할" 가정이었으나,
  40영업일 보유에서는 -60~-75% 진짜 폭락이 흔해 실손실 11.7%(186건)가
  성과 통계에서 삭제되고 있었음 (CAGR +156% → 실제 +96%).
  액면분할 왜곡은 strategies/_swing_base.find_corporate_action_dates 가
  KRX 등락률 대조 방식으로 해당 매매만 정확히 제외함.
- price_map/trading_dates 를 주면 보유 포지션을 일별 종가로 평가(mark-to-market).
  이전에는 진입원가로 평가해 보유 중 평가손실이 MDD 에 안 잡혔고(과소),
  Sharpe 도 entry/exit 일자만의 불규칙 곡선에 √252 를 곱해 과대했음.

산출:
  - total_return_pct (총 수익률)
  - cagr_pct (연환산 수익률)
  - capital_mdd_pct (자본 기준 MDD — MTM 모드면 평가손 반영)
  - capital_sharpe (일별 자본 수익률 기준)
  - n_skipped_signals (슬롯 부족으로 놓친 신호)
  - util_pct (자본 가동률 평균)
"""

import math
import pandas as pd


def simulate_capital(trades, initial_capital=10_000_000, max_concurrent=10,
                     min_gross_pct=None, max_gross_pct=None,
                     price_map=None, trading_dates=None):
    """
    Args:
        trades: list[StrategyTrade]
        initial_capital: 시작 자본 (원)
        max_concurrent: 매일 최대 보유 종목
        min_gross_pct / max_gross_pct: gross_pct 범위 밖 매매 제외.
            기본 None (제외 안 함). 액면분할 왜곡은 백테스트 단계의
            CA filter 가 처리하므로 보통 쓸 필요 없음.
        price_map: dict[(code, date_str)] -> close. 주면 MTM 평가.
        trading_dates: sorted list[date_str(YYYYMMDD)]. price_map 과 세트.

    Returns: dict with metrics, 또는 None (매매 없음)
    """
    if not trades:
        return None

    n_before = len(trades)
    if min_gross_pct is not None:
        trades = [t for t in trades if t.gross_pct >= min_gross_pct]
    if max_gross_pct is not None:
        trades = [t for t in trades if t.gross_pct <= max_gross_pct]
    n_dropped = n_before - len(trades)
    if n_dropped > 0:
        print(f"  [cutoff] gross_pct 범위 [{min_gross_pct}, {max_gross_pct}]% "
              f"밖 매매 {n_dropped}건 제외")
    if not trades:
        return None

    df = pd.DataFrame([{
        "entry_date": str(t.entry_date),
        "exit_date":  str(t.exit_date),
        "code":       str(t.code),
        "entry_price": float(t.entry_price),
        "net_pct":    t.net_pct,
        "score":      float(getattr(t, "score", 0.0) or 0.0),
    } for t in trades])
    df = df.dropna(subset=["entry_date", "exit_date"])
    df = df[df["entry_date"].str.len() == 8]
    df = df.sort_values("entry_date").reset_index(drop=True)
    if df.empty:
        return None

    use_mtm = price_map is not None and trading_dates is not None
    first_entry, last_exit = df["entry_date"].min(), df["exit_date"].max()

    if use_mtm:
        all_dates = [d for d in trading_dates if first_entry <= d <= last_exit]
    else:
        all_dates = sorted(set(df["entry_date"]) | set(df["exit_date"]))

    cash = float(initial_capital)
    positions = []  # {code, exit_date, invested_value, entry_price, net_pct}
    equity_curve = []
    n_skipped = 0
    util_sum = 0.0
    days_recorded = 0

    for date in all_dates:
        # 1. 오늘 청산할 포지션
        remaining = []
        for p in positions:
            if p["exit_date"] <= date:
                cash += p["invested_value"] * (1 + p["net_pct"] / 100)
            else:
                remaining.append(p)
        positions = remaining

        # 2. 오늘 새 진입 — 슬롯 부족 시 score(신호일 거래대금) 큰 순으로 배정.
        #    이전엔 CSV 도착 순서(사실상 임의)로 배정돼 결과가 운에 민감했음.
        todays = df[df["entry_date"] == date].sort_values("score", ascending=False)
        for _, row in todays.iterrows():
            slots_left = max_concurrent - len(positions)
            if slots_left <= 0:
                n_skipped += 1
                continue
            invest = cash / slots_left
            if invest <= 0:
                n_skipped += 1
                continue
            positions.append({
                "code": row["code"],
                "exit_date": row["exit_date"],
                "invested_value": invest,
                "entry_price": row["entry_price"],
                "net_pct": row["net_pct"],
            })
            cash -= invest

        # 3. 일별 equity 기록
        if use_mtm:
            invested = 0.0
            for p in positions:
                close = price_map.get((p["code"], date))
                if close and p["entry_price"] > 0:
                    invested += p["invested_value"] * (close / p["entry_price"])
                else:
                    invested += p["invested_value"]   # 가격 없으면 원가 평가
        else:
            invested = sum(p["invested_value"] for p in positions)
        equity = cash + invested
        equity_curve.append((date, equity))
        if equity > 0:
            util_sum += invested / equity
            days_recorded += 1

    eq = pd.DataFrame(equity_curve, columns=["date", "equity"]).sort_values("date")
    eq["daily_return"] = eq["equity"].pct_change()
    final_eq = float(eq["equity"].iloc[-1])

    d0 = pd.to_datetime(eq["date"].iloc[0], format="%Y%m%d")
    d1 = pd.to_datetime(eq["date"].iloc[-1], format="%Y%m%d")
    days_span = (d1 - d0).days
    years = max(days_span / 365.25, 1 / 252)

    total_return = (final_eq / initial_capital - 1) * 100
    cagr = ((final_eq / initial_capital) ** (1 / years) - 1) * 100

    peak = eq["equity"].cummax()
    dd = (eq["equity"] - peak) / peak * 100
    capital_mdd = float(dd.min())

    daily = eq["daily_return"].dropna()
    if use_mtm and daily.std() > 0:
        sharpe = daily.mean() / daily.std() * math.sqrt(252)
    elif daily.std() > 0:
        # 비-MTM: 곡선이 entry/exit 일자만 있어 √252 환산이 부정확 — 참고치
        sharpe = daily.mean() / daily.std() * math.sqrt(252)
    else:
        sharpe = 0.0
    util_avg = (util_sum / days_recorded * 100) if days_recorded else 0.0

    return {
        "initial":         initial_capital,
        "final":           round(final_eq, 0),
        "total_ret_pct":   round(total_return, 2),
        "cagr_pct":        round(cagr, 2),
        "real_mdd_pct":    round(capital_mdd, 2),
        "real_sharpe":     round(float(sharpe), 2),
        "util_avg_pct":    round(util_avg, 1),
        "skipped":         n_skipped,
        "n_trades":        len(df),
        "years":           round(years, 2),
        "mtm":             use_mtm,
    }


def build_price_lookup(df):
    """macro_data DataFrame → simulate_capital 용 (price_map, trading_dates).

    df: code/date/close 컬럼 보유 (date YYYYMMDD str).
    """
    d = df[["code", "date", "close"]].copy()
    d["code"] = d["code"].astype(str)
    d["date"] = d["date"].astype(str)
    price_map = {(c, dt): float(cl)
                 for c, dt, cl in zip(d["code"], d["date"], d["close"])
                 if cl and cl > 0}
    trading_dates = sorted(d["date"].unique().tolist())
    return price_map, trading_dates
