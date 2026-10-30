# -*- coding: utf-8 -*-
"""모의계좌 탭(§83) — 비용 차감 미실현 손익, KIS 매입평균 변환, 두 계좌 같은 배치."""
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_kis_to_int_accepts_decimal_strings():
    import kis_trader as kx
    assert kx._to_int("23050.0000") == 23050, "매입평균(pchs_avg_pric)이 0 으로 읽히던 버그"
    assert kx._to_int("1,234") == 1234 and kx._to_int("+5") == 5 and kx._to_int("x") == 0


@pytest.fixture(scope="module")
def dash():
    import integrated_dashboard_server as d
    return d


def test_net_pnl_is_gain_minus_costs(dash):
    r = dash._net_pnl(100, 10000, 11000, on="2026")          # 수수료 0.015% ×2(매수·매도) + 거래세 0.20%(매도)
    assert r["gross"] == 100000
    assert r["cost"] == round(1_000_000 * 0.00015 + 1_100_000 * (0.00015 + 0.002))
    assert r["net"] == r["gross"] - r["cost"] and r["pct"] == round(r["net"] / 1_000_000 * 100, 2)
    assert dash._net_pnl(10, 0, 100) is None, "매입가를 모르면 계산하지 않는다"


def test_apply_net_pnl_keeps_broker_value_and_sums(dash):
    pos = [{"qty": 10, "avg_price": 1000, "cur_price": 1100, "pnl": 999, "pnl_pct": 9.9},
           {"qty": 5, "avg_price": 0, "cur_price": 500, "pnl": 2500, "pnl_pct": 0}]
    snap = {}
    dash._apply_net_pnl(pos, snap)
    assert pos[0]["broker_pnl"] == 999 and pos[0]["pnl"] < 1000 and pos[1]["pnl"] is None
    assert snap["eval_pnl_net"] == pos[0]["pnl"] and snap["eval_pnl_missing"] == 1


def test_mock_tab_same_order_for_both_accounts(dash):
    html = dash.HTML
    seg = html[html.index('<div id="tab-mock"'):html.index("<!-- ===== 강도매매 ===== -->")]
    order = lambda acct: [seg.index(f"<h2>{acct} {t}") for t in ("잔고", "슬롯", "보유 포지션", "매매내역", "전략별 성과")]
    for acct in ("키움", "KIS"):
        o = order(acct)
        assert o == sorted(o), acct
    assert seg.index("<h2>KIS 보유 포지션") < seg.index("<h2>KIS 손절 모니터") < seg.index("<h2>KIS 매매내역")
    assert seg.count("<div") == seg.count("</div>")
