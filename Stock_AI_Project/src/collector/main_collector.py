"""
[2026-06-14] 트레이딩 시스템(C:/fin/outputs/pykrx_collector.py) 연동 메모:
  - 트레이딩 시스템이 매일 16:05 에 KOSDAQ OHLCV 를 pykrx 로 수집 후
    stock.db korea_stocks 에 INSERT(UPSERT).
  - 이 파일의 collect_all() 이 latest_dates 를 조회할 때 이미 트레이딩 시스템이
    채운 KOSDAQ 날짜는 fetch_start >= end 조건으로 자동 스킵됨.
  - KOSPI, S&P500 은 트레이딩 시스템 수집 범위 외이므로 여기서 계속 담당.
  - name / sector 는 FDR 에서만 가져올 수 있으므로 최초 수집 시 이 파일이 담당.
"""
import FinanceDataReader as fdr
import pandas as pd
import time
from datetime import datetime, timedelta
from .config import START_DATE
from .db import save_stock
from .gaps import market_calendar, price_gap_start
from .krx_sector import get_sector_overrides
from src.config_db import get_connection
from src.logger import get_logger

logger = get_logger('collector')

def get_all_tickers():
    """KOSPI + KOSDAQ + S&P500 전체 종목 리스트.

    한국 종목은 sector_map(네이버 업종 분류)을 우선 참조한다.
    KOSPI Dept 는 '우량기업부' 등 시장 소속부여서 업종명이 아님.
    sector_map 에 없는 종목은 FDR Dept 를 그대로 사용.
    """
    tickers = []

    # sector_map 우선 로드 (krx_sector.py 로 생성)
    try:
        _sm_conn = get_connection()
        sector_overrides = get_sector_overrides(_sm_conn)
        _sm_conn.close()
        if sector_overrides:
            logger.info(f"sector_map 적용: {len(sector_overrides):,}개 ticker 업종 교정")
    except Exception:
        sector_overrides = {}

    korea_ok = False
    logger.info("KOSPI 종목 리스트 가져오는 중...")
    try:
        kospi = fdr.StockListing('KOSPI')[['Code', 'Name', 'Dept']].copy()
        kospi.columns = ['ticker', 'name', 'sector']
        kospi['sector'] = kospi.apply(
            lambda r: sector_overrides.get(r['ticker'], r['sector']), axis=1)
        kospi['market'] = 'korea'
        tickers.append(kospi)
        korea_ok = True
        logger.info(f"KOSPI {len(kospi)}개 종목 확인")
    except Exception as e:
        logger.error(f"KOSPI 리스트 가져오기 실패: {e}")

    logger.info("KOSDAQ 종목 리스트 가져오는 중...")
    try:
        kosdaq = fdr.StockListing('KOSDAQ')[['Code', 'Name', 'Dept']].copy()
        kosdaq.columns = ['ticker', 'name', 'sector']
        kosdaq['sector'] = kosdaq.apply(
            lambda r: sector_overrides.get(r['ticker'], r['sector']), axis=1)
        kosdaq['market'] = 'korea'
        tickers.append(kosdaq)
        korea_ok = True
        logger.info(f"KOSDAQ {len(kosdaq)}개 종목 확인")
    except Exception as e:
        logger.error(f"KOSDAQ 리스트 가져오기 실패: {e}")

    # [폴백] KRX(data.krx.co.kr)가 점검/차단으로 종목 리스트를 막으면(JSON 대신 점검 HTML)
    # KOSPI·KOSDAQ 둘 다 실패한다. 이때 기존 korea_stocks 의 종목 유니버스로 수집을 이어간다.
    # (가격 엔드포인트는 리스트 엔드포인트와 달라 보통 살아있어 가격은 계속 받힘.)
    if not korea_ok:
        try:
            _c = get_connection()
            fb = pd.read_sql(
                "SELECT DISTINCT ticker, name, sector FROM korea_stocks WHERE ticker IS NOT NULL", _c)
            _c.close()
            if len(fb):
                fb['sector'] = fb.apply(
                    lambda r: sector_overrides.get(r['ticker'], r['sector']), axis=1)
                fb['market'] = 'korea'
                tickers.append(fb)
                logger.warning(f"[폴백] KRX 리스트 실패 → 기존 korea_stocks {len(fb):,}개 종목으로 수집 진행 "
                               f"(KRX 점검/차단 추정. 가격은 정상 수집될 수 있음)")
            else:
                logger.error("[폴백] korea_stocks 가 비어 폴백 불가 — KRX 복구 후 재시도 필요")
        except Exception as e:
            logger.error(f"[폴백] korea_stocks 조회 실패: {e}")

    logger.info("S&P500 종목 리스트 가져오는 중...")
    try:
        sp500 = fdr.StockListing('S&P500')[['Symbol', 'Name', 'Sector']].copy()
        sp500.columns = ['ticker', 'name', 'sector']
        sp500['market'] = 'usa'
        tickers.append(sp500)
        logger.info(f"S&P500 {len(sp500)}개 종목 확인")
    except Exception as e:
        logger.error(f"S&P500 리스트 가져오기 실패: {e}", exc_info=True)

    df = pd.concat(tickers, ignore_index=True)
    logger.info(f"총 {len(df)}개 종목 확인완료")
    return df

def get_latest_dates():
    """DB에 저장된 종목별 가장 최근 날짜 가져오기"""
    conn = get_connection()
    try:
        kr = pd.read_sql(
            "SELECT ticker, MAX(date) as last_date FROM korea_stocks GROUP BY ticker",
            conn
        )
        us = pd.read_sql(
            "SELECT ticker, MAX(date) as last_date FROM usa_stocks GROUP BY ticker",
            conn
        )
        df = pd.concat([kr, us])
        return dict(zip(df['ticker'], df['last_date']))
    except Exception as e:
        logger.warning(f"DB 읽기 실패(처음 실행일 수 있음): {e}")
        return {}
    finally:
        conn.close()

def collect_all():
    """전체 종목 증분 업데이트"""
    end = datetime.today().strftime('%Y-%m-%d')
    all_tickers = get_all_tickers()
    total = len(all_tickers)

    # 종목별 최신 날짜 불러오기
    latest_dates = get_latest_dates()

    # ── 결측치(안쪽 공백) 우선 확인 (2026-06-13 정책) ──
    # '마지막 날짜 이후'만 받으면 중간에 비어 있는 날짜는 영원히 안 메워진다.
    # 시장 달력(전 종목 날짜의 합집합) 대비 안쪽 공백을 찾아,
    # 공백이 있으면 fetch 시작일을 공백 시작일까지 당긴다.
    # save_stock 이 INSERT OR IGNORE + UNIQUE(ticker,date) 라 재수집 중복 무해.
    gap_conn = get_connection()
    try:
        kr_cal = market_calendar(gap_conn, 'korea_stocks', START_DATE)
        us_cal = market_calendar(gap_conn, 'usa_stocks', START_DATE)
    except Exception as e:
        logger.warning(f"시장 달력 로드 실패 - 안쪽 공백 검사 생략: {e}")
        kr_cal, us_cal = [], []

    logger.info(f"총 {total}개 종목 증분 업데이트 시작 (목표일: {end})")

    failed_tickers = []
    gap_repaired = 0
    n_updated = 0
    n_skipped = 0
    log_step = max(1, total // 20)   # 5% 단위 로그 (최대 20줄)

    for seq, (i, row) in enumerate(all_tickers.iterrows(), 1):
        ticker = row['ticker']
        name = row['name']
        market = row['market']
        sector = row['sector']

        # 시작 날짜 결정 (없으면 START_DATE, 있으면 마지막 날짜 +1일)
        if ticker in latest_dates and latest_dates[ticker]:
            last = datetime.strptime(latest_dates[ticker][:10], '%Y-%m-%d')
            fetch_start = (last + timedelta(days=1)).strftime('%Y-%m-%d')

            # 안쪽 공백이 있으면 거기부터 다시 받는다 (결측치 우선)
            stock_table = 'korea_stocks' if market == 'korea' else 'usa_stocks'
            cal = kr_cal if market == 'korea' else us_cal
            try:
                gap_start = price_gap_start(gap_conn, stock_table, ticker, cal)
            except Exception:
                gap_start = None
            if gap_start is not None and gap_start < fetch_start:
                logger.info(f"[{ticker}] 안쪽 공백 감지 ({gap_start}~) → 재수집")
                fetch_start = gap_start
                gap_repaired += 1
        else:
            fetch_start = START_DATE

        # 이미 최신이면 스킵
        if fetch_start >= end:
            n_skipped += 1
            if seq % log_step == 0 or seq == total:
                logger.info(f"진행 [{seq}/{total}] {seq/total*100:.0f}% | 갱신 {n_updated} | 스킵 {n_skipped} | 실패 {len(failed_tickers)}")
            continue

        if seq % log_step == 0 or seq == total:
            logger.info(f"진행 [{seq}/{total}] {seq/total*100:.0f}% | 갱신 {n_updated} | 실패 {len(failed_tickers)}")

        for attempt in range(3):
            try:
                logger.debug(f"[{seq}/{total}] [{sector}] {name} ({ticker}) | {fetch_start} ~ {end}")
                df = fdr.DataReader(ticker, fetch_start, end)

                if df is None or len(df) == 0:
                    logger.debug(f"새로운 데이터 없음: {ticker}")
                    break

                df['ticker'] = ticker
                df['name'] = name
                df['sector'] = sector
                df.index.name = 'date'
                df.reset_index(inplace=True)
                save_stock(df, market=market)
                n_updated += 1
                break

            except Exception as e:
                logger.warning(f"시도 {attempt+1}/3 실패 [{ticker}]: {e}")
                if attempt < 2:
                    time.sleep(2)
                else:
                    logger.error(f"최종 실패 [{ticker}] {name}: {e}", exc_info=True)
                    failed_tickers.append((ticker, name))

    gap_conn.close()

    if failed_tickers:
        logger.warning(f"\n=== 최종 실패 종목 {len(failed_tickers)}개 ===")
        for t, n in failed_tickers:
            logger.warning(f"  {t} {n}")

    if gap_repaired:
        logger.info(f"안쪽 공백 재수집: {gap_repaired}종목")
    logger.info(f"전체 수집 완료! 갱신 {n_updated} / 스킵 {n_skipped} / 실패 {len(failed_tickers)}")

if __name__ == "__main__":
    collect_all()