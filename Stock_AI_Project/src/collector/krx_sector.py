"""
네이버 금융 업종 분류로 ticker → sector 매핑 테이블 생성/갱신.

배경
----
fdr.StockListing('KOSPI') 의 Dept 필드는 KRX 시장 소속부(우량기업부, 중견기업부 등)를
반환해 업종 분류로 쓸 수 없다. KOSDAQ 은 업종명을 반환하지만 KOSPI 는 다르다.
→ 네이버 금융 업종별 종목 페이지에서 올바른 업종을 크롤링해
  sector_map(ticker, sector) 테이블에 저장한다.

main_collector.py 는 이 테이블을 우선 참조해 올바른 섹터로 저장.

실행
----
  python -m src.collector.krx_sector          # 전 섹터 갱신
  python -m src.collector.krx_sector --sector 2차전지  # 특정 섹터만
"""
import time
import argparse
import requests
from bs4 import BeautifulSoup

from src.config_db import get_connection
from src.logger import get_logger

logger = get_logger('krx_sector')

# 네이버 금융 업종 코드 (type=upjong&no=XXX)
NAVER_SECTORS = {
    '반도체':   '278',
    '방산':     '284',
    '조선':     '291',
    '2차전지':  '306',
    '엔터':     '285',
    '바이오':   '286',
    '제약':     '261',
    '자동차':   '273',
    '자동차부품': '270',
    '소프트웨어': '287',
    'IT서비스': '267',
    '전기장비': '274',   # 전기/전자장비
    '화학':     '272',
    '철강':     '304',
    '건설':     '279',
    '은행':     '301',
    '증권':     '321',
    '보험':     '315',
    '게임':     '263',
    '통신':     '333',
    '항공':     '305',
    '해운':     '323',
    '음식료':   '268',
}

_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


def _ensure_sector_map(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sector_map (
            ticker  TEXT PRIMARY KEY,
            sector  TEXT NOT NULL,
            source  TEXT DEFAULT 'naver',
            updated TEXT DEFAULT (date('now'))
        )
    ''')
    conn.commit()


def fetch_sector_tickers(sector_name, sector_no, retries=3):
    """네이버 금융 업종 페이지에서 종목 리스트 크롤링."""
    url = (f'https://finance.naver.com/sise/sise_group_detail.naver'
           f'?type=upjong&no={sector_no}')
    for attempt in range(retries):
        try:
            res = requests.get(url, headers=_HEADERS, timeout=10)
            res.encoding = 'euc-kr'
            soup = BeautifulSoup(res.text, 'html.parser')
            tickers = []
            for row in soup.select('table.type_5 tr'):
                link = row.select_one('a[href*="item/main"]')
                if link:
                    ticker = link['href'].split('code=')[-1][:6]
                    name = link.text.strip()
                    tickers.append((ticker, name))
            if tickers:
                return tickers
        except Exception as e:
            logger.warning(f'{sector_name} 시도 {attempt+1}/{retries} 실패: {e}')
            time.sleep(2)
    logger.error(f'{sector_name} 크롤링 최종 실패')
    return []


def update_sector_map(sectors=None):
    """sector_map 테이블을 네이버 업종 분류로 갱신.

    sectors: None 이면 전체, list 이면 해당 섹터만.
    """
    target = {k: v for k, v in NAVER_SECTORS.items()
              if sectors is None or k in sectors}

    conn = get_connection()
    _ensure_sector_map(conn)

    total = 0
    for sector_name, sector_no in target.items():
        logger.info(f'  {sector_name} (no={sector_no}) 크롤링 중...')
        tickers = fetch_sector_tickers(sector_name, sector_no)
        if not tickers:
            continue

        rows = [(t, sector_name) for t, _ in tickers]
        conn.executemany('''
            INSERT INTO sector_map (ticker, sector, updated)
            VALUES (?, ?, date('now'))
            ON CONFLICT(ticker) DO UPDATE SET
                sector  = excluded.sector,
                updated = excluded.updated
        ''', rows)
        conn.commit()
        logger.info(f'    → {len(tickers)}개 종목 매핑')
        total += len(tickers)
        time.sleep(0.5)   # 네이버 서버 부담 최소화

    # sector_map 반영: korea_stocks.sector 도 동시 업데이트
    # (이미 INSERT OR IGNORE 로 쌓인 기존 행들의 sector 를 올바르게 교정)
    updated = conn.execute('''
        UPDATE korea_stocks
        SET sector = (
            SELECT sm.sector FROM sector_map sm WHERE sm.ticker = korea_stocks.ticker
        )
        WHERE EXISTS (
            SELECT 1 FROM sector_map sm WHERE sm.ticker = korea_stocks.ticker
        )
    ''').rowcount
    conn.commit()
    conn.close()

    logger.info(f'완료: sector_map {total}개 ticker, korea_stocks {updated:,}행 섹터 교정')


def get_sector_overrides(conn):
    """sector_map 에서 ticker→sector dict 반환. main_collector 용."""
    try:
        rows = conn.execute('SELECT ticker, sector FROM sector_map').fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _parse_args():
    p = argparse.ArgumentParser(description='네이버 업종 분류로 sector_map 갱신')
    p.add_argument('--sector', nargs='+', default=None,
                   help='특정 섹터만 갱신 (예: --sector 반도체 2차전지)')
    return p.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    update_sector_map(sectors=args.sector)
