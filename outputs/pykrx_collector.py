"""
과거 3년 치 일봉 & 수급 데이터 수집기 (스텔스 & 좀비 모드 패치)

[2026-06-14] stock.db 연동:
  - CSV 저장 후 Stock_AI_Project/data/stock.db 의 korea_stocks / supply_demand 에 UPSERT.
  - Stock_AI_Project main_collector 가 같은 날짜 KOSDAQ 데이터를 보유하게 되어 중복 수집 자동 감소.
  - name/sector 는 건드리지 않음 (Stock_AI_Project 가 채운 값 보존).

[2026-06-14] 터미널 출력:
  - 수집 진행률: 인라인 게이지 (\r 덮어씀, 터미널 1줄 유지).
  - 경고·오류: print_event 로 새 줄 출력.
  - 비TTY(스케줄러) 환경에서는 자동으로 10개마다 줄 출력으로 폴백.
"""

# config 가 .env 로드 (KRX_ID/PW 환경변수) → pykrx 가 그 후 import 되어 인식
import config  # noqa: F401

from pykrx import stock
import pandas as pd
import os
import sqlite3
import time
import random
from datetime import datetime, timedelta

from progress import print_bar, print_event

DATA_DIR = "./macro_data/daily"
os.makedirs(DATA_DIR, exist_ok=True)

# stock.db 경로 후보 (환경변수 > 상대경로 > 절대경로)
_STOCK_DB_CANDIDATES = [
    os.getenv("STOCK_DB", ""),
    "../Stock_AI_Project/data/stock.db",
    "C:/fin/Stock_AI_Project/data/stock.db",
]


def _get_stock_db_path():
    for p in _STOCK_DB_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def _upsert_to_stock_db(df_merged, date_str):
    """
    수집 확정 데이터를 stock.db 에 반영.
      - korea_stocks : OHLCV UPSERT (name/sector 는 기존 값 보존)
      - supply_demand : 외국인/기관 순매수 INSERT OR REPLACE
    """
    db_path = _get_stock_db_path()
    if db_path is None:
        return

    date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    try:
        con = sqlite3.connect(db_path, timeout=30)
        cur = con.cursor()
        cur.execute("SELECT ticker FROM korea_stocks WHERE date=?", (date_fmt,))
        existing_tickers = {r[0] for r in cur.fetchall()}

        insert_rows, update_rows = [], []
        for _, r in df_merged.iterrows():
            ticker = str(r["code"]).zfill(6)
            chg = float(r.get("change_pct", 0) or 0) / 100
            vals_ohlcv = (
                int(r.get("open", 0) or 0),
                int(r.get("high", 0) or 0),
                int(r.get("low", 0) or 0),
                int(r.get("close", 0) or 0),
                int(r.get("volume", 0) or 0),
                chg,
            )
            if ticker in existing_tickers:
                update_rows.append(vals_ohlcv + (date_fmt, ticker))
            else:
                insert_rows.append((date_fmt, ticker) + vals_ohlcv)

        if update_rows:
            con.executemany("""
                UPDATE korea_stocks
                SET Open=?, High=?, Low=?, Close=?, Volume=?, Change=?
                WHERE date=? AND ticker=?
            """, update_rows)

        if insert_rows:
            con.executemany("""
                INSERT INTO korea_stocks (date, ticker, Open, High, Low, Close, Volume, Change)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, insert_rows)

        n_ks = len(update_rows) + len(insert_rows)

        sd_rows = []
        for _, r in df_merged.iterrows():
            ticker = str(r["code"]).zfill(6)
            f_net = r.get("foreign_net")
            i_net = r.get("inst_net")
            if f_net is None and i_net is None:
                continue
            sd_rows.append((
                date_fmt, ticker,
                float(f_net) if f_net is not None and not pd.isna(f_net) else None,
                float(i_net) if i_net is not None and not pd.isna(i_net) else None,
            ))

        if sd_rows:
            con.executemany("""
                INSERT INTO supply_demand (date, ticker, foreign_net_value, institution_net_value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date, ticker) DO UPDATE SET
                    foreign_net_value=excluded.foreign_net_value,
                    institution_net_value=excluded.institution_net_value
            """, sd_rows)

        con.commit()
        con.close()
        print_event(f"  [stock.db] {date_str} → korea_stocks {n_ks}종목 / supply_demand {len(sd_rows)}종목")

    except Exception as e:
        print_event(f"  [stock.db] UPSERT 실패 (파이프라인 무관): {e}")


def collect_macro_data(start_date_str, end_date_str, market="KOSDAQ"):
    print(f"[{market}] {start_date_str} ~ {end_date_str} 수집 시작 (스텔스 모드)")
    print("(KRX 로그인 경고는 무시하십시오.)\n")

    date_range = pd.bdate_range(start=start_date_str, end=end_date_str)
    total_days = len(date_range)

    today_str = datetime.today().strftime("%Y%m%d")
    now_hm = datetime.now().strftime("%H%M")

    _t0 = time.time()
    _n_fetched = 0
    _n_skip = 0

    for i, dt in enumerate(date_range):
        date_str = dt.strftime("%Y%m%d")
        save_path = f"{DATA_DIR}/{date_str}.csv"

        if os.path.exists(save_path) or os.path.exists(save_path + ".holiday"):
            _n_skip += 1
            print_bar(i + 1, total_days, _t0,
                      extra=f"신규 {_n_fetched} | 스킵 {_n_skip}")
            continue

        if date_str >= today_str and now_hm < "1540":
            print_event(f"  [{date_str}] 장마감 전 — 수집 보류")
            continue

        print_bar(i + 1, total_days, _t0,
                  extra=f"수집중 {date_str} | 신규 {_n_fetched}")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                time.sleep(random.uniform(1.5, 3.5))

                df_ohlcv = stock.get_market_ohlcv(date_str, market)

                if df_ohlcv is None or df_ohlcv.empty:
                    if date_str < today_str:
                        open(save_path + ".holiday", "w").close()
                    print_event(f"  [{date_str}] 휴장일 — 마커 기록")
                    break

                if (df_ohlcv["종가"] == 0).all():
                    if date_str < today_str:
                        open(save_path + ".holiday", "w").close()
                    print_event(f"  [{date_str}] 휴장일(전종목 0원) — 마커 기록")
                    break

                df_ohlcv = df_ohlcv.reset_index()
                df_ohlcv.rename(columns={
                    '티커': 'code', '시가': 'open', '고가': 'high',
                    '저가': 'low', '종가': 'close', '거래량': 'volume',
                    '거래대금': 'trading_value', '등락률': 'change_pct',
                }, inplace=True)

                time.sleep(random.uniform(0.5, 1.5))
                df_foreigner = stock.get_market_net_purchases_of_equities_by_ticker(
                    date_str, date_str, market, "외국인")
                if not df_foreigner.empty:
                    df_foreigner = df_foreigner.reset_index()[['티커', '순매수거래대금']]
                    df_foreigner.rename(columns={'티커': 'code', '순매수거래대금': 'foreign_net'}, inplace=True)
                else:
                    df_foreigner = pd.DataFrame(columns=['code', 'foreign_net'])

                time.sleep(random.uniform(0.5, 1.5))
                df_inst = stock.get_market_net_purchases_of_equities_by_ticker(
                    date_str, date_str, market, "기관합계")
                if not df_inst.empty:
                    df_inst = df_inst.reset_index()[['티커', '순매수거래대금']]
                    df_inst.rename(columns={'티커': 'code', '순매수거래대금': 'inst_net'}, inplace=True)
                else:
                    df_inst = pd.DataFrame(columns=['code', 'inst_net'])

                df_merged = pd.merge(df_ohlcv, df_foreigner, on='code', how='left')
                df_merged = pd.merge(df_merged, df_inst, on='code', how='left')
                df_merged['date'] = date_str

                df_merged.to_csv(save_path, index=False, encoding="utf-8-sig")
                _n_fetched += 1

                # stock.db 동기
                _upsert_to_stock_db(df_merged, date_str)

                print_bar(i + 1, total_days, _t0,
                          extra=f"신규 {_n_fetched} | 스킵 {_n_skip}")
                break

            except Exception as e:
                error_msg = str(e)
                if "Expecting value" in error_msg or "None of" in error_msg:
                    print_event(f"  [{date_str}] KRX 방화벽 감지 — 15초 대기 ({attempt+1}/{max_retries})")
                    time.sleep(15)
                else:
                    print_event(f"  [{date_str}] 오류: {e}")
                    break

    print()  # 인라인 게이지 완료 후 줄바꿈
    print(f"[완료] 신규 {_n_fetched}일 수집 / {_n_skip}일 스킵")


if __name__ == "__main__":
    import sys
    years = 3
    if len(sys.argv) > 1:
        try:
            years = int(sys.argv[1])
        except ValueError:
            print(f"[warn] 년수 인자 '{sys.argv[1]}' 무시, 기본 3년")
    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=365 * years)
    print(f"[기간] 최근 {years}년 ({start_dt:%Y%m%d} ~ {end_dt:%Y%m%d})")
    collect_macro_data(start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"))
