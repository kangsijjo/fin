# -*- coding: utf-8 -*-
"""
foreign_ratio_collector.py — 외국인 지분율(보유비중) 수집기 (2026-07-21 신설)

목적: pykrx 외국인 한도소진율 엔드포인트로 '외국인 지분율'을 전종목 일배치 수집해
      stock.db 의 foreign_ratio 테이블에 누적. '외국인 비중 상승 + 가격확인' 전략의
      백테스트/라이브 신호 소스.

설계 근거(2026-07-21 실증):
  - stock.get_exhaustion_rates_of_foreign_investment(date, market) = 특정일 전종목 지분율.
    KRX 로그인 필요(KRX_ID/KRX_PW, .env 에 설정됨). 날짜당 KOSPI+KOSDAQ 2콜(≈2,764종목).
  - 지분율은 하루 0.0X%씩 느리게 변동 → 실시간 조회 무의미, 전일확정→익일 판정이 정답.
  - KRX 서버 빈응답(JSONDecodeError) 잦음 → 재시도/스로틀 하드닝 필수(오늘 실측).
  - 백테스트 가격 데이터가 2018-06~ 라 그 범위만 수집해도 백테스트 구간 100% 커버.

멱등·재개: (date,ticker) PK + 날짜별 commit. 세션 만료/크래시 후 재실행하면
      '이미 수집한 날짜'는 건너뛰고 빈 곳만 채운다(장시간 잡 안전).

사용:
  python foreign_ratio_collector.py --backfill              # 일봉 달력 전체(2018-06~)
  python foreign_ratio_collector.py --backfill 20240101 20241231
  python foreign_ratio_collector.py                         # 최신 거래일 1일치만(일 누적용)
"""
import os
import sys
import glob
import time
import sqlite3
from datetime import datetime

# ── 경로 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
DB_PATH  = os.path.join(_HERE, "data", "stock.db")
DAILY_DIR = os.path.join(_REPO, "outputs", "macro_data", "daily")  # 백테스트 거래일 달력

# ── KRX 로그인 자격증명 주입(안 하면 지분율 엔드포인트가 "KRX 로그인 실패") ──
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_HERE, ".env"))
except Exception:
    pass

# 콘솔 인코딩 방탄(cp949 콘솔에서 진행문자/한글 print 크래시 방지 — 타 수집기와 동일)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MARKETS = ("KOSPI", "KOSDAQ")
CALL_SLEEP = 0.8       # 콜 간 스로틀(빈응답 예방)
RETRY_SLEEP = 2.5      # 실패 재시도 대기
RETRIES = 4


def ensure_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS foreign_ratio (
            date          TEXT NOT NULL,   -- YYYYMMDD (백테스트 df 와 동일 포맷)
            ticker        TEXT NOT NULL,   -- 6자리(영문 포함 가능, 그대로 저장)
            holding_ratio REAL,            -- 외국인 지분율(%)
            held_shares   INTEGER,         -- 외국인 보유수량
            listed_shares INTEGER,         -- 상장주식수
            PRIMARY KEY (date, ticker)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_fr_date ON foreign_ratio(date)")
    con.commit()


def collected_dates(con):
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT date FROM foreign_ratio")}
    except Exception:
        return set()


def trading_calendar():
    """백테스트가 쓰는 일봉 CSV 파일명(YYYYMMDD) = 정확한 거래일 달력."""
    fs = sorted(glob.glob(os.path.join(DAILY_DIR, "*.csv")))
    return [os.path.basename(f).rsplit(".", 1)[0] for f in fs
            if os.path.basename(f)[:8].isdigit()]


def _pull(stock, date, market):
    """(date, market) 전종목 지분율 — 빈응답 재시도. 실패 시 None."""
    last = None
    for i in range(RETRIES):
        try:
            df = stock.get_exhaustion_rates_of_foreign_investment(date, market)
            if df is not None and len(df):
                return df
        except Exception as e:
            last = e
        time.sleep(RETRY_SLEEP)
    print(f"    [warn] {date} {market} 수집 실패({type(last).__name__ if last else '빈응답'})")
    return None


def _rows_from(df, date):
    """pykrx df(index=티커) → (date,ticker,ratio,held,listed) 행 리스트."""
    rows = []
    df = df.reset_index()
    # 컬럼명 방어(버전별 '한도소진율/한도소진률' 등 표기차) — 필요한 3개만 이름으로 선택
    tick_col = df.columns[0]  # reset_index 후 첫 컬럼 = 티커
    def col(name):
        return df[name] if name in df.columns else None
    ratio = col("지분율")
    held  = col("보유수량")
    listed = col("상장주식수")
    if ratio is None:
        return rows
    for i in range(len(df)):
        tk = str(df[tick_col].iloc[i]).strip()
        if not tk:
            continue
        try:
            r = float(ratio.iloc[i])
        except (ValueError, TypeError):
            continue
        h = held.iloc[i] if held is not None else None
        l = listed.iloc[i] if listed is not None else None
        try: h = int(h) if h == h and h is not None else None
        except (ValueError, TypeError): h = None
        try: l = int(l) if l == l and l is not None else None
        except (ValueError, TypeError): l = None
        rows.append((date, tk, r, h, l))
    return rows


def collect_date(con, stock, date):
    """하루치(KOSPI+KOSDAQ) 수집·저장. 저장 종목수 반환(0=실패로 간주, 재실행 대상)."""
    all_rows = []
    for mk in MARKETS:
        df = _pull(stock, date, mk)
        if df is not None:
            all_rows += _rows_from(df, date)
        time.sleep(CALL_SLEEP)
    if not all_rows:
        return 0
    con.executemany(
        "INSERT OR REPLACE INTO foreign_ratio "
        "(date,ticker,holding_ratio,held_shares,listed_shares) VALUES (?,?,?,?,?)",
        all_rows)
    con.commit()   # 날짜별 커밋 = 재개 지점(장시간 잡 안전)
    return len(all_rows)


def main():
    args = sys.argv[1:]
    from pykrx import stock

    con = sqlite3.connect(DB_PATH)
    ensure_table(con)

    if "--backfill" in args:
        rest = [a for a in args if a != "--backfill"]
        cal = trading_calendar()
        if len(rest) == 2:
            lo, hi = rest
            cal = [d for d in cal if lo <= d <= hi]
        done = collected_dates(con)
        todo = [d for d in cal if d not in done]
        print(f"[foreign_ratio] 백필 대상 {len(todo)}일 "
              f"(달력 {len(cal)}일 중 기수집 {len(cal)-len(todo)}일 건너뜀)")
        t0 = time.time()
        ok = 0
        for i, d in enumerate(todo, 1):
            n = collect_date(con, stock, d)
            if n:
                ok += 1
            if i % 20 == 0 or i == len(todo):
                el = time.time() - t0
                eta = el / i * (len(todo) - i)
                print(f"  [{i}/{len(todo)}] {d}  누적성공 {ok}일  "
                      f"경과 {el/60:.1f}분  ETA {eta/60:.1f}분")
        print(f"[foreign_ratio] 백필 종료 — 성공 {ok}/{len(todo)}일 "
              f"({(time.time()-t0)/60:.1f}분). 실패분은 재실행 시 자동 재시도.")
    else:
        # 최신 거래일 1일치(일 누적)
        cal = trading_calendar()
        if not cal:
            print("[foreign_ratio] 일봉 달력 없음"); con.close(); return
        d = cal[-1]
        if d in collected_dates(con):
            print(f"[foreign_ratio] {d} 이미 수집됨 — 스킵")
        else:
            n = collect_date(con, stock, d)
            print(f"[foreign_ratio] {d} 수집 {'완료 '+str(n)+'종목' if n else '실패(재실행 요망)'}")
    con.close()


if __name__ == "__main__":
    main()
