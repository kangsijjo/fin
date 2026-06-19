import FinanceDataReader as fdr
import pandas as pd
import sqlite3
import time
from datetime import datetime
from src.config_db import get_db_path
from src.logger import get_logger
from src.config_db import get_connection

logger = get_logger('macro')
DB_PATH = get_db_path()

# 네트워크 재시도 (main_collector 와 동일 정책)
_FETCH_RETRIES = 3
_FETCH_WAIT_SEC = 2


def _fetch_with_retry(ticker, start, end, name):
    """fdr.DataReader 를 재시도와 함께 호출. 최종 실패 시 None."""
    for attempt in range(_FETCH_RETRIES):
        try:
            return fdr.DataReader(ticker, start, end)
        except Exception as e:
            logger.warning(f"{name}({ticker}) 시도 {attempt+1}/{_FETCH_RETRIES} 실패: {e}")
            if attempt < _FETCH_RETRIES - 1:
                time.sleep(_FETCH_WAIT_SEC)
    logger.error(f"{name}({ticker}) {_FETCH_RETRIES}회 재시도 모두 실패")
    return None



def collect_macro(start='2015-01-01'):
    """거시지표 수집.

    결측치 정책 (2026-06-13): 이 수집기는 별도 공백 검사가 필요 없다.
    지표가 6개뿐이라 매번 start~오늘 전체 구간을 다시 받아
    INSERT OR REPLACE 하므로 구조적으로 공백이 생길 수 없음 (자가치유).
    """
    end = datetime.today().strftime('%Y-%m-%d')
    
    targets = {
        'NASDAQ':   '^IXIC',    # 나스닥
        'KRW_USD':  'USD/KRW',  # 환율
        'SOX':      '^SOX',     # 필라델피아 반도체
        'SP500':    '^GSPC',    # S&P500
        'KOSPI':    'KS11',     # 코스피
        'VIX':      '^VIX',     # 공포지수
    }
    
    conn = get_connection()
    
    # 테이블 생성
    conn.execute('''
        CREATE TABLE IF NOT EXISTS macro_indicators (
            date TEXT,
            indicator TEXT,
            close REAL,
            change_pct REAL,
            PRIMARY KEY (date, indicator)
        )
    ''')
    conn.commit()
    
    for name, ticker in targets.items():
        try:
            logger.info(f"수집 중: {name} ({ticker})")
            df = _fetch_with_retry(ticker, start, end, name)

            if df is None or len(df) == 0:
                logger.warning(f"데이터 없음: {name}")
                continue
            
            df.index.name = 'date'
            df.reset_index(inplace=True)
            df['indicator'] = name
            df['close'] = df['Close']
            df['change_pct'] = df['Close'].pct_change() * 100
            df = df[['date', 'indicator', 'close', 'change_pct']].dropna()
            
            # 중복 제거 후 저장
            for _, row in df.iterrows():
                conn.execute('''
                    INSERT OR REPLACE INTO macro_indicators 
                    (date, indicator, close, change_pct)
                    VALUES (?, ?, ?, ?)
                ''', [str(row['date'])[:10], row['indicator'],
                      row['close'], row['change_pct']])
            
            conn.commit()
            logger.info(f"저장 완료: {name} ({len(df)}행)")
            
        except Exception as e:
            logger.error(f"실패 {name}: {e}", exc_info=True)
            continue
    
    conn.close()
    logger.info("거시지표 수집 완료!")

def load_macro_features(date):
    """특정 날짜 거시지표 피처 반환"""
    conn = get_connection()
    try:
        df = pd.read_sql(
            "SELECT indicator, change_pct FROM macro_indicators WHERE date<=? ORDER BY date DESC",
            conn, params=[date]
        )
        conn.close()
        
        if df.empty:
            return {}
        
        # 최신값만 가져오기
        latest = df.groupby('indicator').first()
        return latest['change_pct'].to_dict()
    except:
        conn.close()
        return {}

if __name__ == "__main__":
    collect_macro()