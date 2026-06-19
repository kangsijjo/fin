import os
import sqlite3

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT_DIR, 'data', 'stock.db')

def get_db_path():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return DB_PATH

def get_connection():
    """WAL 모드로 연결 (쓰기 중에도 읽기 가능)"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def write_retry(fn, retries=5, wait=5.0, logger=None):
    """sqlite 'database is locked' 시 대기 후 재시도하는 래퍼.

    WAL 이어도 쓰기 잠금은 동시에 1개. 다른 수집 프로세스가 긴 트랜잭션을
    잡고 있으면 timeout(30s) 초과 후 OperationalError 가 난다.
    수집 작업은 멱등(INSERT OR IGNORE)이므로 기다렸다 재시도하면 안전.

    사용: write_retry(lambda: conn.executemany(sql, rows), logger=logger)
    """
    import time as _time
    for attempt in range(retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < retries:
                if logger:
                    logger.warning(
                        f"DB locked - {wait:.0f}s 후 재시도 ({attempt+1}/{retries})")
                _time.sleep(wait)
                continue
            raise