"""
거래량 / 등락률 상위 종목을 분당 1회 폴링하여 live_movers 테이블에 누적.

용도:
  - 매수 후보 풀의 동적 확장 근거 데이터 (Phase 2 활용)
  - 시장 레짐 변화(거래대금 급증, 일부 종목 급등) 감지
  - 사후 분석: "급등주가 다음 5일 어떻게 됐나" 같은 검증

데이터 한계:
  - KIS Open API 의 시세성 TR 은 분당 호출 한도 안에서 안전.
  - 모의/실전 모두 동작하지만 일부 필드가 다를 수 있다.
  - 현재는 누적만, 매수 자동 연결은 X (운영 데이터 모은 뒤 결정).

수동 실행:
  python -m src.trader.market_scanner --interval 60
"""
import time
import argparse
from datetime import datetime, time as dtime
from contextlib import closing

from src.config_db import get_connection
from src.logger import get_logger
from src.trader.kis_api import KISTrader

logger = get_logger('market_scanner')

KR_MARKET_OPEN  = dtime(9, 0)
KR_MARKET_CLOSE = dtime(15, 30)


def _ensure_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS live_movers (
            timestamp TEXT,
            sort_type TEXT,       -- '상승률' / '하락률' / '거래량'
            rank INTEGER,
            ticker TEXT,
            name TEXT,
            current_price REAL,
            change_rate REAL,
            volume REAL,
            volume_value REAL
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_movers_ts     ON live_movers(timestamp)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_movers_ticker ON live_movers(ticker)')


def _within_market_hours():
    now = datetime.now().time()
    return KR_MARKET_OPEN <= now <= KR_MARKET_CLOSE


def _fnum(v):
    """KIS 응답 값을 안전하게 float 변환."""
    try:
        return float(v) if v not in (None, '', '-') else 0.0
    except (TypeError, ValueError):
        return 0.0


def _snapshot(trader, conn, sort_type, rows):
    if not rows:
        return 0
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    inserts = []
    for i, r in enumerate(rows, 1):
        ticker = r.get('mksc_shrn_iscd') or r.get('stck_shrn_iscd') or ''
        inserts.append((
            ts, sort_type, i,
            ticker[:6] if ticker else None,
            r.get('hts_kor_isnm') or r.get('data_rank') or '',
            _fnum(r.get('stck_prpr')),
            _fnum(r.get('prdy_ctrt')),
            _fnum(r.get('acml_vol')),
            _fnum(r.get('acml_tr_pbmn')),
        ))
    conn.executemany('''
        INSERT INTO live_movers
        (timestamp, sort_type, rank, ticker, name,
         current_price, change_rate, volume, volume_value)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', inserts)
    conn.commit()
    return len(inserts)


def run(interval_sec=60, mock=True, count=20):
    trader = KISTrader(mock=mock)
    if not trader.get_token():
        logger.error("토큰 발급 실패 - 종료")
        return

    logger.info(
        f"시장 스캐너 시작: interval={interval_sec}s mock={mock} count={count}"
    )

    with closing(get_connection()) as conn:
        _ensure_table(conn)

        while True:
            if not _within_market_hours():
                logger.info("장 마감 - 시장 스캐너 종료")
                return

            try:
                up   = trader.get_top_change_rate(count=count, asc=False)
                time.sleep(0.5)
                vol  = trader.get_top_volume(count=count)

                n_up  = _snapshot(trader, conn, '상승률', up)
                n_vol = _snapshot(trader, conn, '거래량', vol)
                logger.info(f"스냅샷 저장: 상승률 {n_up}건 / 거래량 {n_vol}건")
            except KeyboardInterrupt:
                logger.info("사용자 중단 - 종료")
                return
            except Exception as e:
                logger.error(f"스냅샷 사이클 오류: {e}", exc_info=True)

            time.sleep(interval_sec)


def _parse_args():
    p = argparse.ArgumentParser(description="국내주식 등락률/거래량 상위 폴링")
    p.add_argument('--interval', type=int, default=60, help='폴링 간격(초)')
    p.add_argument('--count',    type=int, default=20, help='조회 종목 수')
    p.add_argument('--real', dest='mock', action='store_false', default=True,
                   help='실전모드 (기본: 모의)')
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(interval_sec=args.interval, mock=args.mock, count=args.count)
