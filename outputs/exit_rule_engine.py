"""
청산 시점 결정 엔진 — 미국장 영향 반영.

룰:
  - 보유 일수 < 40: 청산 안 함 (룰 v2)
  - 보유 일수 >= 40: 청산 결정. 미국 proxy 등락률 따라:
      * proxy <= -2.0%: 09:00 시가 매도 (강하게)
      * -2.0% < proxy <= 0%: 09:10 매도 (지연)
      * 0% < proxy < +1.0%: 정상 종가 매도 (15:30)
      * +1.0% 이상: 종가 매도 (강세장 막판 추가 상승 기대)

섹터 매핑 sector_mapping.csv:
  code → us_proxy (SOX/NDX/SPX/XBI 등)
  매핑 없는 종목은 default SPX 사용.
"""

import os
import pandas as pd

import config

_MACRO_IND_CSV = os.path.join(os.path.dirname(__file__), "macro_data", "indicators.csv")


def load_us_market() -> dict:
    """macro_data/indicators.csv 에서 미국 지수 전일 대비 등락률 계산.

    us_market_collector.py 는 제거됨 — 미국 데이터는 macro_collector 가 수집해
    indicators.csv 에 저장. SP500→SPX, NASDAQ→NDX, SOX→SOX 로 매핑.
    """
    if not os.path.exists(_MACRO_IND_CSV):
        return {}
    try:
        df = pd.read_csv(_MACRO_IND_CSV)
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date")
        w = df.pivot_table(index="date", columns="indicator", values="close", aggfunc="last")
        if len(w) < 2:
            return {}
        latest = w.iloc[-1]
        prev   = w.iloc[-2]
        result = {}
        mapping = {"SP500": "SPX", "NASDAQ": "NDX", "SOX": "SOX",
                   "KOSPI": "KOSPI", "VIX": "VIX", "KRW_USD": "KRW_USD"}
        for col, alias in mapping.items():
            if col in w.columns and prev[col] and prev[col] != 0:
                result[alias] = float((latest[col] - prev[col]) / prev[col] * 100)
        return result
    except Exception as e:
        print(f"[exit_rule_engine] US 지수 로드 실패: {e}")
        return {}

SECTOR_CSV = "./sector_mapping.csv"
HOLDING_DAYS = 40


def load_sector_map():
    """code → us_proxy dict."""
    if not os.path.exists(SECTOR_CSV):
        return {}
    df = pd.read_csv(SECTOR_CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(6)
    return dict(zip(df["code"], df["us_proxy"]))


def decide_exit(open_position, us_market=None, sector_map=None,
                default_proxy="SPX"):
    """
    Args:
      open_position: dict with keys (code, holding_so_far, ...)
      us_market: dict {symbol: change_pct}, 없으면 자동 로드
      sector_map: dict {code: proxy_symbol}, 없으면 자동 로드

    Returns: dict
      {exit_today, exit_timing, exit_price_source, reason}
        exit_timing: "market_open" | "09:10" | "regular_close"
        exit_price_source: "open" | "morning_avg" | "close"
    """
    if us_market is None:
        us_market = load_us_market()
    if sector_map is None:
        sector_map = load_sector_map()

    code = open_position["code"]
    holding = open_position.get("holding_so_far", 0)

    # 보유 일수 < 40 → 청산 안 함
    if holding < HOLDING_DAYS:
        return {
            "exit_today": False,
            "exit_timing": None,
            "exit_price_source": None,
            "reason": f"보유 {holding}/{HOLDING_DAYS}일",
        }

    # 섹터 proxy 결정
    proxy = sector_map.get(code, default_proxy)
    us_change = us_market.get(proxy)

    if us_change is None:
        # 데이터 없으면 보수적으로 정상 청산
        return {
            "exit_today": True,
            "exit_timing": "regular_close",
            "exit_price_source": "close",
            "reason": f"미국 데이터 없음 ({proxy}), 정상 청산",
        }

    if us_change <= -2.0:
        return {
            "exit_today": True,
            "exit_timing": "market_open",
            "exit_price_source": "open",
            "reason": f"{proxy} {us_change:+.2f}% 급락 — 09:00 시가 매도",
        }
    elif us_change <= 0:
        return {
            "exit_today": True,
            "exit_timing": "09:10",
            "exit_price_source": "morning_avg",  # 09:00~09:10 평균 (단순화)
            "reason": f"{proxy} {us_change:+.2f}% 지연 매도 (09:10)",
        }
    elif us_change < 1.0:
        return {
            "exit_today": True,
            "exit_timing": "regular_close",
            "exit_price_source": "close",
            "reason": f"{proxy} {us_change:+.2f}% 정상 청산",
        }
    else:
        return {
            "exit_today": True,
            "exit_timing": "regular_close",
            "exit_price_source": "close",
            "reason": f"{proxy} {us_change:+.2f}% 강세 (종가 매도)",
        }


def main_diag():
    """진단 — 현재 보유 포지션의 청산 결정 출력."""
    print("=== 청산 룰 엔진 진단 ===\n")
    us = load_us_market()
    sm = load_sector_map()

    print(f"[미국 시장 최신]")
    for sym, chg in sorted(us.items()):
        print(f"  {sym}: {chg:+.2f}%")
    print(f"\n[섹터 매핑 등록]: {len(sm)} 종목\n")

    # paper_signals.csv 의 보유 포지션 진단
    if os.path.exists("./paper_signals.csv"):
        from strategies.daily_loader import load_macro_daily
        df = load_macro_daily()
        code_dates = df.groupby("code")[["date"]].apply(
            lambda x: sorted(x["date"].astype(str).tolist())).to_dict()
        signals = pd.read_csv("./paper_signals.csv", dtype={"code": str})
        signals["code"] = signals["code"].astype(str).str.zfill(6)
        signals["signal_date"] = signals["signal_date"].astype(str)

        print("[보유 포지션 청산 결정]")
        n_open = 0
        for _, sig in signals.iterrows():
            code = sig["code"]
            sig_date = sig["signal_date"]
            if code not in code_dates:
                continue
            dates_list = code_dates[code]
            next_dates = [d for d in dates_list if d > sig_date]
            if not next_dates:
                continue
            entry_date = next_dates[0]
            try:
                start_idx = dates_list.index(entry_date)
            except ValueError:
                continue
            holding_so_far = len(dates_list) - start_idx - 1
            if holding_so_far >= HOLDING_DAYS - 5 and holding_so_far < HOLDING_DAYS + 5:
                pos = {"code": code, "holding_so_far": holding_so_far}
                d = decide_exit(pos, us, sm)
                marker = "🔴" if d["exit_today"] else "  "
                print(f"  {marker} {code} 보유 {holding_so_far}일 → {d['reason']}")
                n_open += 1
        if n_open == 0:
            print("  (40일 근접 보유 포지션 없음)")


if __name__ == "__main__":
    main_diag()
