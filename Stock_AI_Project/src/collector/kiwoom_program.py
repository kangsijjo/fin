# -*- coding: utf-8 -*-
"""
kiwoom_program.py — 종목별 일별 프로그램매매 수집 (키움 ka90013) → program_trading 테이블. (2026-09-08)

왜 필요한가
  factor_scorer 의 prm_net_5d_ratio 는 라이브 6전략 83,705건에서 IC +0.141(IS +0.165 / OOS +0.111),
  IC 질량 10.7% 로 수급 계열 중 유일하게 살아있는 피처인데, program_trading 테이블이 없어
  라이브에서 89% 결측이었다(2026-09-08 진단). 이 수집기가 그 구멍을 메운다.

학습된 정의(복원, kiwoom_hist_features.csv 역산):
  prm_net_5d_raw   = 최근 5거래일 프로그램 순매수 금액 합 (키움 단위: 백만원)
  prm_net_5d_ratio = prm_net_5d_raw / 당일 거래대금(원)   ← 분모는 '당일' (5일합 아님)
  ※ 단위 불일치(백만원/원)로 값이 1e-7 스케일이지만 IC 는 순위 기반이라 무해. 정의 그대로 유지.
  → 이 테이블은 API 원형 필드를 저장하고, 5일 합·비율 계산은 factor_scorer(prepare_db_features) 가 한다.

운영 제약 (kiwoom_extra 와 동일)
  · prod 키움 API(mock 은 시세성 TR 미지원). env 는 Stock_AI_Project/.env 의 KIWOOM_ENV 를 따른다 — 여기서 바꾸지 않음.
  · 키움 키가 다른 시스템과 공유 → 주중 실행 차단(weekend_guard), --force 로만 해제. 파일락(_kiwoom_lock) 공유.
  · 정식 수집은 kiwoom_extra 와 같이 토요일 배치에 얹는 것을 권고(별도 결정).

사용
  python -m src.collector.kiwoom_program --ticker 079960 --since 2026-01-10 --force   # 1종목 시험
  python -m src.collector.kiwoom_program --backfill --since 2022-01-01 [--limit N] [--force]
  python -m src.collector.kiwoom_program                                             # 증분(최근 공백만)
"""
import os
import sys
import time
import argparse
from datetime import datetime

from tqdm import tqdm

from src.config_db import get_connection, write_retry
from src.logger import get_logger
from src.collector.kiwoom_api import KiwoomClient, weekend_guard
from src.collector.supply_demand import _kr_tickers, _fnum
from src.collector.kiwoom_extra import _kiwoom_lock, SLEEP_BETWEEN_CALLS, _iso

logger = get_logger("kiwoom_program")


def _kw_num(v):
    """키움 숫자 파서. 키움은 음수를 '--153', 등락 부호를 '+267000'/'-26650' 으로 보낸다.
    공용 _fnum 은 '--153' 을 0.0 으로 만들어(float 실패) 음수 순매수가 전부 0 이 되고,
    과거 kiwoom_backfill 도 그랬기에 학습된 prm_net_5d_raw 는 '음수 날=0' 절단 정의다
    (2026-09-08 079960 대조: 01-28 순매수 -520 을 0 으로 쳐야 CSV 값 978 과 일치).
    → DB 에는 참값(부호 보존)을 저장하고, 절단 재현은 factor_scorer 피처 계층에서 한다."""
    if v in (None, '', '-', '--'):
        return 0.0
    s = str(v).strip()
    neg = s.startswith('--')
    s = s.lstrip('+-')
    try:
        x = float(s)
    except (TypeError, ValueError):
        return 0.0
    return -x if neg else x


def _ensure_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS program_trading (
            date TEXT,
            ticker TEXT,
            prm_net_amt REAL,        -- 프로그램 순매수 금액 (키움 단위: 백만원)
            prm_net_amt_irds REAL,   -- 순매수 금액 증감
            prm_buy_amt REAL,
            prm_sell_amt REAL,
            prm_net_qty REAL,        -- 순매수 수량
            cur_prc REAL,            -- 당일 종가(부호 제거)
            trde_qty REAL,           -- 거래량
            UNIQUE(date, ticker)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pt_ticker ON program_trading(ticker)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pt_date ON program_trading(date)')


def _latest_date(conn, ticker):
    row = conn.execute(
        "SELECT MAX(date) FROM program_trading WHERE ticker=?", (ticker,)).fetchone()
    return row[0] if row and row[0] else None


def collect_program(client, conn, ticker, since_iso=None, paginate=False, date_param=None):
    """ka90013 → program_trading. 반환: 신규 행 수.
    since_iso(YYYY-MM-DD) 이전은 저장하지 않고 페이지네이션도 거기서 멈춘다(불필요 호출 차단)."""
    inserted = 0
    cont_yn, next_key = 'N', ''
    date_q = date_param.replace('-', '') if date_param else None
    while True:
        rows, cont_yn, next_key = client.program_daily(
            ticker, date=date_q, cont_yn=cont_yn, next_key=next_key)
        if not rows:
            # 실패 호출(인증·한도·네트워크)의 빈 결과를 '데이터 없음'으로 삼키면 "신규 0행/실패 0"
            # 으로 조용히 성공 처리된다(2026-09-08 시험에서 실측). 오류면 예외로 올린다.
            if getattr(client, 'last_error_msg', None):
                raise RuntimeError(f"ka90013 실패: {client.last_error_msg}")
            break
        inserts, reached = [], False
        # 당일 행 가드: ka90013 은 장 시작 전에도 '오늘' 행을 0값 자리표시로 돌려준다(2026-09-09 06:00
        # 실측 — 자정 넘긴 야간 백필이 이를 저장해 5일 합의 최신 칸을 0 으로 오염). 장마감(15:40) 전엔
        # 오늘 행을 버리고, 미래 날짜는 항상 버린다. 마감 후 재수집 시 REPLACE 로 정상값이 들어간다.
        _now = datetime.now()
        _today = _now.strftime('%Y-%m-%d')
        _after_close = _now.strftime('%H%M') >= '1540'
        for r in rows:
            d = _iso(r.get('dt'))
            if d is None:
                continue
            if d > _today or (d == _today and not _after_close):
                continue
            if since_iso and d < since_iso:
                reached = True
                continue
            inserts.append((d, ticker,
                            _kw_num(r.get('prm_netprps_amt')),        # 부호 보존(참값)
                            _kw_num(r.get('prm_netprps_amt_irds')),
                            _kw_num(r.get('prm_buy_amt')),
                            _kw_num(r.get('prm_sell_amt')),
                            _kw_num(r.get('prm_netprps_qty')),
                            abs(_kw_num(r.get('cur_prc'))),           # 가격의 +/- 는 등락 표시일 뿐
                            _kw_num(r.get('trde_qty'))))
        if inserts:
            # REPLACE: 파서 수정 등으로 재수집하면 기존 행을 갱신(IGNORE 면 옛 값이 영구 잔존)
            cur = write_retry(
                lambda: conn.executemany(
                    """INSERT OR REPLACE INTO program_trading
                       (date, ticker, prm_net_amt, prm_net_amt_irds, prm_buy_amt, prm_sell_amt,
                        prm_net_qty, cur_prc, trde_qty)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", inserts),
                logger=logger)
            inserted += cur.rowcount or 0
        if not paginate or reached or cont_yn != 'Y' or not next_key:
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    return inserted


def _oldest_date(conn, ticker):
    row = conn.execute(
        "SELECT MIN(date) FROM program_trading WHERE ticker=?", (ticker,)).fetchone()
    return row[0] if row and row[0] else None


def _past_deadline(until_hhmm):
    """until='06:00' 형식. 지정 시각을 넘었으면 True (같은 날 기준; 자정 넘김은 시각이 작아지므로
    '시작 시각보다 작고 현재가 until 이상'으로 판정 — 야간 백필이 아침 충돌 시간대로 넘어가는 것 방지)."""
    if not until_hhmm:
        return False
    now = datetime.now()
    hh, mm = (int(x) for x in until_hhmm.split(':'))
    return (now.hour, now.minute) >= (hh, mm) and now.hour < 12


def run(backfill=False, since='2022-01-01', limit=None, force=False, ticker=None, until=None):
    """backfill=True: since 까지 과거 방향 페이지네이션(이미 since 까지 채운 종목은 건너뜀 → 재개 가능).
    False: 종목별 마지막 저장일 이후만(증분). until='HH:MM' 이면 그 시각에 정상 종료(다음 실행이 이어받음)."""
    if not weekend_guard(force):
        return False

    with _kiwoom_lock():
        client = KiwoomClient()
        if not client.get_token():
            logger.error("키움 토큰 발급 실패 — 중단")
            return False

        with get_connection() as conn:
            _ensure_table(conn)
            tickers = [ticker] if ticker else _kr_tickers()
            if limit:
                tickers = tickers[:limit]
            logger.info(f"[program] 대상 {len(tickers)}종목 | backfill={backfill} since={since} "
                        f"until={until} env={client.env}")

            total, fail, skipped, attempted = 0, 0, 0, 0
            t0 = time.time()
            bar = tqdm(tickers, desc="program(ka90013)", unit="종목", ncols=90,
                       disable=not sys.stdout.isatty())
            for i, t in enumerate(bar, 1):
                if _past_deadline(until):
                    logger.warning(f"[program] 마감 {until} 도달 — {i - 1}/{len(tickers)} 처리 후 정상 종료"
                                   f"(재실행 시 이어서 수집)")
                    break
                try:
                    if backfill:
                        oldest = _oldest_date(conn, t)
                        if oldest and oldest <= since:
                            skipped += 1          # 이미 since 까지 채워진 종목 → 재개 시 건너뜀
                            continue
                        attempted += 1
                        n = collect_program(client, conn, t, since_iso=since, paginate=True)
                    else:
                        attempted += 1
                        last = _latest_date(conn, t)
                        # 증분: 마지막 저장일 이후만. 첫 수집이면 since 부터.
                        n = collect_program(client, conn, t, since_iso=(last or since), paginate=True)
                    total += n
                    conn.commit()
                except Exception as e:
                    fail += 1
                    logger.warning(f"{t} 실패: {str(e)[:100]}")
                    if fail >= 20 and fail / max(attempted, 1) > 0.5:
                        logger.error("실패율 50% 초과 — 토큰/한도 문제 의심, 중단")
                        return False
                if i % 100 == 0:
                    el = time.time() - t0
                    eta = el / i * (len(tickers) - i) / 60
                    logger.info(f"[program] 진행 {i}/{len(tickers)} | 신규 {total:,}행 | 실패 {fail} | "
                                f"건너뜀 {skipped} | 경과 {el / 60:.0f}분 | ETA {eta:.0f}분")
                time.sleep(SLEEP_BETWEEN_CALLS)

            logger.info(f"[program] 완료: 신규 {total:,}행 / 실패 {fail}종목 / 건너뜀 {skipped}종목 "
                        f"(총 {(time.time() - t0) / 60:.0f}분)")
            tickers = tickers[:attempted] if attempted else tickers   # 전종목실패 판정은 시도분 기준
            # 시도한 종목이 전부 실패(인증·한도 등)면 '성공' 종료코드를 주지 않는다 —
            # exit 0 이면 스케줄러가 '완료'로 오인하는 조용한 실패(2026-07-04 credit 사례 재발 방지)
            if fail and fail >= len(tickers):
                logger.error("[program] 전 종목 실패 — 비0 종료")
                return False
    return True


def _parse_args():
    p = argparse.ArgumentParser(description="키움 ka90013 종목별 일별 프로그램매매 수집")
    p.add_argument('--backfill', action='store_true', help='since 까지 과거 전체 페이지네이션')
    p.add_argument('--since', default='2022-01-01', help='수집 시작일 YYYY-MM-DD')
    p.add_argument('--limit', type=int, default=None, help='처리 종목 수 상한(시험용)')
    p.add_argument('--ticker', default=None, help='단일 종목 코드(시험용)')
    p.add_argument('--force', action='store_true',
                   help='주중 실행 강제 (키움 키 공유 충돌 주의 — 장중 금지)')
    p.add_argument('--until', default=None,
                   help='HH:MM 마감 — 야간 백필이 아침(다른 시스템 키 사용) 시간대로 넘어가지 않게 정상 종료')
    return p.parse_args()


if __name__ == "__main__":
    a = _parse_args()
    ok = run(backfill=a.backfill or bool(a.ticker), since=a.since, limit=a.limit,
             force=a.force, ticker=a.ticker, until=a.until)
    sys.exit(0 if ok else 1)
