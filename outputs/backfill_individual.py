# -*- coding: utf-8 -*-
"""
backfill_individual.py — 개인 순매수(순매수거래대금)를 supply_demand 에 백필. (2026-09-08 신설)

목적: '개인 이탈 + 외국인/기관 유입' 수급역전 전략(사용자 아이디어 2026-09-08)을
      백테스트하려면 종목별·일별 '개인' 순매수가 필요하다. supply_demand 에는 외국인/
      기관만 있었고 개인이 없어서, pykrx 로 개인 순매수를 수집해 individual_net_value
      컬럼에 채운다.

설계 (foreign_ratio_collector 의 검증된 패턴을 그대로):
  - pykrx get_market_net_purchases_of_equities_by_ticker(date, date, market, "개인")
    = 특정일 전종목 개인 순매수거래대금. (외국인/기관과 동일 함수, investor 만 다름)
  - KRX 로그인 필요(KRX_ID/KRX_PW, .env). 빈응답/스로틀 잦음 → 재시도·throttle 하드닝.
  - 멱등: individual_net_value 가 이미 채워진 날짜는 스킵(--backfill). 재실행 안전.
  - supply_demand 의 date 는 대시형('2026-09-04'), pykrx 는 무대시('20260904') → 변환.

사용:
  python backfill_individual.py --backfill                    # 미채움 전체
  python backfill_individual.py --backfill --limit 3          # 최근 3거래일만(테스트)
  python backfill_individual.py --backfill 20260101 20260131  # 기간 지정

주의: KRX 대량 호출(일별 x 2시장). 장 마감(15:30) 후·주말 권장. 장중 대량수집은
      틱 웹소켓/다른 KRX 작업과 부하를 다툴 수 있다(2026-09-04 교훈).
"""
import os
import sys
import time
import argparse
import sqlite3
from contextlib import closing
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))

# KRX 로그인 자격증명 주입(개인 순매수 엔드포인트가 로그인 필요)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_HERE, ".env"), override=True)
except Exception:
    pass

# 콘솔 인코딩 방탄(cp949 콘솔에서 한글/진행문자 print 크래시 방지)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from fin_paths import STOCK_DB as _DB_PATH
    DB_PATH = str(_DB_PATH)
except Exception:
    DB_PATH = os.path.join(_HERE, "..", "Stock_AI_Project", "data", "stock.db")

MARKETS = ("KOSPI", "KOSDAQ")
CALL_SLEEP = 0.8       # 콜 간 throttle(빈응답 예방)
RETRY_SLEEP = 2.5      # 실패 재시도 대기
RETRIES = 4


def _dash(d8: str) -> str:
    """'20260904' → '2026-09-04' (supply_demand 저장 포맷)."""
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"


def _target_dates(conn, lo=None, hi=None):
    """백필 대상 거래일(무대시 YYYYMMDD). supply_demand 에 있는 날짜 중
    individual_net_value 가 '한 종목도 안 채워진' 날짜만."""
    done = {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM supply_demand WHERE individual_net_value IS NOT NULL")}
    alld = {r[0] for r in conn.execute("SELECT DISTINCT date FROM supply_demand")}
    todo = sorted(alld - done)                      # 대시형
    out = []
    for d in todo:
        d8 = d.replace("-", "")
        if lo and d8 < lo:
            continue
        if hi and d8 > hi:
            continue
        out.append(d8)
    return out


def _pull_individual(stock, date8, market):
    """특정일·시장 개인 순매수거래대금 → {ticker: value}. 실패 시 None."""
    last_err = None
    for attempt in range(RETRIES):
        try:
            df = stock.get_market_net_purchases_of_equities_by_ticker(
                date8, date8, market, "개인")
            if df is None or df.empty:
                # 빈응답: 마지막 시도까지 재시도(스로틀 오응답 vs 진짜 휴장 구분 불가)
                if attempt < RETRIES - 1:
                    time.sleep(RETRY_SLEEP)
                    continue
                return {}
            df = df.reset_index()
            col = "순매수거래대금"
            tcol = "티커" if "티커" in df.columns else df.columns[0]
            return {str(r[tcol]).zfill(6): float(r[col])
                    for _, r in df.iterrows() if col in df.columns}
        except Exception as e:
            last_err = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_SLEEP)
    print(f"  [warn] {date8} {market} 개인 수집 실패: {str(last_err)[:80]}")
    return None


def collect_date(conn, stock, date8):
    """하루치(KOSPI+KOSDAQ) 개인 순매수 → supply_demand UPDATE. 갱신 행수 반환."""
    ddash = _dash(date8)
    total = 0
    for mk in MARKETS:
        vals = _pull_individual(stock, date8, mk)
        if not vals:
            time.sleep(CALL_SLEEP)
            continue
        rows = [(v, ddash, code) for code, v in vals.items()]
        cur = conn.executemany(
            "UPDATE supply_demand SET individual_net_value=? WHERE date=? AND ticker=?",
            rows)
        total += cur.rowcount or 0
        time.sleep(CALL_SLEEP)
    return total


def run(lo=None, hi=None, limit=None):
    from pykrx import stock
    with closing(sqlite3.connect(DB_PATH, timeout=60)) as conn:
        dates = _target_dates(conn, lo, hi)
        if limit:
            dates = dates[-limit:]                   # 최근 N거래일(테스트)
        print(f"[individual] 백필 대상 {len(dates)}거래일 "
              f"({dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'})")
        if not dates:
            print("[individual] 채울 날짜 없음 — 이미 최신")
            return True
        t0 = time.time()
        ok = 0
        for i, d8 in enumerate(dates, 1):
            n = collect_date(conn, stock, d8)
            if n:
                ok += 1
            conn.commit()
            if i % 20 == 0 or i == len(dates):
                el = time.time() - t0
                eta = el / i * (len(dates) - i)
                print(f"[individual] {i}/{len(dates)} ({i/len(dates)*100:.0f}%) "
                      f"| 성공 {ok}일 | 경과 {el/60:.1f}분 | ETA {eta/60:.1f}분")
        print(f"[individual] 백필 종료 — 성공 {ok}/{len(dates)}일 ({(time.time()-t0)/60:.1f}분)")
        return True


def main():
    p = argparse.ArgumentParser(description="개인 순매수 supply_demand 백필")
    p.add_argument("--backfill", action="store_true", help="미채움 날짜 수집")
    p.add_argument("dates", nargs="*", help="[lo hi] 기간 지정(YYYYMMDD)")
    p.add_argument("--limit", type=int, default=None, help="최근 N거래일만(테스트)")
    args = p.parse_args()
    lo = hi = None
    if len(args.dates) == 2:
        lo, hi = args.dates
    elif len(args.dates) == 1:
        lo = hi = args.dates[0]
    ok = run(lo=lo, hi=hi, limit=args.limit)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
