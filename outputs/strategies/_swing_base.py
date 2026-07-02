"""
스윙 전략 공통 헬퍼 — 진입가 = signal day+1 의 시가, 청산가 = N일 후 종가.

벡터화 형태로 일괄 처리해서 100배 빠름.
"""

import bisect
import pandas as pd
from .base import StrategyTrade


def find_corporate_action_dates(df):
    """액면분할/병합/감자/권리락 발생일 감지.

    원리: KRX 등락률(change_pct)은 기준가 조정(기업행위 반영) 기준이므로,
    원시 종가 비율(close/prev_close)과 10%p 이상 괴리가 나면 그날 기업행위 발생.
    (정상일은 두 값이 일치. 상하한가 ±30% 이내라 잘못 걸릴 일 없음)

    Returns: dict[code] -> sorted list[date(YYYYMMDD str)]
    """
    d = df.sort_values(["code", "date"]).reset_index(drop=True)
    prev_close = d.groupby("code")["close"].shift(1)
    raw_ret = d["close"] / prev_close - 1
    krx_ret = d["change_pct"] / 100.0
    # close=0 행(거래정지 등)은 가격이 아니라 데이터 공백 — 오탐 방지를 위해 제외
    valid = (d["close"] > 0) & (prev_close > 0)
    mismatch = ((raw_ret - krx_ret).abs() > 0.10) & valid
    ca = d.loc[mismatch, ["code", "date"]]
    out = {}
    for code, g in ca.groupby("code"):
        out[str(code)] = sorted(g["date"].astype(str).tolist())
    return out


def _trade_has_corporate_action(ca_map, code, entry_date, exit_date):
    """보유 기간 (entry_date, exit_date] 에 기업행위 발생일이 있는지."""
    dates = ca_map.get(str(code))
    if not dates:
        return False
    i = bisect.bisect_right(dates, str(entry_date))
    return i < len(dates) and dates[i] <= str(exit_date)


def _add_market_gate(df, ma_days=60):
    """시장 강세 게이트 컬럼 추가.

    전 종목 평균 change_pct 의 ma_days 이동평균 > 0 이면 mkt_strong=True.
    이 컬럼을 signal 조건에 AND 해서 약세장 진입을 막는다.
    """
    mkt = df.groupby("date")["change_pct"].mean()
    mkt_ma = mkt.rolling(ma_days, min_periods=ma_days).mean()
    mkt_dict = (mkt_ma > 0).to_dict()
    df["mkt_strong"] = df["date"].map(mkt_dict).fillna(False)
    return df


def _make_trades_for_signals(df_with_sig, holding_days, strategy_name,
                              costs, sig_col="signal", entry_lag=1,
                              entry_col="open", slippage_pct=0.0,
                              gap_filter_pct=None,
                              exit_slippage_pct=0.0):
    """
    공통 로직: signal 행 다음날 시가 매수, N일 후 종가 매도.

    df_with_sig: code/date 정렬 + sig_col(bool) 컬럼 보유. 행마다 한 종목-일.
    entry_lag: 시그널 발생 후 N일째 시가 진입 (보통 1 = 다음 영업일).
    holding_days: 진입 후 N일째 종가 청산 (1 = 진입 당일은 close).
    entry_col: 진입가 컬럼 ("open" 기본 / "close" = 시간외 종가 가정).
    slippage_pct: 진입가에 곱해질 슬리피지 (+2 = +2% 비싸게 매수).
    gap_filter_pct: T+1 시가가 T 종가 대비 ±X% 밖이면 매매 스킵 (None=미적용).
        → 시간외 매수 백업 룰: 갭상승/갭하락 시 거래 포기.
    """
    df = df_with_sig.copy()

    # 코드별로 lag 적용
    df["entry_price"] = df.groupby("code")[entry_col].shift(-entry_lag) * (1 + slippage_pct / 100)
    df["entry_date_next"] = df.groupby("code")["date"].shift(-entry_lag)
    # 갭 필터용: T+1 시가 (entry_lag=0/1 무관 다음날 시가)
    df["next_open_for_gap"] = df.groupby("code")["open"].shift(-1)
    df["exit_price"] = df.groupby("code")["close"].shift(-(entry_lag + holding_days - 1)
                                                          if holding_days >= 1
                                                          else -entry_lag) * (1 + exit_slippage_pct / 100)
    df["exit_date_next"] = df.groupby("code")["date"].shift(-(entry_lag + holding_days - 1)
                                                              if holding_days >= 1
                                                              else -entry_lag)

    sig = df[df[sig_col] &
             df["entry_price"].notna() &
             df["exit_price"].notna() &
             (df["entry_price"] > 0) &
             (df["exit_price"] > 0)].copy()

    # 갭 필터: T+1 시가 / T close 비교 (entry_at_close=True 시에만 의미 있음)
    if gap_filter_pct is not None and entry_col == "close" and entry_lag == 0:
        # T close = df["close"], T+1 시가 = next_open_for_gap
        gap_pct = (sig["next_open_for_gap"] / sig["close"] - 1) * 100
        sig = sig[gap_pct.abs() <= gap_filter_pct].copy()

    # 기업행위(액면분할/병합 등) 포함 매매 제외 — 가격 데이터가 무수정주가라
    # 보유 중 기업행위가 끼면 손익이 왜곡됨. (진짜 폭락/폭등은 그대로 보존)
    # ※ sig 가 비었으면 스킵 — pandas 는 빈 df.apply(axis=1)가 object 빈 Series 를 반환해
    #   sig[keep] 이 '컬럼 선택'으로 오동작(컬럼 전멸 → 이후 KeyError 'exit_price').
    #   h52w_short/cred_dec 신호 0건에서 실제 발생(2026-07-02 수정).
    ca_map = find_corporate_action_dates(df)
    if ca_map and len(sig):
        before = len(sig)
        keep = sig.apply(lambda r: not _trade_has_corporate_action(
            ca_map, r["code"], r["entry_date_next"], r["exit_date_next"]), axis=1)
        sig = sig[keep].copy()
        n_ca = before - len(sig)
        if n_ca:
            print(f"  [CA filter] {strategy_name}: 기업행위 포함 매매 {n_ca}건 제외")

    sig["gross_pct"] = (sig["exit_price"] / sig["entry_price"] - 1) * 100
    sig["cost_pct"] = costs["total_pct"]
    sig["net_pct"] = sig["gross_pct"] - sig["cost_pct"]

    trades = [
        StrategyTrade(
            strategy=strategy_name,
            code=str(r["code"]),
            entry_date=str(r["entry_date_next"]),
            entry_price=float(r["entry_price"]),
            exit_date=str(r["exit_date_next"]),
            exit_price=float(r["exit_price"]),
            holding_days=holding_days,
            gross_pct=float(r["gross_pct"]),
            cost_pct=float(r["cost_pct"]),
            net_pct=float(r["net_pct"]),
            exit_reason="hold_exit",
            score=float(r.get("trading_value", 0) or 0),  # 신호일 거래대금 = 슬롯 랭킹
        )
        for _, r in sig.iterrows()
    ]
    return trades


def _make_trades_with_stops(df_with_sig, holding_days, strategy_name,
                             costs, sig_col="signal", entry_lag=1,
                             stop_loss_pct=None, trailing_peak_pct=None,
                             trailing_activate_pct=10.0,
                             time_check_days=None, time_check_min_pct=None,
                             time_stop_pct=None):
    """
    조건부 청산 백테스트 — 손절 + trailing stop + 시간 조건부 + 만기 청산.

    Args:
      stop_loss_pct: 진입가 대비 (-15.0 = -15%). None 이면 미적용.
      trailing_peak_pct: peak 대비 (-15.0 = peak -15%). None 이면 미적용.
      trailing_activate_pct: trailing 활성화 threshold (+10.0 = +10% 도달 후).
      time_check_days: 보유 N일 후부터 시간 조건부 손절 체크 (None 미적용).
      time_check_min_pct: 보유 중 최고가 +X% 도달 못 했으면 트리거 (None 무시 → 조건 false).
      time_stop_pct: 현재 손익 <= X% 이면 트리거 (None 무시 → 조건 false).
        → time_check_days 이후 매일 체크. 두 조건 모두 만족 시 종가 청산.

    청산 우선순위: stop_loss (일중 low) > trailing_stop (종가) > time_stop (종가) > 만기.
    """
    df = df_with_sig.copy().sort_values(["code", "date"]).reset_index(drop=True)
    sig_df = df[df[sig_col] == True]
    df_by_code = {code: g.reset_index(drop=True) for code, g in df.groupby("code")}
    ca_map = find_corporate_action_dates(df)

    trades = []
    n_ca_skipped = 0
    cost_pct = costs["total_pct"]

    for _, sig_row in sig_df.iterrows():
        code = sig_row["code"]
        sig_date = sig_row["date"]
        g = df_by_code.get(code)
        if g is None:
            continue
        idx_arr = g.index[g["date"] == sig_date]
        if len(idx_arr) == 0:
            continue
        sig_idx = idx_arr[0]
        entry_idx = sig_idx + entry_lag
        if entry_idx >= len(g):
            continue
        entry_p = g.iloc[entry_idx]["open"]
        if not entry_p or entry_p <= 0:
            continue
        entry_date = g.iloc[entry_idx]["date"]

        max_exit_idx = min(entry_idx + holding_days - 1, len(g) - 1)
        if max_exit_idx <= entry_idx:
            continue

        peak = float(entry_p)
        peak_activated = False
        max_pct_so_far = 0.0   # 보유 중 최고 손익률 (high 기준)
        stop_price = entry_p * (1 + stop_loss_pct / 100) if stop_loss_pct is not None else None

        exit_idx = max_exit_idx
        exit_p = float(g.iloc[exit_idx]["close"])
        exit_reason = "hold_exit"

        # 보유 기간 일별 검사 (entry 다음날부터 만기까지)
        for i in range(entry_idx + 1, max_exit_idx + 1):
            row = g.iloc[i]
            day_high = float(row["high"] or 0)
            day_low = float(row["low"] or 0)
            day_close = float(row["close"] or 0)
            if day_close <= 0:
                continue

            # 1. 하드 손절 (일중 low)
            if stop_price is not None and day_low > 0 and day_low <= stop_price:
                exit_idx = i
                day_open = float(row["open"] or 0)
                # [fix] 갭하락으로 시가가 이미 손절가 아래면 손절가 체결은 불가능 — 시가 체결
                exit_p = min(stop_price, day_open) if day_open > 0 else stop_price
                exit_reason = "stop_loss"
                break

            # 2. peak 갱신 + max_pct 추적
            if day_high > peak:
                peak = day_high
                if not peak_activated and (peak / entry_p - 1) * 100 >= trailing_activate_pct:
                    peak_activated = True
            high_pct = (day_high / entry_p - 1) * 100 if day_high > 0 else None
            if high_pct is not None and high_pct > max_pct_so_far:
                max_pct_so_far = high_pct

            # 3. Trailing stop (종가 기준, activate 후만)
            if trailing_peak_pct is not None and peak_activated:
                trailing_stop = peak * (1 + trailing_peak_pct / 100)
                if day_close <= trailing_stop:
                    exit_idx = i
                    exit_p = day_close
                    exit_reason = "trailing_stop"
                    break

            # 4. 시간 조건부 손절 (종가 기준)
            if time_check_days is not None:
                days_held = i - entry_idx
                if days_held >= time_check_days:
                    current_pct = (day_close / entry_p - 1) * 100
                    cond_max = (time_check_min_pct is None) or (max_pct_so_far < time_check_min_pct)
                    cond_loss = (time_stop_pct is None) or (current_pct <= time_stop_pct)
                    if cond_max and cond_loss:
                        exit_idx = i
                        exit_p = day_close
                        exit_reason = "time_stop"
                        break

        exit_date = g.iloc[exit_idx]["date"]
        if not exit_p or exit_p <= 0:
            continue
        # 기업행위 포함 매매 제외 (무수정주가 왜곡 방어)
        if _trade_has_corporate_action(ca_map, code, entry_date, exit_date):
            n_ca_skipped += 1
            continue
        gross_pct = (exit_p / entry_p - 1) * 100
        net_pct = gross_pct - cost_pct

        trades.append(StrategyTrade(
            strategy=strategy_name,
            code=str(code),
            entry_date=str(entry_date),
            entry_price=float(entry_p),
            exit_price=float(exit_p),
            holding_days=exit_idx - entry_idx + 1,
            gross_pct=float(gross_pct),
            cost_pct=float(cost_pct),
            net_pct=float(net_pct),
            exit_reason=exit_reason,
            score=float(sig_row.get("trading_value", 0) or 0),
        ))

    if n_ca_skipped:
        print(f"  [CA filter] {strategy_name}: 기업행위 포함 매매 {n_ca_skipped}건 제외")
    return trades
