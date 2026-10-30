"""
스윙 전략 공통 헬퍼 — 진입가 = signal day+1 의 시가, 청산가 = N일 후 종가.

벡터화 형태로 일괄 처리해서 100배 빠름.
"""

import bisect
import numpy as np
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


_CA_CACHE = {}
# [2026-10-23] 긴 거래정지 뒤 재개 직후(정리매매 포함) 신호 제외 — 상장폐지 결정 → 정지 → 정리매매(7거래일, 가격제한 없음)
#   구간의 신호는 실매매에서 살 수 없거나 사면 안 되는 종목이고, 가격도 정상 발견이 아니다(예 024810 정리매매 중 +330%).
#   정지 사실은 그때 알 수 있는 정보라 미래참조가 아니다. 실매매(engine_signals)도 같은 규칙.
HALT_MIN_DAYS = 5          # 거래량 0 이 이만큼 이어지면 '긴 정지'
HALT_RESUME_SKIP_DAYS = 10  # 재개 후 이 거래일 동안의 신호는 제외


def _resume_dates(df):
    """긴 정지(거래량 0 이 HALT_MIN_DAYS 일 이상) 뒤 거래가 다시 시작된 날 → dict[code] -> sorted list[date]."""
    if "volume" not in df.columns:
        return {}
    d = df[["code", "date", "volume"]].sort_values(["code", "date"]).reset_index(drop=True)
    z = ~(d["volume"].fillna(0) > 0)
    grp = (~z).astype(int).groupby(d["code"]).cumsum()
    run = z.astype(int).groupby([d["code"], grp]).cumsum()
    prev = run.groupby(d["code"]).shift(1).fillna(0)
    m = (~z) & (prev >= HALT_MIN_DAYS)
    out = {}
    for code, g in d.loc[m, ["code", "date"]].groupby("code"):
        out[str(code)] = sorted(g["date"].astype(str).tolist())
    return out


def _ca_map_cached(df):
    """(기업행위일 map, 거래일 목록, 정지 재개일 map) — df 지문(행수·기간·종목수·종가합)으로 캐시해 전략마다 다시 계산하지 않게."""
    try:
        key = (len(df), str(df["date"].min()), str(df["date"].max()), int(df["code"].nunique()),
               round(float(df["close"].sum()), 2))
    except Exception:
        key = None
    if key is None or key not in _CA_CACHE:
        val = (find_corporate_action_dates(df), sorted(df["date"].astype(str).unique()), _resume_dates(df))
        if key is None:
            return val
        if len(_CA_CACHE) > 4:
            _CA_CACHE.clear()
        _CA_CACHE[key] = val
    return _CA_CACHE[key]


def halt_resume_recent(resume_map, dates, code, signal_date, days=None):
    """신호일까지 days 거래일 안에 '긴 정지 뒤 재개'가 있었는가(정리매매·재개 직후 가격 왜곡 구간)."""
    return ca_in_lookback(resume_map, dates, code, signal_date, HALT_RESUME_SKIP_DAYS if days is None else days)


def signal_lookback(s) -> int:
    """신호가 그 종목의 과거 몇 거래일 가격을 보는가(창 길이 최댓값). 시장 게이트(지수)는 빼고 본다.
    period_backtest.warmup_days 와 같은 규칙(그쪽은 여기에 시장 게이트·여유를 더한다)."""
    v = vars(s)
    keys = ("lookback", "baseline", "high_period", "slow", "slow_ma", "ma_period", "period",
            "bb_period", "ma_days", "accum_days", "window", "rsi_period", "k_period", "vol_ma_days",
            "n_days")
    w = max([int(v[k]) for k in keys if isinstance(v.get(k), (int, float)) and v.get(k)] or [0])
    if v.get("squeeze_window"):
        w = max(w, int(v["squeeze_window"]) + int(v.get("bb_period") or 20))
    cls = type(s).__name__
    if cls == "PullbackMaStrategy":
        w = max(w, 60 + int(v.get("ma60_slope_days") or 5))
    if cls == "MaRibbonStrategy":
        w = max(w, 120)
    if cls == "FiveDayMomentumStrategy":
        w = max(w, 5)
    return w or 260


def ca_in_lookback(ca_map, dates, code, signal_date, lookback):
    """신호일까지 lookback 거래일 창 (dates[i-lookback], signal_date] 에 기업행위일이 있는가."""
    lst = ca_map.get(str(code))
    if not lst:
        return False
    sd = str(signal_date)
    i = bisect.bisect_right(dates, sd) - 1
    lo = dates[max(0, i - int(lookback))] if i >= 0 else sd
    j = bisect.bisect_right(lst, lo)
    return j < len(lst) and lst[j] <= sd


def drop_ca_lookback_trades(trades, df, lookback):
    """매매 목록에서 ① 신호 창에 기업행위(분할·병합·감자)가 낀 매매 ② 긴 정지 재개 직후(정리매매 포함) 신호를 뺀다.
    신호일 = 진입일 직전 거래일(엔진 진입 규칙 entry_lag=1)."""
    if "change_pct" not in getattr(df, "columns", []):
        return trades
    ca_map, dates, resume = _ca_map_cached(df)
    if not ca_map and not resume:
        return trades
    out = []
    for t in trades:
        ed = str(t.entry_date)
        k = bisect.bisect_left(dates, ed) - 1          # 진입일 직전 거래일 = 신호일
        if k >= 0 and (ca_in_lookback(ca_map, dates, t.code, dates[k], lookback)
                       or halt_resume_recent(resume, dates, t.code, dates[k])):
            continue
        out.append(t)
    return out


def _trade_has_corporate_action(ca_map, code, entry_date, exit_date):
    """보유 기간 (entry_date, exit_date] 에 기업행위 발생일이 있는지."""
    dates = ca_map.get(str(code))
    if not dates:
        return False
    i = bisect.bisect_right(dates, str(entry_date))
    return i < len(dates) and dates[i] <= str(exit_date)


# ── [2026-10-23] 실매매와 맞추는 공통 규칙 ───────────────────────────────────────
UPPER_LIMIT_SKIP_PCT = 29.5     # 진입일 시가가 전일 종가 대비 이 이상(사실상 상한가 시초가)이면 못 산 것으로 본다


def trade_cost_pct(costs, exit_date):
    """왕복 비용(%) — costs 에 tax_by_year 가 있으면 매도 연도의 실제 거래세, 없으면 total_pct(구형 dict 호환)."""
    tby = costs.get("tax_by_year") if isinstance(costs, dict) else None
    if not tby:
        return float(costs["total_pct"])
    tax = tby.get(str(exit_date)[:4], costs.get("tax_pct", 0.20))
    return 2 * float(costs.get("fee_pct", 0.015)) + 2 * float(costs.get("slip_pct", 0.05)) + float(tax)


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
    df["_entry_prev_close"] = df.groupby("code")["close"].shift(-(entry_lag - 1)) if entry_lag >= 1 else df["close"]

    # [2026-10-23] 보유 중 상장폐지·장기 거래정지: 종전엔 청산가가 없어 매매를 통째로 버렸다(52주 신고가 47건, 중앙값 −58%
    # — 가장 나쁜 결과만 사라지는 낙관 편향). 그 종목의 데이터가 자료 끝보다 먼저 끝났거나 청산일 종가가 0(정지)이면,
    # 진입 뒤 마지막으로 거래된 종가로 청산한 것으로 본다. 자료 끝까지 이어지는 종목(아직 보유 중)은 종전대로 제외.
    data_last = str(df["date"].max())
    _need = df[sig_col].fillna(False).astype(bool) & df["entry_price"].notna() & (df["entry_price"] > 0) & \
        ~(df["exit_price"].notna() & (df["exit_price"] > 0))
    if _need.any():
        need_arr = _need.to_numpy()
        for code, g in df[df["code"].isin(df.loc[_need, "code"].unique())].groupby("code"):
            idx = g.index.to_numpy()
            ds = g["date"].astype(str).to_numpy()
            cl = g["close"].to_numpy(dtype=float)
            ended = ds[-1] < data_last
            for k in np.flatnonzero(need_arr[df.index.get_indexer(idx)]):
                i = idx[k]
                e = k + entry_lag
                x = e + max(holding_days, 1) - 1
                if e >= len(idx):
                    continue
                if x < len(idx) and not ended and cl[x] > 0:
                    continue
                if x < len(idx) or ended:
                    hi = min(x, len(idx) - 1)
                    pos = [j for j in range(e, hi + 1) if cl[j] > 0]
                    if pos:
                        j = pos[-1]
                        df.at[i, "exit_price"] = cl[j] * (1 + exit_slippage_pct / 100)
                        df.at[i, "exit_date_next"] = ds[j]

    sig = df[df[sig_col] &
             df["entry_price"].notna() &
             df["exit_price"].notna() &
             (df["entry_price"] > 0) &
             (df["exit_price"] > 0)].copy()
    # [2026-10-23] 상한가 시초가 — 실매매(09:00 시장가)에선 대부분 체결되지 않는다 → 못 산 것으로 보고 제외
    if entry_col == "open" and UPPER_LIMIT_SKIP_PCT is not None:
        _pc = sig["_entry_prev_close"]
        sig = sig[~((_pc > 0) & (sig["entry_price"] >= _pc * (1 + UPPER_LIMIT_SKIP_PCT / 100)))].copy()

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
    sig["cost_pct"] = [trade_cost_pct(costs, x) for x in sig["exit_date_next"]]   # [2026-10-23] 연도별 실제 거래세
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
                             time_stop_pct=None,
                             take_profit_pct=None, tp_fill="high"):
    """
    조건부 청산 백테스트 — 손절 + trailing stop + 시간 조건부 + 익절 + 만기 청산.

      take_profit_pct: 진입가 대비 익절 목표 (+2.0 = +2%). None 이면 미적용. (2026-09-08 신설)
        라이브 kiwoom 의 '매수 후 익절 지정가'와 같은 기제 — 진입 당일부터 체크.
      tp_fill: "high" = 일중 고가가 목표에 닿으면 목표가 체결(갭상승으로 시가가 목표 위면 시가).
               "close" = 종가가 목표 이상일 때 종가 체결(보수적 감도 검사용).
      청산 우선순위: stop_loss(일중 low) > take_profit(일중 high) > trailing(종가) > time_stop > 만기.
      같은 날 손절·익절이 둘 다 닿으면 손절을 먼저 본다(보수적).

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
    data_last = str(df["date"].max())

    trades = []
    n_ca_skipped = 0

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
        _pc = float(g.iloc[entry_idx - 1]["close"] or 0) if entry_idx >= 1 else 0.0
        if UPPER_LIMIT_SKIP_PCT is not None and _pc > 0 and entry_p >= _pc * (1 + UPPER_LIMIT_SKIP_PCT / 100):
            continue                                   # [2026-10-23] 상한가 시초가 — 못 산 것으로 본다
        entry_date = g.iloc[entry_idx]["date"]

        # [2026-10-23] 만기가 자료 끝을 넘는데 종목 자료가 자료 끝까지 이어지면 = 아직 보유 중 → 제외(벡터판과 같은 규칙).
        #              종목 자료가 먼저 끝났으면(상장폐지·장기정지) 마지막 거래일 종가로 청산한 것으로 본다.
        if entry_idx + holding_days - 1 >= len(g) and str(g.iloc[-1]["date"]) >= data_last:
            continue
        max_exit_idx = min(entry_idx + holding_days - 1, len(g) - 1)
        if max_exit_idx <= entry_idx:
            continue

        peak = float(entry_p)
        peak_activated = False
        max_pct_so_far = 0.0   # 보유 중 최고 손익률 (high 기준)
        stop_price = entry_p * (1 + stop_loss_pct / 100) if stop_loss_pct is not None else None
        tp_price = entry_p * (1 + take_profit_pct / 100) if take_profit_pct is not None else None

        exit_idx = max_exit_idx
        exit_p = float(g.iloc[exit_idx]["close"])
        exit_reason = "hold_exit"
        if not exit_p > 0:                             # [2026-10-23] 만기일이 정지(종가 0)면 마지막 거래 종가로
            _pos = [j for j in range(entry_idx, max_exit_idx + 1) if float(g.iloc[j]["close"] or 0) > 0]
            if _pos:
                exit_idx = _pos[-1]
                exit_p = float(g.iloc[exit_idx]["close"])

        # 0. 진입 당일 익절 — 시가 진입 직후 걸어둔 지정가가 당일 고가에 닿는 경우
        #    (진입가=시가라 '시가가 목표 위' 갭 케이스는 당일엔 없다 → 목표가 체결)
        _loop_start = entry_idx + 1
        if tp_price is not None:
            _r0 = g.iloc[entry_idx]
            _h0 = float(_r0["high"] or 0)
            _c0 = float(_r0["close"] or 0)
            _hit0 = (_c0 >= tp_price) if tp_fill == "close" else (_h0 > 0 and _h0 >= tp_price)
            if _hit0:
                exit_idx = entry_idx
                exit_p = _c0 if tp_fill == "close" else tp_price
                exit_reason = "take_profit"
                _loop_start = max_exit_idx + 1          # 일별 루프 건너뜀

        # 보유 기간 일별 검사 (entry 다음날부터 만기까지)
        for i in range(_loop_start, max_exit_idx + 1):
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

            # 1b. 익절 지정가 (2026-09-08) — 손절 다음 우선순위(같은 날 둘 다면 손절 우선, 보수적)
            if tp_price is not None:
                if tp_fill == "close":
                    if day_close >= tp_price:
                        exit_idx = i
                        exit_p = day_close
                        exit_reason = "take_profit"
                        break
                elif day_high > 0 and day_high >= tp_price:
                    day_open = float(row["open"] or 0)
                    # 갭상승으로 시가가 이미 목표 위면 지정가 매도는 시가에 체결(유리한 쪽)
                    exit_idx = i
                    exit_p = max(tp_price, day_open) if day_open > 0 else tp_price
                    exit_reason = "take_profit"
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
        cost_pct = trade_cost_pct(costs, exit_date)    # [2026-10-23] 연도별 실제 거래세
        net_pct = gross_pct - cost_pct

        trades.append(StrategyTrade(
            strategy=strategy_name,
            code=str(code),
            entry_date=str(entry_date),
            entry_price=float(entry_p),
            exit_date=str(exit_date),      # [2026-09-08 fix] 누락돼 있어 이 함수가 통째로 TypeError — 손절 백테스트 전부 불능이었음
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
