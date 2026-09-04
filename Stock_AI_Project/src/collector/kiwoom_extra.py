"""
키움 전용 데이터 수집: 신용매매동향(ka10013) + 종목별 대차거래추이(ka20068).

KIS 가 제공하지 않는 키움 전용 데이터.
  - credit_balance : 신용잔고/잔고율 → 피처 credit_ratio (개인 레버리지 과열 지표)
  - stock_lending  : 대차잔고주수   → 피처 lending_chg_5d (공매도 압력 프록시)

매일 1회 증분 수집 (scheduler daily_data_job 에서 호출):
  python -m src.collector.kiwoom_extra

과거 백필 (1회성, 페이지네이션):
  python -m src.collector.kiwoom_extra --backfill --since 2022-01-01
  python -m src.collector.kiwoom_extra --backfill --limit 10        # 테스트

INSERT OR IGNORE 라 중복 무해. 중단 후 재실행 가능.
"""
import sys
import os
import time
import json
import argparse
from contextlib import closing, contextmanager
from datetime import datetime

import pandas as pd
from tqdm import tqdm

from src.config_db import get_connection, write_retry
from src.logger import get_logger
from src.collector.kiwoom_api import KiwoomClient, weekend_guard
from src.collector.supply_demand import _kr_tickers, _fnum
from src.collector.gaps import missing_range

logger = get_logger('kiwoom_extra')

# 호출 속도 제한은 KiwoomClient 내부 throttle 이 담당. 여기는 잔여 여유분만.
SLEEP_BETWEEN_CALLS = 0.05

# ── 키움 API 파일락 ──────────────────────────────────────────────────────────
# kiwoom_trader(우리 시스템)와 동일 lock 파일 공유 → 동시 접근 방지.
_LOCK_CANDIDATES = [
    os.getenv("KIWOOM_LOCK", ""),
    os.path.join(os.path.dirname(__file__), "../../../data/.kiwoom.lock"),
    os.path.join(os.environ.get("FIN_ROOT", ""), "Stock_AI_Project", "data", ".kiwoom.lock")
        if os.environ.get("FIN_ROOT") else "",     # FIN_ROOT 환경변수(이관 대비, 2026-09-04)
]
_LOCK_PATH = next((os.path.normpath(p) for p in _LOCK_CANDIDATES
                   if p and os.path.isdir(os.path.dirname(os.path.normpath(p)))), None)


@contextmanager
def _kiwoom_lock(timeout: int = 60):
    """키움 API 파일락. 이미 잠겨 있으면 timeout 초 대기 후 경고와 함께 진행."""
    if _LOCK_PATH is None:
        yield
        return

    deadline = time.time() + timeout
    while os.path.exists(_LOCK_PATH):
        try:
            with open(_LOCK_PATH, "r") as f:
                info = json.load(f)
            age = time.time() - info.get("ts", 0)
            if age > 300:   # 5분 이상 stale → 강제 해제
                os.remove(_LOCK_PATH)
                break
            logger.warning(f"키움 락 대기 중 (보유: {info.get('who','?')}, {age:.0f}초 경과)...")
        except Exception:
            break
        if time.time() > deadline:
            logger.warning("키움 락 대기 시간 초과 — 잠금 무시하고 진행")
            break
        time.sleep(5)

    try:
        with open(_LOCK_PATH, "w") as f:
            json.dump({"who": f"kiwoom_extra(pid={os.getpid()})",
                       "ts": time.time(), "started": datetime.now().isoformat()}, f)
    except Exception:
        pass

    try:
        yield
    finally:
        try:
            if os.path.exists(_LOCK_PATH):
                os.remove(_LOCK_PATH)
        except Exception:
            pass


def _ensure_tables(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS credit_balance (
            date TEXT,
            ticker TEXT,
            credit_remain REAL,      -- 신용잔고 (주수)
            credit_remain_rt REAL,   -- 신용잔고율 (%)
            UNIQUE(date, ticker)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cb_ticker ON credit_balance(ticker)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stock_lending (
            date TEXT,
            ticker TEXT,
            lending_balance REAL,    -- 대차잔고 (주수)
            lending_amount REAL,     -- 대차잔고 금액
            UNIQUE(date, ticker)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_sl_ticker ON stock_lending(ticker)')


def _iso(d):
    d = (d or '').strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else None


# 공백 탐지는 전 수집기 공용 모듈로 이동 (2026-06-13, src/collector/gaps.py)
def _missing_range(conn, ticker, table, since_iso):
    return missing_range(conn, ticker, table, since_iso, logger=logger)


def collect_credit(client, conn, ticker, since_iso=None, paginate=False,
                   date_param=None):
    """ka10013 신용매매동향 → credit_balance. 반환: 신규 행 수.

    date_param(YYYY-MM-DD): 조회 기준일 — 공백 구간의 마지막 날부터
    과거 방향으로 since_iso 까지만 페이지네이션 (불필요한 호출 차단).
    """
    inserted = 0
    cont_yn, next_key = 'N', ''
    date_q = date_param.replace('-', '') if date_param else None
    while True:
        rows, cont_yn, next_key = client.credit_trend(
            ticker, date=date_q, cont_yn=cont_yn, next_key=next_key)
        if not rows:
            break
        inserts, reached = [], False
        for r in rows:
            d = _iso(r.get('dt'))
            if d is None:
                continue
            if since_iso and d < since_iso:
                reached = True
                continue
            inserts.append((d, ticker,
                            _fnum(r.get('remn')),
                            _fnum(r.get('remn_rt'))))
        if inserts:
            cur = write_retry(
                lambda: conn.executemany(
                    """INSERT OR IGNORE INTO credit_balance
                       (date, ticker, credit_remain, credit_remain_rt)
                       VALUES (?, ?, ?, ?)""", inserts),
                logger=logger)
            inserted += cur.rowcount or 0
        if not paginate or reached or cont_yn != 'Y' or not next_key:
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    return inserted


def collect_lending(client, conn, ticker, since_iso=None, paginate=False,
                    end_iso=None):
    """ka20068 대차거래추이 → stock_lending. 반환: 신규 행 수.

    end_iso(YYYY-MM-DD): 공백 구간의 마지막 날. ka20068 은 시작/종료일을
    직접 받으므로 공백 구간만 정확히 요청 가능.
    """
    inserted = 0
    cont_yn, next_key = 'N', ''
    start_dt = since_iso.replace('-', '') if since_iso else ''
    end_dt = (end_iso.replace('-', '') if end_iso
              else datetime.now().strftime('%Y%m%d'))
    while True:
        rows, cont_yn, next_key = client.lending_trend(
            ticker, start_date=start_dt, end_date=end_dt,
            cont_yn=cont_yn, next_key=next_key)
        if not rows:
            break
        inserts = []
        for r in rows:
            d = _iso(r.get('dt'))
            if d is None or (since_iso and d < since_iso):
                continue
            inserts.append((d, ticker,
                            _fnum(r.get('rmnd')),
                            _fnum(r.get('remn_amt'))))
        if inserts:
            cur = write_retry(
                lambda: conn.executemany(
                    """INSERT OR IGNORE INTO stock_lending
                       (date, ticker, lending_balance, lending_amount)
                       VALUES (?, ?, ?, ?)""", inserts),
                logger=logger)
            inserted += cur.rowcount or 0
        if not paginate or cont_yn != 'Y' or not next_key:
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    return inserted


def run(backfill=False, since='2022-01-01', limit=None, force=False):
    """공백(gap) 감지 기반 수집.

    korea_stocks 거래일 대비 빠진 날짜가 있는 종목만, 그 공백 구간만 호출.
    공백이 전혀 없으면 키움 API 호출(토큰 발급 포함) 0회.
    backfill 인자는 하위호환용으로만 남김 (이제 항상 공백 기반으로 동작).
    """
    if not weekend_guard(force):
        return

    tickers = _kr_tickers()
    if not tickers:
        logger.warning("종목 리스트 비어 있음")
        return
    if limit:
        tickers = tickers[:limit]

    logger.info(f"키움 신용/대차 공백 수집 시작: 종목 {len(tickers)}개, since={since}")

    client = None   # 공백이 있는 종목을 처음 만났을 때만 토큰 발급
    total_c = total_l = skipped = 0
    start_ts = time.time()
    with _kiwoom_lock(timeout=60), closing(get_connection()) as conn:
        _ensure_tables(conn)
        # 터미널 직접 실행 시 퍼센트 게이지(tqdm), 스케줄러/리다이렉트 시 자동 비활성
        bar = tqdm(tickers, desc="신용/대차 수집", unit="종목",
                   disable=not sys.stderr.isatty())
        for i, t in enumerate(bar, 1):
            try:
                gap_c = _missing_range(conn, t, 'credit_balance', since)
                gap_l = _missing_range(conn, t, 'stock_lending', since)
                if gap_c is None and gap_l is None:
                    skipped += 1
                    continue

                if client is None:
                    client = KiwoomClient()
                    if not client.get_token():
                        logger.error("키움 토큰 발급 실패 - 중단 "
                                     "(8050=지정단말기 인증이면 키움 홈페이지에서 이 PC 등록/해제 필요)")
                        # exit 0 으로 끝나면 스케줄러가 '✔ 완료'로 오인(3초 조용한 실패,
                        # 2026-07-04 실제 발생) → 비0 종료로 ERROR/failures.log 에 남긴다.
                        sys.exit(1)

                if gap_c is not None:
                    total_c += collect_credit(
                        client, conn, t, since_iso=gap_c[0],
                        paginate=True, date_param=gap_c[1])
                    time.sleep(SLEEP_BETWEEN_CALLS)
                if gap_l is not None:
                    total_l += collect_lending(
                        client, conn, t, since_iso=gap_l[0],
                        paginate=True, end_iso=gap_l[1])
            except Exception as e:
                logger.error(f"{t} 수집 실패(다음 종목 진행): {e}")
            # 종목마다 커밋 → 중단 시 1종목 이내만 손실
            write_retry(conn.commit, logger=logger)
            if i % 50 == 0:
                pct = i / len(tickers) * 100
                eta_min = (time.time() - start_ts) / i * (len(tickers) - i) / 60
                tqdm.write(f"  진행 {i}/{len(tickers)} ({pct:.1f}%) | "
                           f"신용 {total_c:,} / 대차 {total_l:,}행 / 공백없음 {skipped} | "
                           f"예상 잔여 {eta_min:.0f}분")
            time.sleep(SLEEP_BETWEEN_CALLS)
        write_retry(conn.commit, logger=logger)

    logger.info(f"완료. 신용 {total_c:,}행 / 대차 {total_l:,}행 추가 "
                f"(공백 없어 스킵 {skipped}종목).")


def _parse_args():
    p = argparse.ArgumentParser(description="키움 신용/대차 데이터 수집")
    p.add_argument('--backfill', action='store_true', help='과거 백필 모드 (페이지네이션)')
    p.add_argument('--since', default='2022-01-01', help='백필 시작일 (YYYY-MM-DD)')
    p.add_argument('--limit', type=int, default=None, help='테스트용 종목 수 제한')
    p.add_argument('--force', action='store_true',
                   help='주중 실행 강제 (키움 키 공유 충돌 주의)')
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(backfill=args.backfill, since=args.since, limit=args.limit, force=args.force)
