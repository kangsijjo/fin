# -*- coding: utf-8 -*-
"""
전략 계산 점검 5건 회귀 테스트 (2026-10-23, 설명서 §64)

  ① 슬롯 규칙 = 실매매: 그날 아침 매수 뒤에 그날 만기 청산(같은 날 만기 슬롯 재사용 금지) · 보유 종목 재매수 금지
  ② 상장폐지·장기정지: 마지막 거래 종가로 청산(통째로 버리지 않음) / 자료 끝까지 이어지는 미청산은 제외
  ③ 신호 창(lookback)에 분할·병합이 낀 신호 제외 (BaseStrategy.backtest 공통 래퍼)
  ④ 상한가 시초가(+29.5% 이상) 진입은 체결 안 된 것으로 제외
  ⑤ 비용 = 수수료·슬리피지 왕복 + 매도 연도의 실제 거래세
"""
import os
import sys
from types import SimpleNamespace as NS

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

OLD_COSTS = {"fee_pct": 0.015, "tax_pct": 0.18, "slip_pct": 0.05, "total_pct": 0.245}
DATES = pd.bdate_range("2025-01-01", periods=60).strftime("%Y%m%d").tolist()


def _rows(code, closes, opens=None, sig_day=None, n=None, chg=None):
    n = n or len(closes)
    opens = opens or list(closes)
    out = []
    for i in range(n):
        c = closes[i]
        out.append(dict(code=code, date=DATES[i], name=code, open=opens[i], high=c * 1.01, low=c * 0.99,
                        close=c, volume=1e5, trading_value=3e9,
                        change_pct=(chg[i] if chg else (0.0 if i == 0 or not closes[i - 1] else
                                                        (c / closes[i - 1] - 1) * 100 if c else 0.0)),
                        market_cap=1e12, signal=(i == sig_day)))
    return out


def _df(*parts):
    return pd.DataFrame([r for p in parts for r in p]).sort_values(["code", "date"]).reset_index(drop=True)


# ── ⑤ 비용 ───────────────────────────────────────────────────────────────────────
def test_cost_by_exit_year():
    from strategy_engine import DEFAULT_COSTS as C
    from strategies._swing_base import trade_cost_pct
    assert abs(trade_cost_pct(C, "20260105") - 0.33) < 1e-9
    assert abs(trade_cost_pct(C, "20250601") - 0.28) < 1e-9
    assert abs(trade_cost_pct(C, "20210601") - 0.36) < 1e-9
    assert abs(trade_cost_pct(OLD_COSTS, "20250601") - 0.245) < 1e-9     # 옛 dict 는 total_pct 그대로
    assert abs(C["total_pct"] - trade_cost_pct(C, "20260101")) < 1e-9


def test_vector_trades_use_year_cost():
    from strategy_engine import DEFAULT_COSTS as C
    from strategies._swing_base import _make_trades_for_signals
    df = _df(_rows("A", [100.0] * 30, sig_day=3), _rows("B", [50.0] * 40))
    t = _make_trades_for_signals(df, 5, "t", C)
    assert len(t) == 1 and abs(t[0].cost_pct - 0.28) < 1e-9


# ── ④ 상한가 시초가 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("gap,expect", [(1.30, 0), (1.29, 1)])
def test_upper_limit_open_skipped_vector(gap, expect):
    from strategies._swing_base import _make_trades_for_signals
    closes = [100.0] * 30
    opens = list(closes)
    opens[4] = 100.0 * gap                                  # 신호(3일째) 다음날 시가
    df = _df(_rows("A", closes, opens=opens, sig_day=3), _rows("B", [50.0] * 40))
    assert len(_make_trades_for_signals(df, 5, "t", OLD_COSTS)) == expect


@pytest.mark.parametrize("gap,expect", [(1.30, 0), (1.29, 1)])
def test_upper_limit_open_skipped_with_stops(gap, expect):
    from strategies._swing_base import _make_trades_with_stops
    closes = [100.0] * 30
    opens = list(closes)
    opens[4] = 100.0 * gap
    df = _df(_rows("A", closes, opens=opens, sig_day=3), _rows("B", [50.0] * 40))
    assert len(_make_trades_with_stops(df, 5, "t", OLD_COSTS)) == expect


# ── ② 상장폐지·거래정지 ──────────────────────────────────────────────────────────
def test_delisted_exits_at_last_close_vector():
    from strategies._swing_base import _make_trades_for_signals
    closes = [100.0] * 10 + [40.0, 30.0]                     # 12일째(인덱스 11)에 자료 끝 = 상장폐지
    df = _df(_rows("A", closes, sig_day=8), _rows("B", [50.0] * 40))
    t = _make_trades_for_signals(df, 20, "t", OLD_COSTS)
    assert len(t) == 1
    assert t[0].exit_date == DATES[11] and abs(t[0].gross_pct - (30 / 100 - 1) * 100) < 1e-6


def test_open_at_data_end_still_dropped_vector():
    from strategies._swing_base import _make_trades_for_signals
    df = _df(_rows("A", [100.0] * 40, sig_day=35))          # 자료 끝까지 이어짐 → 아직 보유 중
    assert _make_trades_for_signals(df, 20, "t", OLD_COSTS) == []


def test_halt_on_exit_day_uses_last_traded_close_vector():
    from strategies._swing_base import _make_trades_for_signals
    closes = [100.0] * 40
    for i in range(7, 12):
        closes[i] = 0.0                                     # 거래정지(종가 0) — 청산일(인덱스 8) 포함
    closes[6] = 90.0
    chg = [0.0] * 40
    chg[6] = -10.0
    df = _df(_rows("A", closes, sig_day=3, chg=chg))
    t = _make_trades_for_signals(df, 5, "t", OLD_COSTS)     # 진입 4, 청산 예정 8
    assert len(t) == 1 and t[0].exit_date == DATES[6] and abs(t[0].gross_pct + 10) < 1e-6


def test_delisted_and_open_end_with_stops():
    from strategies._swing_base import _make_trades_with_stops
    closes = [100.0] * 10 + [40.0, 30.0]
    df = _df(_rows("A", closes, sig_day=8), _rows("B", [50.0] * 40, sig_day=35))
    t = _make_trades_with_stops(df, 20, "t", OLD_COSTS)
    assert [x.code for x in t] == ["A"]                    # B 는 자료 끝 미청산 → 제외
    assert t[0].exit_date == DATES[11]


# ── ③ 신호 창 안 분할·병합 ───────────────────────────────────────────────────────
def _ca_df(sig_day):
    closes = [100.0] * 5 + [1000.0] * 45                    # 인덱스 5 에 1:10 병합(원시 종가 10배, KRX 등락률 0)
    chg = [0.0] * 50
    return _df(_rows("A", closes, sig_day=sig_day, chg=chg), _rows("B", [50.0] * 60))


def test_drop_ca_lookback_trades():
    from strategies._swing_base import _make_trades_for_signals, drop_ca_lookback_trades
    df = _ca_df(sig_day=20)
    t = _make_trades_for_signals(df, 5, "t", OLD_COSTS)
    assert len(t) == 1
    assert drop_ca_lookback_trades(t, df, 20) == []          # 창 20일 안(인덱스 5) → 제외
    assert len(drop_ca_lookback_trades(t, df, 10)) == 1      # 창 10일이면 무관 → 유지


def test_base_strategy_wrapper_applies_ca_filter():
    import strategies.base as B
    from strategies._swing_base import _make_trades_for_signals

    class Toy(B.BaseStrategy):
        name = "toy"

        def __init__(self, lookback):
            self.lookback = lookback

        def backtest(self, df, costs):
            return _make_trades_for_signals(df, 5, self.name, costs)

    df = _ca_df(sig_day=20)
    assert Toy(30).backtest(df.copy(), OLD_COSTS) == []
    assert len(Toy(8).backtest(df.copy(), OLD_COSTS)) == 1
    old = B.CA_LOOKBACK_FILTER
    try:
        B.CA_LOOKBACK_FILTER = False
        assert len(Toy(30).backtest(df.copy(), OLD_COSTS)) == 1
    finally:
        B.CA_LOOKBACK_FILTER = old


def test_every_engine_strategy_is_wrapped():
    import strategy_engine as E
    for s in E.ALL_STRATEGIES:
        if s.timeframe == "daily":
            assert getattr(type(s).backtest, "_ca_wrapped", False), s.name


def test_live_rows_drop_ca():
    import engine_signals as ES

    class S:
        lookback = 30
    df = _ca_df(sig_day=None)
    rows = [{"code": "A"}, {"code": "B"}]
    assert [r["code"] for r in ES._drop_ca_rows(rows, S(), df, DATES[20], "x", log=lambda *_: None)] == ["B"]


def test_signal_right_after_long_halt_is_dropped():
    """긴 정지(거래량 0 ≥5일) 뒤 재개 10거래일 안 신호(정리매매 등) 제외 — 백테스트·실매매 같은 규칙."""
    import engine_signals as ES
    from strategies._swing_base import _make_trades_for_signals, drop_ca_lookback_trades
    closes = [100.0] * 50
    rows = _rows("A", closes, sig_day=22, chg=[0.0] * 50)
    for i in range(10, 18):
        rows[i]["volume"] = 0                                 # 8일 정지 → 18일째 재개
    df = _df(rows, _rows("B", [50.0] * 60))
    t = _make_trades_for_signals(df, 5, "t", OLD_COSTS)
    assert len(t) == 1
    assert drop_ca_lookback_trades(t, df, 5) == []            # 재개(18) 후 4거래일째 신호 → 제외
    rows2 = _rows("A", closes, sig_day=35, chg=[0.0] * 50)
    for i in range(10, 18):
        rows2[i]["volume"] = 0
    df2 = _df(rows2, _rows("B", [50.0] * 60))
    assert len(drop_ca_lookback_trades(_make_trades_for_signals(df2, 5, "t", OLD_COSTS), df2, 5)) == 1

    class S:
        def __init__(self):
            self.lookback = 5
    out = ES._drop_ca_rows([{"code": "A"}, {"code": "B"}], S(), df, DATES[22], "x", log=lambda *_: None)
    assert [r["code"] for r in out] == ["B"]


# ── ① 슬롯 규칙 ──────────────────────────────────────────────────────────────────
def _cal(n):
    return pd.bdate_range("2025-01-01", periods=n).strftime("%Y%m%d").tolist()


def test_same_day_expiring_slot_not_reused_and_no_rebuy():
    import period_backtest as P
    from capital_simulator import simulate_capital
    cal = _cal(20)
    pm = {(c, d): 100.0 for c in "ABC" for d in cal}
    tr = [NS(entry_date=cal[1], exit_date=cal[5], code="A", entry_price=100, net_pct=1.0, gross_pct=1.0, score=3),
          # 슬롯 1개: A 가 cal[5] 종가에 청산 → cal[5] 아침 매수(B)는 못 한다(실매매: 09:00 매수, 15:20 청산)
          NS(entry_date=cal[5], exit_date=cal[9], code="B", entry_price=100, net_pct=5.0, gross_pct=5.0, score=2),
          NS(entry_date=cal[6], exit_date=cal[9], code="C", entry_price=100, net_pct=2.0, gross_pct=2.0, score=1)]
    eq, taken = P.equity_curve(tr, pm, cal, slots=1)
    assert taken == [1.0, 2.0]
    ref = simulate_capital(tr, max_concurrent=1, price_map=pm, trading_dates=cal)
    assert abs((eq.iloc[-1] / 1e7 - 1) * 100 - ref["total_ret_pct"]) < 0.01

    tr2 = [NS(entry_date=cal[1], exit_date=cal[8], code="A", entry_price=100, net_pct=1.0, gross_pct=1.0, score=3),
           NS(entry_date=cal[3], exit_date=cal[10], code="A", entry_price=100, net_pct=9.0, gross_pct=9.0, score=3)]
    eq2, taken2 = P.equity_curve(tr2, pm, cal, slots=5)
    assert taken2 == [1.0]                                    # 이미 보유 중인 종목은 다시 사지 않는다
    ref2 = simulate_capital(tr2, max_concurrent=5, price_map=pm, trading_dates=cal)
    assert abs((eq2.iloc[-1] / 1e7 - 1) * 100 - ref2["total_ret_pct"]) < 0.01
