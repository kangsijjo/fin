import sqlite3
import os
import pandas as pd
from contextlib import closing

from src.config_db import get_db_path
DB_PATH = get_db_path()

from src.config_db import get_connection

def _ensure_table(conn, table):
    """테이블 + (ticker,date) 유니크 인덱스 보장 (원자적 upsert를 위해 필요)"""
    conn.execute(f'''
        CREATE TABLE IF NOT EXISTS {table} (
            date TEXT, ticker TEXT, name TEXT, sector TEXT,
            Open REAL, High REAL, Low REAL, Close REAL, Volume REAL,
            "Adj Close" REAL,
            "Change" REAL,
            UNIQUE(ticker, date)
        )
    ''')
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS idx_{table}_ticker_date ON {table}(ticker, date)'
    )

# [2026-11-02 §92] 다시 받아 덮어쓸 가격 칸(이름·업종 등 나머지 칸은 그대로)
PRICE_COLS = ("Open", "High", "Low", "Close", "Volume", "Change")


def save_stock(df, market='korea', refresh_dates=()):
    """
    주식 데이터 저장.
    - read→diff→insert 의 race condition 제거 → INSERT OR IGNORE 원자 upsert
    - 예외 발생해도 커넥션 항상 닫힘 (with 컨텍스트)
    - Python 3.12+ 에서 sqlite3 가 Timestamp/datetime 의 기본 adapter 를
      제거했기 때문에, datetime 타입 컬럼은 미리 ISO 문자열로 변환한다.
    - [§92] refresh_dates 의 날짜 행은 이미 있어도 가격 칸(PRICE_COLS)을 새 값으로 덮어쓴다(값이 다를 때만) —
      장중·마감 직후에 들어간 미완성 일봉을 다음 수집이 바로잡게. 그 밖의 날짜는 종전처럼 있는 행을 건드리지 않음
      (과거 행을 다시 받은 값으로 덮으면 액면분할 등 수정주가가 섞일 수 있어서).
    반환: (새로 넣은 행 수, 덮어쓴 행 수)
    """
    if df.empty:
        return 0, 0

    table = 'korea_stocks' if market == 'korea' else 'usa_stocks'

    # 1) datetime/Timestamp 컬럼을 sqlite 바인딩 가능한 문자열로 정규화
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime('%Y-%m-%d')

    # 2) 남아 있는 NaN/NaT 등 결측치는 sqlite NULL 로 매핑
    df = df.astype(object).where(df.notna(), None)

    # to_sql 은 if_exists='append' + UNIQUE 제약을 만나면 전체 실패하므로,
    # executemany + INSERT OR IGNORE 패턴으로 행 단위 무시 처리한다.
    cols = list(df.columns)
    placeholders = ','.join(['?'] * len(cols))
    col_list = ','.join(f'"{c}"' for c in cols)
    sql = f'INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})'

    rows = [tuple(x) for x in df[cols].itertuples(index=False, name=None)]

    refresh = {str(d)[:10] for d in (refresh_dates or ()) if d}
    upd_cols = [c for c in PRICE_COLS if c in cols]
    upd_rows = []
    if refresh and upd_cols and 'date' in cols and 'ticker' in cols:
        di, ti = cols.index('date'), cols.index('ticker')
        ci = [cols.index(c) for c in upd_cols]
        for r in rows:
            if str(r[di])[:10] in refresh:
                vals = [r[i] for i in ci]
                upd_rows.append(tuple(vals) + (r[ti], r[di]) + tuple(vals))
    sql_u = (f'UPDATE {table} SET ' + ','.join(f'"{c}"=?' for c in upd_cols) +
             ' WHERE ticker=? AND date=? AND (' + ' OR '.join(f'"{c}" IS NOT ?' for c in upd_cols) + ')')

    with closing(get_connection()) as conn:
        _ensure_table(conn, table)
        cur = conn.executemany(sql, rows)
        inserted = cur.rowcount if cur.rowcount is not None else 0
        changed = 0
        if upd_rows:
            cu = conn.executemany(sql_u, upd_rows)
            changed = cu.rowcount if cu.rowcount is not None and cu.rowcount > 0 else 0
        conn.commit()

    if inserted > 0 or changed > 0:
        print(f"  {inserted}행 저장완료" + (f" · {changed}행 덮어씀(미완성 일봉 치유)" if changed else ""))
    elif not refresh:
        print(f"  이미 최신 데이터")
    return inserted, changed

def load_sector(sector, market='korea'):
    """섹터 데이터 불러오기"""
    # config의 구조가 변경되었으므로 KOREA_SECTORS와 USA_SECTORS를 활용하도록 업데이트
    from .config import KOREA_SECTORS, USA_SECTORS
    
    tickers = []
    if market == 'korea' and sector in KOREA_SECTORS:
        # 만약 config.yaml에 종목 리스트가 딕셔너리로 관리된다면 여기에 맞게 수정됨
        tickers = KOREA_SECTORS[sector] 
    elif market == 'usa' and sector in USA_SECTORS:
        tickers = USA_SECTORS[sector]
        
    table = 'korea_stocks' if market == 'korea' else 'usa_stocks'
    
    conn = get_connection()
    if tickers:
        query = f"""
            SELECT * FROM {table} 
            WHERE ticker IN ({','.join(['?']*len(tickers))})
            ORDER BY date
        """
        df = pd.read_sql(query, conn, params=tickers)
    else:
        # 섹터 이름으로 직접 검색
        query = f"SELECT * FROM {table} WHERE sector=? ORDER BY date"
        df = pd.read_sql(query, conn, params=[sector])
        
    conn.close()
    return df