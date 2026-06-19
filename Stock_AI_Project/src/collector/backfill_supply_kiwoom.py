"""
키움 ka10059 로 외국인/기관 수급 '과거 백필' (1회성/재개 가능).

배경
----
KIS API 는 종목당 최근 30일만 제공 → 9년 백필 불가 (문서화된 한계).
키움 ka10059 는 연속조회(next-key)로 과거 무제한 제공 → 이 스크립트로 채운다.
매일 증분 수집은 기존 KIS(supply_demand.py) 그대로 유지한다 (사용자 결정).

단위 자동 보정 (중요)
--------------------
KIS 와 키움의 금액 단위가 다를 수 있다 (KIS: 천원 추정, 키움: 백만원 추정).
같은 컬럼에 두 스케일이 섞이면 rolling 합 피처가 오염되므로,
이미 KIS 로 쌓인 (ticker, date) 와 겹치는 날짜에서 두 값의 비율을 측정해
{0.001, 1, 1000} 중 가장 가까운 보정계수를 자동 선택한다.
겹침 표본이 부족하면 --scale 수동 지정 없이는 중단한다 (오염 방지).

실행
----
  python -m src.collector.backfill_supply_kiwoom                  # 2015-01-01 까지
  python -m src.collector.backfill_supply_kiwoom --since 2022-01-01
  python -m src.collector.backfill_supply_kiwoom --limit 10       # 테스트
  python -m src.collector.backfill_supply_kiwoom --scale 1000     # 보정계수 수동

소요시간: 종목 수 × 기간에 비례. 3000종목 × 9년이면 수십 시간 → 주말 실행 권장.
중단해도 재실행하면 이미 since 까지 채워진 종목은 건너뛴다 (재개 가능).
"""
import sys
import time
import argparse
import statistics
from contextlib import closing
from datetime import datetime

import pandas as pd
from tqdm import tqdm

from src.config_db import get_connection, write_retry
from src.logger import get_logger
from src.collector.kiwoom_api import KiwoomClient, weekend_guard
from src.collector.supply_demand import _ensure_table, _kr_tickers, _fnum
from src.collector.gaps import missing_range as _missing_range

logger = get_logger('backfill_supply')

# 호출 속도 제한은 KiwoomClient 내부 throttle 이 담당 (모의 1.2s / 실전 0.3s).
# 여기는 잔여 여유분만.
SLEEP_BETWEEN_CALLS = 0.05
CANDIDATE_SCALES = [0.001, 1.0, 1000.0]
MIN_CALIB_SAMPLES = 5


def _iso(d):
    """YYYYMMDD → YYYY-MM-DD"""
    d = (d or '').strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else None


def calibrate_scale(client, conn, tickers, max_probe=20):
    """KIS 기존 데이터와 겹치는 날짜에서 (KIS값 / 키움값) 비율 측정.
    반환: 보정계수 또는 None (표본 부족)."""
    ratios = []
    for t in tickers[:max_probe]:
        kis = pd.read_sql(
            "SELECT date, foreign_net_value FROM supply_demand "
            "WHERE ticker=? AND foreign_net_value != 0",
            conn, params=[t]
        )
        if kis.empty:
            continue
        kis_map = dict(zip(kis['date'], kis['foreign_net_value']))

        rows, _, _ = client.investor_daily(t)
        time.sleep(SLEEP_BETWEEN_CALLS)
        for r in rows:
            d = _iso(r.get('dt'))
            kw = _fnum(r.get('frgnr_invsr'))
            if d in kis_map and kw != 0 and kis_map[d] != 0:
                ratios.append(abs(kis_map[d] / kw))
        if len(ratios) >= MIN_CALIB_SAMPLES * 3:
            break

    if len(ratios) < MIN_CALIB_SAMPLES:
        return None
    med = statistics.median(ratios)
    # 후보 스케일 중 log 거리 최소
    import math
    scale = min(CANDIDATE_SCALES, key=lambda s: abs(math.log10(med) - math.log10(s)))
    logger.info(f"단위 보정: 표본 {len(ratios)}개, 중앙값 비율 {med:.4g} → scale={scale}")
    if not (0.3 < med / scale < 3.0):
        logger.warning(
            f"비율 중앙값 {med:.4g} 이 후보 스케일과 멀리 떨어짐 — "
            f"수동 확인 권장 (--scale 로 지정 가능)")
    return scale


def backfill_ticker(client, conn, ticker, since_iso, scale, start_iso=None):
    """한 종목을 since 까지 페이지네이션으로 백필. 반환: 신규 행 수.

    start_iso: 공백 구간의 마지막 날(여기서부터 과거 방향으로 조회).
    미지정 시 오늘부터.
    """
    inserted = 0
    cont_yn, next_key = 'N', ''
    date_param = (start_iso.replace('-', '') if start_iso
                  else datetime.now().strftime('%Y%m%d'))

    while True:
        rows, cont_yn, next_key = client.investor_daily(
            ticker, date=date_param, cont_yn=cont_yn, next_key=next_key)
        if not rows:
            break

        inserts = []
        reached_since = False
        for r in rows:
            d = _iso(r.get('dt'))
            if d is None:
                continue
            if d < since_iso:
                reached_since = True
                continue
            inserts.append((
                d, ticker,
                _fnum(r.get('frgnr_invsr')) * scale,   # 외국인 순매수 금액
                _fnum(r.get('orgn')) * scale,          # 기관 순매수 금액
                None,
            ))
        if inserts:
            # 다른 수집 프로세스(06:30 일일 수집 등)가 쓰기 잠금을 잡고 있으면
            # 'database is locked' → 기다렸다 재시도 (멱등이라 안전)
            cur = write_retry(
                lambda: conn.executemany(
                    """INSERT OR IGNORE INTO supply_demand
                       (date, ticker, foreign_net_value, institution_net_value, short_balance_ratio)
                       VALUES (?, ?, ?, ?, ?)""", inserts),
                logger=logger)
            inserted += cur.rowcount or 0

        if reached_since or cont_yn != 'Y' or not next_key:
            break
        time.sleep(SLEEP_BETWEEN_CALLS)

    return inserted


def run(since='2015-01-01', limit=None, scale=None, force=False):
    if not weekend_guard(force):
        return
    client = KiwoomClient()
    if not client.get_token():
        logger.error("키움 토큰 발급 실패 - 중단")
        return

    tickers = _kr_tickers()
    if not tickers:
        logger.warning("종목 리스트 비어 있음")
        return
    if limit:
        tickers = tickers[:limit]

    with closing(get_connection()) as conn:
        _ensure_table(conn)

        if scale is None:
            scale = calibrate_scale(client, conn, tickers)
            if scale is None:
                logger.error(
                    "단위 보정 표본 부족 (KIS 데이터와 겹치는 날짜 없음). "
                    "KIS 수급이 며칠 쌓인 뒤 재시도하거나 --scale 로 직접 지정하세요. "
                    "(KIS=천원, 키움=백만원이면 --scale 1000)")
                return
        else:
            logger.info(f"단위 보정 수동 지정: scale={scale}")

        logger.info(f"수급 백필 시작: 종목 {len(tickers)}개, since={since}, scale={scale}")
        total = 0
        skipped = 0
        start_ts = time.time()
        # 터미널 직접 실행 시 퍼센트 게이지(tqdm), 스케줄러/리다이렉트 시 자동 비활성
        bar = tqdm(tickers, desc="수급 백필", unit="종목",
                   disable=not sys.stderr.isatty())
        for i, t in enumerate(bar, 1):
            # 공백 감지: 거래일(korea_stocks) 대비 빠진 날짜가 없으면 호출 0회.
            # 안쪽 구멍(중간 누락)도 감지되어 그 구간만 다시 받는다.
            gap = _missing_range(conn, t, 'supply_demand', since)
            if gap is None:
                skipped += 1
                continue
            try:
                total += backfill_ticker(client, conn, t, gap[0], scale,
                                         start_iso=gap[1])
            except Exception as e:
                logger.error(f"{t} 백필 실패(다음 종목 진행): {e}")
            # 자주 커밋해서 쓰기 잠금 보유 시간을 짧게 유지
            # (다른 프로세스의 locked 오류 예방. WAL 커밋은 저렴함)
            if i % 5 == 0:
                write_retry(conn.commit, logger=logger)
            if i % 20 == 0:
                pct = i / len(tickers) * 100
                eta_min = (time.time() - start_ts) / i * (len(tickers) - i) / 60
                logger.info(f"  진행 {i}/{len(tickers)} ({pct:.1f}%) | "
                            f"신규 {total:,}행, 스킵 {skipped} | "
                            f"예상 잔여 {eta_min:.0f}분")
            time.sleep(SLEEP_BETWEEN_CALLS)
        write_retry(conn.commit, logger=logger)

    logger.info(f"백필 완료. 신규 {total:,}행, 스킵 {skipped}종목 (이미 완료분).")


def _parse_args():
    p = argparse.ArgumentParser(description="키움 ka10059 수급 과거 백필")
    p.add_argument('--since', default='2015-01-01', help='백필 시작일 (YYYY-MM-DD)')
    p.add_argument('--limit', type=int, default=None, help='테스트용 종목 수 제한')
    p.add_argument('--scale', type=float, default=None,
                   help='키움→KIS 단위 보정계수 수동 지정 (미지정 시 자동 보정)')
    p.add_argument('--force', action='store_true',
                   help='주중 실행 강제 (키움 키 공유 충돌 주의)')
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(since=args.since, limit=args.limit, scale=args.scale, force=args.force)
