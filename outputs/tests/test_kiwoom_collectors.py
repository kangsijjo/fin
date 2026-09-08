# -*- coding: utf-8 -*-
"""
키움 수집기 '조용한 실패' 회귀 — 2026-09-08

실사고: kiwoom_program 1종목 시험에서 캐시 토큰이 서버에서 무효(8005)였는데 수집기는
"신규 0행 / 실패 0종목" 으로 성공 보고했다. call() 이 실패 시 (None,'N','') 을 돌려주고
래퍼가 rows=[] 로 바꾸는데, 수집 루프가 빈 rows 를 '데이터 없음'으로 해석했기 때문.
같은 패턴이 신용(collect_credit)·대차(collect_lending) 수집기에도 잠복해 있었다.

검증하는 것 (prod 호출 없음 — 가짜 클라이언트)
  ① 빈 rows + last_error_msg 설정 → 세 수집기 모두 RuntimeError (실패로 집계됨)
  ② 빈 rows + last_error_msg 없음(진짜 데이터 없음) → 예외 없이 0 반환
  ③ kiwoom_program 백필 재개: 이미 since 까지 채운 종목은 _oldest_date 로 판별
  ④ _past_deadline: 야간 백필 아침 마감 판정
"""
import os
import sys
import sqlite3

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIN = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(FIN, "Stock_AI_Project"))


class _FakeClient:
    """실패(오류 메시지 有) 또는 진짜 빈 응답(오류 無)을 흉내내는 클라이언트."""
    def __init__(self, error=None):
        self.last_error_msg = error
        self.env = "fake"

    def _empty(self, *a, **k):
        return [], 'N', ''

    credit_trend = _empty
    lending_trend = _empty
    program_daily = _empty


def _conn():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE credit_balance (date TEXT, ticker TEXT, credit_remain REAL, credit_remain_rt REAL, UNIQUE(date,ticker))")
    con.execute("CREATE TABLE stock_lending (date TEXT, ticker TEXT, lending_balance REAL, lending_amount REAL, UNIQUE(date,ticker))")
    con.execute("""CREATE TABLE program_trading (date TEXT, ticker TEXT, prm_net_amt REAL, prm_net_amt_irds REAL,
                   prm_buy_amt REAL, prm_sell_amt REAL, prm_net_qty REAL, cur_prc REAL, trde_qty REAL, UNIQUE(date,ticker))""")
    return con


@pytest.mark.parametrize("collector_name", ["collect_credit", "collect_lending", "collect_program"])
def test_failed_call_raises_instead_of_silent_zero(collector_name):
    from src.collector import kiwoom_extra, kiwoom_program
    fn = getattr(kiwoom_extra, collector_name, None) or getattr(kiwoom_program, collector_name)
    with pytest.raises(RuntimeError, match="실패"):
        fn(_FakeClient(error="인증에 실패했습니다[8005:Token이 유효하지 않습니다]"), _conn(), "079960")


@pytest.mark.parametrize("collector_name", ["collect_credit", "collect_lending", "collect_program"])
def test_genuinely_empty_response_returns_zero_without_error(collector_name):
    from src.collector import kiwoom_extra, kiwoom_program
    fn = getattr(kiwoom_extra, collector_name, None) or getattr(kiwoom_program, collector_name)
    assert fn(_FakeClient(error=None), _conn(), "079960") == 0


def test_backfill_resume_skips_tickers_already_filled_to_since():
    from src.collector.kiwoom_program import _oldest_date
    con = _conn()
    con.executemany("INSERT INTO program_trading (date, ticker, prm_net_amt) VALUES (?,?,?)",
                    [("2021-12-30", "A", 1.0), ("2026-09-08", "A", 2.0), ("2025-01-02", "B", 3.0)])
    since = "2022-01-01"
    assert _oldest_date(con, "A") <= since, "A 는 since 이전까지 채워짐 → 재개 시 건너뜀 대상"
    assert not (_oldest_date(con, "B") <= since), "B 는 아직 → 수집 대상"
    assert _oldest_date(con, "C") is None, "미수집 종목 → 수집 대상"


def test_past_deadline_morning_cutoff():
    from src.collector import kiwoom_program as kp
    from datetime import datetime

    class _Now:
        def __init__(self, h, m):
            self.hour, self.minute = h, m

    orig = kp.datetime
    try:
        for (h, m), until, expect in [((5, 59), "06:00", False), ((6, 0), "06:00", True),
                                      ((6, 30), "06:00", True), ((23, 0), "06:00", False),
                                      ((6, 30), None, False)]:
            kp.datetime = type("D", (), {"now": staticmethod(lambda h=h, m=m: _Now(h, m))})
            assert kp._past_deadline(until) is expect, f"{h:02d}:{m:02d} until={until}"
    finally:
        kp.datetime = orig
