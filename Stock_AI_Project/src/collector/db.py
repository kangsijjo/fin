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

def save_stock(df, market='korea'):
    """
    주식 데이터 저장.
    - read→diff→insert 의 race condition 제거 → INSERT OR IGNORE 원자 upsert
    - 예외 발생해도 커넥션 항상 닫힘 (with 컨텍스트)
    - Python 3.12+ 에서 sqlite3 가 Timestamp/datetime 의 기본 adapter 를
      제거했기 때문에, datetime 타입 컬럼은 미리 ISO 문자열로 변환한다.
    """
    if df.empty:
        return

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

    with closing(get_connection()) as conn:
        _ensure_table(conn, table)
        cur = conn.executemany(sql, rows)
        inserted = cur.rowcount if cur.rowcount is not None else 0
        conn.commit()

    if inserted > 0:
        print(f"  {inserted}행 저장완료")
    else:
        print(f"  이미 최신 데이터")

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