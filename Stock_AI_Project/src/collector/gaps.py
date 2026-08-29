"""
데이터 공백(결측치) 탐지 공용 유틸 (2026-06-13 정책).

모든 수집기는 '수집 전에 결측치부터 확인'하고 결측 구간만 받는다:
  1) 기준 달력 = 주가 테이블(korea_stocks/usa_stocks)의 거래일
  2) 대상 테이블에 (ticker, date) 가 없는 날짜 = 공백
  3) 공백이 있는 종목만, 공백 구간만 API 호출

주가 테이블 자체의 공백은 '시장 달력(전 종목 날짜의 합집합)' 대비로 판정
(main_collector 일일 수집 + weekend_audit 주말 정밀 백필).

사용처: supply_demand(KIS), kiwoom_extra, backfill_supply_kiwoom, main_collector
"""
import bisect


def missing_range(conn, ticker, table, since_iso,
                  stock_table='korea_stocks', logger=None):
    """종목의 공백 구간 (최초 공백일, 최후 공백일) 반환. 공백 없으면 None.

    '주가 행이 있는 날 = 거래일' 을 기준 달력으로 사용.
    table 이 아직 없으면 (since_iso, None) 폴백 — 전체 구간 수집 신호.

    주의: 대상 데이터가 원래 존재하지 않는 종목(신용/대차 비대상 ETF 등)은
    공백이 영원히 안 채워져 실행마다 1회 호출이 발생하지만 비용은 미미.
    """
    try:
        exp = {r[0][:10] for r in conn.execute(
            f"SELECT DISTINCT date FROM {stock_table} WHERE ticker=? AND date>=?",
            [ticker, since_iso])}
        if not exp:
            return None
        have = {r[0][:10] for r in conn.execute(
            f"SELECT DISTINCT date FROM {table} WHERE ticker=? AND date>=?",
            [ticker, since_iso])}
        missing = exp - have
        if not missing:
            return None
        return min(missing), max(missing)
    except Exception as e:
        if logger:
            logger.warning(f"{ticker} 공백 탐지 실패({table}) - 전체 구간 폴백: {e}")
        return since_iso, None


def market_calendar(conn, stock_table, since_iso):
    """시장 전체 거래일 달력 (전 종목 날짜의 합집합). 정렬된 리스트 반환."""
    rows = conn.execute(
        f"SELECT DISTINCT date FROM {stock_table} WHERE date>=? ORDER BY date",
        [since_iso]).fetchall()
    return [r[0][:10] for r in rows]


def price_gap_start(conn, stock_table, ticker, calendar_sorted):
    """주가 테이블 '안쪽 공백'의 최초 날짜. 공백 없으면 None.

    빠른 판정: [종목 최초 상장일 ~ 종목 마지막 날짜] 구간의
    시장 달력 일수 vs 실제 보유 행수를 먼저 비교하고 (쿼리 1번),
    불일치할 때만 날짜 목록을 읽어 정확한 공백 시작일을 찾는다.

    상장 전(종목 최초일 이전)은 공백으로 보지 않는다 — 데이터가 원래 없음.
    거래정지 기간은 공백으로 보이지만, 재수집해도 데이터가 없어
    INSERT OR IGNORE 로 무해 (호출 1번 비용).
    """
    if not calendar_sorted:
        return None
    row = conn.execute(
        f"SELECT MIN(date), MAX(date), COUNT(DISTINCT date) "
        f"FROM {stock_table} WHERE ticker=?", [ticker]).fetchone()
    if not row or not row[0]:
        return None   # 데이터 자체가 없음 → 신규 종목 (전체 수집 경로가 처리)
    mn, mx, cnt = row[0][:10], row[1][:10], row[2]
    lo = bisect.bisect_left(calendar_sorted, mn)
    hi = bisect.bisect_right(calendar_sorted, mx)
    expected = hi - lo
    if cnt >= expected:
        return None   # 안쪽 공백 없음 (추가 쿼리 불필요)
    have = {r[0][:10] for r in conn.execute(
        f"SELECT DISTINCT date FROM {stock_table} WHERE ticker=?", [ticker])}
    for d in calendar_sorted[lo:hi]:
        if d not in have:
            return d
    return None


def assert_fresh(conn, table, logger=None, calendar_table='korea_stocks',
                 label=None, exit_on_stale=True):
    """수집 직후 '기준 달력보다 뒤처졌으면' 실패로 종료. 조용한 성공 보고 차단.

    [2026-08-29 신설] 실사고: KRX 수급 수집이 08-24~28 닷새 동안 매일 1초 만에
    끝나며 스케줄러에 '완료'로 기록됐다. 전량 '공백 없음' 판정 후 0행을 넣고
    exit 0. scheduler._run_subprocess 는 exit 코드만 보므로 '0행 수집'과
    '정상 수집'이 로그에서 구분되지 않았고, watchdog 의 테이블 신선도 점검이
    **6영업일 뒤에야** 잡아냈다(파생 테이블 korea_indicators 도 동반 정체).

    이 시스템의 실패 모드는 '작업이 안 도는 것'이 아니라 **'돌았다고 하는데
    결과가 없는 것'** 이다. 오케스트레이터를 바꿔도 exit 0 은 성공으로 읽히므로,
    각 작업이 스스로 결과를 검증하는 것만이 이 유형을 잡는다.

    반환: 신선하면 True. exit_on_stale=False 면 정체 시 False 반환(종료 안 함).
    판정 자체가 실패하면 True(fail-open) — 감시 로직이 수집을 막아선 안 된다.
    """
    name = label or table
    try:
        cal = conn.execute(f"SELECT MAX(date) FROM {calendar_table}").fetchone()[0]
        mine = conn.execute(f"SELECT MAX(date) FROM {table}").fetchone()[0]
    except Exception as e:
        if logger:
            logger.warning(f"{name} 정체 판정 생략({e})")
        return True
    if not cal or not mine:
        return True
    cal, mine = str(cal)[:10], str(mine)[:10]
    if mine >= cal:
        if logger:
            logger.info(f"신선도 OK — 기준 달력 {cal} / {name} {mine}")
        return True
    msg = (f"수집 후에도 정체: 기준 달력({calendar_table}) {cal} 인데 {name} 는 {mine}. "
           f"API 무응답 또는 공백 판정 오류 의심 — 스케줄러에 실패로 보고한다.")
    if logger:
        logger.error(msg)
    else:
        print(f"[ERROR] {msg}")
    if exit_on_stale:
        import sys
        sys.exit(1)
    return False
