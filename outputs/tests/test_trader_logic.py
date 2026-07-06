"""
트레이더 핵심 로직 단위테스트 — 2026-07-02~05 세션에서 실버그를 잡았던 임시 테스트의 영구화.

배경: 기존 test_feature_contract 는 '데이터 계약'만 커버 — 만기청산 날짜비교·매수 상한·
익절 헬퍼 같은 트레이더 로직 버그는 전부 그 밖이었고, 아래 테스트들의 임시판이 잡았다.
(잡은 버그: _glob 미정의 조용한 실패, 익절 전략 오배정, 매도 price=0, tp 체결 중복기록)

전부 임시 디렉토리/합성 데이터 사용 — 실주문·실파일 무접촉, 네트워크 불필요.
실행: cd C:/fin/outputs ; python -m pytest tests -q
"""
import os
import sys
from datetime import datetime

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)


# ─────────────────────────────────────────────────────────────────────────────
# 호가단위(_tick_floor) — 익절 지정가 유효가격
# ─────────────────────────────────────────────────────────────────────────────

def test_tick_floor_krx_bands():
    import kiwoom_trader as kt
    cases = [  # (원시가, 기대 tick, 기대 결과)
        (1999,          1, 1999),
        (2481 * 1.2,    5, 2975),
        (7450 * 1.2,   10, 8940),
        (32400 * 1.5,  50, 48600),
        (52600 * 1.5, 100, 78900),
        (250000 * 1.2, 500, 300000),
    ]
    for raw, tick, want in cases:
        got = kt._tick_floor(raw)
        assert got == want and got % tick == 0, (raw, got, want)


# ─────────────────────────────────────────────────────────────────────────────
# 익절 지정가 헬퍼 3종 — 임시 ORDERS_DIR 로 격리
#   (실제로 _glob 미정의 조용한 실패·전략 오배정을 잡아낸 테스트)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def kt_tmp_orders(tmp_path, monkeypatch):
    import kiwoom_trader as kt
    monkeypatch.setattr(kt, "ORDERS_DIR", str(tmp_path))
    today = datetime.today().strftime("%Y%m%d")
    pd.DataFrame([
        {"time": "09:03", "side": "buy", "code": "105330", "name": "KNW",
         "strategy": "rsi_vol", "qty": 247, "price": 6888,
         "order_type": "3", "ok": True, "order_no": "B1", "msg": ""},
        {"time": "09:03", "side": "tp_sell", "code": "105330", "name": "KNW",
         "strategy": "rsi_vol", "qty": 247, "price": 8260,
         "order_type": "0", "ok": True, "order_no": "T1", "msg": "target+20%"},
        {"time": "09:04", "side": "tp_sell", "code": "091590", "name": "NHT",
         "strategy": "high_52w_filt", "qty": 160, "price": 10840,
         "order_type": "0", "ok": False, "order_no": "", "msg": "err"},
    ]).to_csv(tmp_path / f"orders_{today}.csv", index=False, encoding="utf-8-sig")
    return kt, tmp_path, today


def test_today_tp_orders_filters_ok(kt_tmp_orders):
    kt, _, _ = kt_tmp_orders
    assert kt._today_tp_orders() == {"105330": "T1"}   # ok=False 는 제외


def test_held_strategy_map_uses_buy_time_strategy(kt_tmp_orders):
    """익절 목표%는 '매수 당시' 전략으로 — 최신 신호 기준이면 오배정(+20%↔+50%)."""
    kt, _, _ = kt_tmp_orders
    assert kt._held_strategy_map().get("105330") == "rsi_vol"


def test_reconcile_tp_fill_synthetic_sell_and_idempotent(kt_tmp_orders):
    """발주됐는데 미보유 = 체결 → 합성 sell 1회만 기록(재실행 멱등)."""
    kt, tmp_path, today = kt_tmp_orders
    kt._reconcile_tp_fills(pos={})                     # 105330 미보유 → 체결 처리
    assert "105330" in kt.today_ordered_codes("sell")
    df = pd.read_csv(tmp_path / f"orders_{today}.csv", dtype=str)
    row = df[(df["side"] == "sell") & (df["code"] == "105330")]
    assert len(row) == 1
    assert row.iloc[0]["price"] == "8260"
    assert "profit_target" in row.iloc[0]["msg"]
    kt._reconcile_tp_fills(pos={})                     # 멱등 — 중복 기록 없음
    df2 = pd.read_csv(tmp_path / f"orders_{today}.csv", dtype=str)
    assert len(df2[(df2["side"] == "sell") & (df2["code"] == "105330")]) == 1


def test_reconcile_tp_holding_not_filled(kt_tmp_orders):
    """아직 보유 중이면 체결 아님(합성 sell 금지). ok=False 발주도 체결 처리 금지."""
    kt, tmp_path, today = kt_tmp_orders
    kt._reconcile_tp_fills(pos={"105330": {"qty": 247, "name": "KNW"}})
    df = pd.read_csv(tmp_path / f"orders_{today}.csv", dtype=str)
    assert not len(df[df["side"] == "sell"])


# ─────────────────────────────────────────────────────────────────────────────
# 대시보드 실현손익 FIFO(_attach_realized_pnl) — 매도 price=0 종가보정 포함
# ─────────────────────────────────────────────────────────────────────────────

def _srv():
    sys.argv = [sys.argv[0]]   # 서버 모듈이 sys.argv[1] 을 PORT 로 파싱 → 차단
    import integrated_dashboard_server as srv
    return srv


def test_realized_pnl_fifo_and_close_backfill(monkeypatch):
    srv = _srv()
    # 매도일 종가 조회를 합성값으로 대체(파일 무접촉)
    monkeypatch.setattr(srv, "_daily_close_map",
                        lambda d: {"000010": 1100.0} if str(d) == "20260620" else {})
    df = pd.DataFrame([
        {"date": "20260601", "time": "09:01", "side": "buy",  "code": "000010",
         "qty": 10, "price": 1000, "ok": True},
        {"date": "20260620", "time": "15:21", "side": "sell", "code": "000010",
         "qty": 10, "price": 0, "ok": True},          # price=0 → 종가 1100 보정
        {"date": "20260602", "time": "09:01", "side": "buy",  "code": "000020",
         "qty": 5, "price": 2000, "ok": True},
        {"date": "20260621", "time": "15:21", "side": "sell", "code": "000020",
         "qty": 5, "price": 1800, "ok": True},
        {"date": "20260622", "time": "09:03", "side": "tp_sell", "code": "000020",
         "qty": 5, "price": 2400, "ok": True},        # tp '발주'는 체결 아님 → pnl 없음
    ]).fillna("")
    out = srv._attach_realized_pnl(df)
    s1 = out[(out["side"] == "sell") & (out["code"] == "000010")].iloc[0]
    assert s1["pnl"] == 1000 and s1["pnl_pct"] == 10.0
    assert s1["price"] == 1100                         # 표시가도 종가로 보정
    s2 = out[(out["side"] == "sell") & (out["code"] == "000020")].iloc[0]
    assert s2["pnl"] == -1000 and s2["pnl_pct"] == -10.0
    tp = out[out["side"] == "tp_sell"].iloc[0]
    assert tp["pnl"] == "" and tp["pnl_pct"] == ""     # 발주 행은 손익 미부여
    for _, b in out[out["side"] == "buy"].iterrows():
        assert b["pnl"] == ""
