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


def test_effective_since_handles_holiday_start_date():
    """실사고(2026-09-11): since='2022-01-01' 은 휴일이라 어떤 종목도 그 날짜 행이 없다.
    MIN(date)='2022-01-03' <= '2022-01-01' 이 항상 거짓 → 완료된 794종목을 재수집(7시간 낭비)."""
    from src.collector.kiwoom_program import _effective_since, _is_backfilled
    con = _conn()
    con.execute("CREATE TABLE program_backfill_state (ticker TEXT PRIMARY KEY, since TEXT, done_at TEXT)")
    con.executemany("INSERT INTO program_trading (date, ticker, prm_net_amt) VALUES (?,?,?)",
                    [("2022-01-03", "A", 1.0), ("2026-09-08", "A", 2.0),   # 2022 첫 거래일부터 = 완료
                     ("2024-05-02", "B", 3.0)])                             # 2024 상장 or 미완료
    since = "2022-01-01"
    assert _effective_since(con, since) == "2022-01-03", "휴일 since → 실제 첫 거래일로 보정"
    assert _is_backfilled(con, "A", since, "2022-01-03"), "A 는 완료 → 건너뜀"
    assert not _is_backfilled(con, "B", since, "2022-01-03"), "B 는 미완료 → 수집"
    assert not _is_backfilled(con, "C", since, "2022-01-03"), "미수집 종목 → 수집"


def test_backfill_marker_prevents_rescan_of_late_listed_ticker():
    """2022 이후 상장 종목은 MIN(date) 가 영원히 eff_since 보다 커서 데이터 추정만으로는
    매일 밤 재수집된다. 완료 마커가 그것을 막는다."""
    from src.collector.kiwoom_program import _is_backfilled, _mark_backfilled
    con = _conn()
    con.execute("CREATE TABLE program_backfill_state (ticker TEXT PRIMARY KEY, since TEXT, done_at TEXT)")
    con.execute("INSERT INTO program_trading (date, ticker, prm_net_amt) VALUES ('2024-05-02','B',3.0)")
    since, eff = "2022-01-01", "2022-01-03"
    assert not _is_backfilled(con, "B", since, eff)
    _mark_backfilled(con, "B", since)
    assert _is_backfilled(con, "B", since, eff), "마커 기록 후에는 건너뜀"
    # 더 이른 since 를 요구하면 다시 수집해야 한다
    assert not _is_backfilled(con, "B", "2020-01-01", "2020-01-02"), "더 과거를 요구하면 재수집"


class _RowsClient:
    """지정 행을 한 페이지로 돌려주는 클라이언트(오류 없음)."""
    def __init__(self, rows):
        self.rows, self.last_error_msg, self.env = rows, None, "fake"

    def program_daily(self, *a, **k):
        return self.rows, 'N', ''


def _patch_now(kp, y, m, d, hh, mm):
    class _Now:
        def strftime(self, fmt):
            from datetime import datetime as _real
            return _real(y, m, d, hh, mm).strftime(fmt)
    kp.datetime = type("D", (), {"now": staticmethod(lambda: _Now())})


def test_collect_program_drops_today_placeholder_before_close_and_future_rows():
    """실사고(2026-09-09 06:00): 자정 넘긴 야간 백필이 ka90013 의 '오늘' 0값 자리표시 행 53개를 저장 →
    5일 합 최신 칸 오염. 장마감(15:40) 전엔 오늘 행 폐기, 미래 날짜는 항상 폐기, 마감 후엔 오늘 행 저장."""
    from src.collector import kiwoom_program as kp
    rows = [{"dt": "20260910", "prm_netprps_amt": "0"},      # 미래 → 항상 폐기
            {"dt": "20260909", "prm_netprps_amt": "0"},      # 오늘(자리표시)
            {"dt": "20260908", "prm_netprps_amt": "204"}]    # 어제(실값)
    orig = kp.datetime
    try:
        _patch_now(kp, 2026, 9, 9, 6, 0)                     # 06:00 장 시작 전
        con = _conn()
        assert kp.collect_program(_RowsClient(rows), con, "079960") == 1
        assert [r[0] for r in con.execute("SELECT date FROM program_trading ORDER BY date")] == ["2026-09-08"]

        _patch_now(kp, 2026, 9, 9, 16, 0)                    # 16:00 장 마감 후 → 오늘 행 저장
        con = _conn()
        assert kp.collect_program(_RowsClient(rows), con, "079960") == 2
        assert [r[0] for r in con.execute("SELECT date FROM program_trading ORDER BY date")] == ["2026-09-08", "2026-09-09"]
    finally:
        kp.datetime = orig


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
