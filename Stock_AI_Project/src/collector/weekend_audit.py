"""
주말 데이터 갭 감지 + 자동 백필 (Mid scope).

매주 토요일 07:00 자동 실행 → 일요일 07:00 weekly_job 이 깨끗한 데이터로 재학습.

대상별 처리
-----------
  korea_stocks / usa_stocks  : 거래일 캘린더(KOSPI/SP500) 기준 갭 감지 + fdr 백필
  macro_indicators           : 재수집 (INSERT OR REPLACE 라 중복 무해)
  supply_demand              : 갭 감지만 (KIS 30일 한계로 백필 불가)
  news                       : 스킵 (transformers 의존성, 별도 작업)

수동 실행
---------
    python -m src.collector.weekend_audit              # 전체 (korea + usa + macro + sd)
    python -m src.collector.weekend_audit --korea-only # 한국만 (빠름)
    python -m src.collector.weekend_audit --dry-run    # 감지만, 백필 안 함
"""
import sys
import time
import argparse
from datetime import datetime, timedelta

import pandas as pd
import FinanceDataReader as fdr

from src.config_db import get_connection
from src.logger import get_logger
from src.collector.db import save_stock

logger = get_logger('weekend_audit')

# 최근 N일만 검사. 더 오래된 갭은 한 번 백필 후엔 변하지 않아 매주 검사 불필요.
AUDIT_WINDOW_DAYS = 365


def _trading_calendar(market='korea'):
    """기준 지수의 거래일 리스트를 '진리' 로 사용.
    한국=KOSPI(KS11), 미국=S&P500(^GSPC).
    반환: 'YYYY-MM-DD' 문자열의 set, 실패 시 빈 set."""
    end = datetime.today().strftime('%Y-%m-%d')
    start = (datetime.today() - timedelta(days=AUDIT_WINDOW_DAYS)).strftime('%Y-%m-%d')
    try:
        idx = 'KS11' if market == 'korea' else '^GSPC'
        df = fdr.DataReader(idx, start, end)
        return set(df.index.strftime('%Y-%m-%d').tolist())
    except Exception as e:
        logger.warning(f"거래일 캘린더 조회 실패 ({market}): {e}")
        return set()


def audit_and_backfill_stocks(market='korea', dry_run=False):
    """종목별 거래일 누락 감지 + 백필.
    반환: (감지된 갭 총합, 복구 시도 종목 수, 백필 실패 종목 수)."""
    cal = _trading_calendar(market)
    if not cal:
        logger.warning(f"{market} 캘린더 비어 있음 - 검증 스킵")
        return 0, 0, 0

    stock_table = 'korea_stocks' if market == 'korea' else 'usa_stocks'

    conn = get_connection()
    try:
        tickers = pd.read_sql(
            f"SELECT DISTINCT ticker, name, sector FROM {stock_table}", conn
        )
    finally:
        conn.close()

    cutoff = (datetime.today() - timedelta(days=AUDIT_WINDOW_DAYS)).strftime('%Y-%m-%d')

    total_gap = 0
    fixed_tickers = 0
    failed_tickers = 0
    detail = []

    for i, row in tickers.iterrows():
        ticker, name, sector = row['ticker'], row['name'], row['sector']

        conn = get_connection()
        try:
            have = pd.read_sql(
                f"SELECT date FROM {stock_table} WHERE ticker=? AND date>=?",
                conn, params=[ticker, cutoff]
            )['date'].tolist()
        finally:
            conn.close()

        # 새 종목/데이터 없음은 스킵 (갭 정의 불가)
        if not have:
            continue
        have_set = {str(d)[:10] for d in have}
        first_date = min(have_set)
        # ticker 상장 이전 거래일은 갭이 아님 → first_date 이후만 비교
        expected = {d for d in cal if d >= first_date}
        missing = sorted(expected - have_set)

        if not missing:
            continue

        total_gap += len(missing)
        detail.append((ticker, name, len(missing), missing[0], missing[-1]))

        if dry_run:
            continue

        # 백필: range 한 번 호출 (개별 호출보다 fdr 한도에 친화적)
        try:
            df = fdr.DataReader(ticker, missing[0], missing[-1])
            if df is not None and not df.empty:
                df['ticker'] = ticker
                df['name'] = name
                df['sector'] = sector
                df.index.name = 'date'
                df.reset_index(inplace=True)
                save_stock(df, market=market)
                fixed_tickers += 1
                logger.info(
                    f"  backfill OK [{ticker}] {name}: 갭 {len(missing)}일 "
                    f"({missing[0]} ~ {missing[-1]})"
                )
            else:
                failed_tickers += 1
                logger.warning(f"  backfill 데이터 없음 [{ticker}] {name}")
        except Exception as e:
            failed_tickers += 1
            logger.warning(f"  backfill 실패 [{ticker}] {name}: {e}")

        # fdr rate limit 회피
        time.sleep(0.3)

    # 요약 리포트
    logger.info("")
    logger.info(f"=== {market} 갭 검증 결과 ===")
    logger.info(f"  검사 종목: {len(tickers)}개")
    logger.info(f"  갭 발견 종목: {len(detail)}개")
    logger.info(f"  갭 총합: {total_gap}일")
    if not dry_run:
        logger.info(f"  백필 성공: {fixed_tickers}개 종목")
        logger.info(f"  백필 실패: {failed_tickers}개 종목")
    else:
        logger.info(f"  (dry-run - 백필 안 함)")
    # 상위 5개만 샘플 표시
    for tk, nm, n, d0, d1 in detail[:5]:
        logger.info(f"    예시: {nm}({tk}) 갭 {n}일 [{d0} ~ {d1}]")
    if len(detail) > 5:
        logger.info(f"    ... 외 {len(detail)-5}개")

    return total_gap, fixed_tickers, failed_tickers


def audit_macro(dry_run=False):
    """거시지표는 재수집이 곧 백필 (INSERT OR REPLACE)."""
    if dry_run:
        logger.info("macro: dry-run 모드, 재수집 스킵")
        return
    from src.collector.macro import collect_macro
    logger.info("macro_indicators 재수집 (INSERT OR REPLACE)")
    collect_macro()


def audit_supply_demand():
    """수급은 KIS 30일 한계로 백필 불가 - 감지/리포트만."""
    conn = get_connection()
    try:
        df = pd.read_sql(
            """SELECT date, COUNT(DISTINCT ticker) AS n_tickers
               FROM supply_demand
               GROUP BY date ORDER BY date DESC LIMIT 60""",
            conn
        )
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()

    if df.empty:
        logger.info("supply_demand: 데이터 없음")
        return

    avg = df['n_tickers'].mean()
    logger.info(f"supply_demand: 최근 {len(df)}일, 평균 {avg:.0f} 종목/일")
    weak = df[df['n_tickers'] < avg * 0.5]
    if not weak.empty:
        logger.warning(
            f"  부분 수집 의심일 {len(weak)}건 "
            f"(KIS 한계로 백필 불가 - 일별 누적으로만 보완)"
        )
        for _, r in weak.head(5).iterrows():
            logger.warning(f"    {r['date']}: {r['n_tickers']}종목")


def run(korea_only=False, dry_run=False):
    logger.info("===== 주말 데이터 갭 감지 + 백필 시작 =====")
    t0 = time.time()

    logger.info("[1/4] korea_stocks 검증")
    audit_and_backfill_stocks('korea', dry_run=dry_run)

    if not korea_only:
        logger.info("[2/4] usa_stocks 검증")
        audit_and_backfill_stocks('usa', dry_run=dry_run)
    else:
        logger.info("[2/4] usa_stocks 스킵 (--korea-only)")

    logger.info("[3/4] macro_indicators 재수집")
    audit_macro(dry_run=dry_run)

    logger.info("[4/4] supply_demand 검증 (백필 불가)")
    audit_supply_demand()

    elapsed = (time.time() - t0) / 60
    logger.info(f"===== 주말 갭 감지 완료 ({elapsed:.1f}분) =====")


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--korea-only', action='store_true', help='미국 스킵 (빠름)')
    p.add_argument('--dry-run', action='store_true', help='감지만, 백필 안 함')
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(korea_only=args.korea_only, dry_run=args.dry_run)
