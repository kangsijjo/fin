# -*- coding: utf-8 -*-
"""
_make_trades_with_stops 청산 로직 회귀 — 2026-09-08

배경(실사고): StrategyTrade 에 exit_date 필드가 필수로 추가된 뒤 _make_trades_with_stops 의
StrategyTrade(...) 호출은 갱신되지 않아 **함수가 통째로 TypeError** 였다. 손절·트레일링을
쓰는 모든 백테스트가 불능이었는데, 116개 테스트 중 어느 것도 이 함수를 부르지 않아 몰랐다.
익절(take_profit_pct)을 추가하다가 발견. → 이 파일이 그 공백을 메운다.

검증하는 것
  ① exit_date 가 채워진 StrategyTrade 가 나온다(기존 버그 회귀 방지)
  ② 익절: 일중 고가가 목표에 닿으면 목표가 체결 / 갭상승은 시가 체결 / 진입 당일도 체크
  ③ 미도달이면 만기(hold_exit) / take_profit_pct=None 은 종전 동작
  ④ 손절과 익절이 같은 날이면 손절 우선(보수적)
"""
import os
import sys

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)

COSTS = {"fee_pct": 0.015, "tax_pct": 0.18, "slip_pct": 0.05, "total_pct": 0.245}
DATES = pd.bdate_range("2025-01-01", periods=40).strftime("%Y%m%d").tolist()


def _df(code, closes, highs=None, lows=None, opens=None, sig_day=5):
    """단일 종목 일봉 + sig_day 에 signal=True."""
    n = len(closes)
    highs = highs or [c * 1.005 for c in closes]
    lows = lows or [c * 0.995 for c in closes]
    opens = opens or list(closes)
    rows = []
    for i in range(n):
        rows.append(dict(code=code, date=DATES[i], name="x", open=opens[i], high=highs[i],
                         low=lows[i], close=closes[i], volume=1e5, trading_value=3e9,
                         change_pct=0.0, market_cap=1e12, signal=(i == sig_day)))
    return pd.DataFrame(rows)


def test_with_stops_returns_trades_with_exit_date():
    from strategies._swing_base import _make_trades_with_stops
    df = _df("A", [10000.0] * 40)
    t = _make_trades_with_stops(df, holding_days=20, strategy_name="t", costs=COSTS)
    assert len(t) == 1
    assert t[0].exit_date and len(str(t[0].exit_date)) == 8, "exit_date 누락(기존 버그 재발)"
    assert t[0].exit_reason == "hold_exit" and t[0].holding_days == 20


def test_take_profit_fills_at_target_when_high_touches():
    from strategies._swing_base import _make_trades_with_stops
    closes = [10000.0] * 40
    highs = [c * 1.005 for c in closes]
    highs[10] = 10250.0                      # 진입(6일째 시가 10000) 후 5일째 고가가 +2.5%
    t = _make_trades_with_stops(_df("A", closes, highs=highs), holding_days=20,
                                strategy_name="t", costs=COSTS, take_profit_pct=2.0)
    assert t[0].exit_reason == "take_profit"
    assert abs(t[0].exit_price - 10200.0) < 1e-6, "목표가(+2%) 체결이어야 한다"
    assert t[0].exit_date == DATES[10]


def test_take_profit_gap_up_fills_at_open():
    from strategies._swing_base import _make_trades_with_stops
    closes = [10000.0] * 40
    opens = list(closes); highs = [c * 1.005 for c in closes]
    opens[12] = 10600.0; highs[12] = 10700.0; closes[12] = 10650.0
    t = _make_trades_with_stops(_df("A", closes, highs=highs, opens=opens), holding_days=20,
                                strategy_name="t", costs=COSTS, take_profit_pct=2.0)
    assert t[0].exit_reason == "take_profit"
    assert abs(t[0].exit_price - 10600.0) < 1e-6, "시가가 목표 위면 시가 체결(유리한 쪽)"


def test_take_profit_same_day_as_entry():
    from strategies._swing_base import _make_trades_with_stops
    closes = [10000.0] * 40
    highs = [c * 1.005 for c in closes]
    highs[6] = 10300.0                       # 진입일(6일째) 고가가 +3%
    t = _make_trades_with_stops(_df("A", closes, highs=highs), holding_days=20,
                                strategy_name="t", costs=COSTS, take_profit_pct=2.0)
    assert t[0].exit_reason == "take_profit" and t[0].holding_days == 1
    assert abs(t[0].exit_price - 10200.0) < 1e-6


def test_take_profit_not_reached_falls_to_hold_exit():
    from strategies._swing_base import _make_trades_with_stops
    t = _make_trades_with_stops(_df("A", [10000.0] * 40), holding_days=20,
                                strategy_name="t", costs=COSTS, take_profit_pct=2.0)
    assert t[0].exit_reason == "hold_exit" and t[0].holding_days == 20


def test_stop_loss_wins_over_take_profit_same_day():
    from strategies._swing_base import _make_trades_with_stops
    closes = [10000.0] * 40
    highs = [c * 1.005 for c in closes]; lows = [c * 0.995 for c in closes]
    highs[10] = 10300.0; lows[10] = 8400.0   # 같은 날 +3% 고가와 -16% 저가
    t = _make_trades_with_stops(_df("A", closes, highs=highs, lows=lows), holding_days=20,
                                strategy_name="t", costs=COSTS, stop_loss_pct=-15.0,
                                take_profit_pct=2.0)
    assert t[0].exit_reason == "stop_loss", "같은 날 둘 다 닿으면 손절 우선(보수적)"


def test_take_profit_close_mode_is_conservative():
    from strategies._swing_base import _make_trades_with_stops
    closes = [10000.0] * 40
    highs = [c * 1.005 for c in closes]
    highs[10] = 10250.0                      # 고가만 닿고 종가는 못 넘음
    t = _make_trades_with_stops(_df("A", closes, highs=highs), holding_days=20,
                                strategy_name="t", costs=COSTS, take_profit_pct=2.0,
                                tp_fill="close")
    assert t[0].exit_reason == "hold_exit", "close 모드는 종가가 목표 이상일 때만 익절"
