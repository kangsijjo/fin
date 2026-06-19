"""
Top-1 paper-trade 추적기.

목적
----
실제 매매(auto_trader)는 top_n=3 + 변동성 사이징으로 분산 매수한다.
그러나 모델 알파의 raw 신호 검증을 위해서는 매일 가장 강한 단일 신호(Top-1)가
얼마나 맞히는지를 별도로 추적할 필요가 있다.

이 모듈은 매일:
  1) 전 섹터 통합 Top-1 (전체 종목 중 최고 확률 1개) → top1_global
  2) 섹터별 Top-1 (각 섹터당 최고 확률 1개)             → top1_sector

두 테이블에 'pending' 으로 기록하고, 5영업일 후 settle_pending() 이
T+1 시초가 진입 / T+6 시초가 청산 가격을 채워 P/L 을 확정한다.

paper-trade 이므로 KIS 계좌에는 어떤 주문도 가지 않는다. 자본/현금에 영향 없음.

진입/청산 룰 (백테스트와 동일)
- pick_date(T): 신호가 발생한 영업일
- entry_date: T 다음 영업일 시초가
- exit_date : entry_date 부터 5영업일 후 시초가
- pnl_pct   : (exit_price - entry_price) / entry_price
- target    : -1 (≤-2%), 0 (-2~+2%), +1 (≥+2%) — 학습 3분류와 일치

사용
----
    python track_top1.py record [--market korea|usa]
    python track_top1.py settle [--market korea|usa]
    python track_top1.py report
"""
import sqlite3
from datetime import datetime, timedelta
import pandas as pd

from src.config_db import get_connection
from src.logger import get_logger

logger = get_logger('top1_tracker')

HOLDING_DAYS = 5


def init_tables():
    """top1_global, top1_sector 스키마 생성."""
    conn = get_connection()
    for name, unique_cols in [
        ('top1_global', '(pick_date, market)'),
        ('top1_sector', '(pick_date, sector, market)'),
    ]:
        conn.execute(f'''
            CREATE TABLE IF NOT EXISTS {name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pick_date TEXT,
                market TEXT,
                ticker TEXT,
                name TEXT,
                sector TEXT,
                prob REAL,
                entry_date TEXT,
                entry_price REAL,
                exit_date TEXT,
                exit_price REAL,
                pnl_pct REAL,
                target_class INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                UNIQUE {unique_cols}
            )
        ''')
    conn.commit()
    conn.close()


# ────────────────────────── 1) record ──────────────────────────

def _scan_signals(market='korea'):
    """auto_trader.scan_all_sectors() 결과 중 해당 market 의 buy 시그널만 반환."""
    # 지연 import (auto_trader 자체가 무거우므로)
    from src.trader.auto_trader import scan_all_sectors
    signals = scan_all_sectors()
    return [s for s in signals
            if s.get('market') == market and s.get('signal') == 'buy']


def record_today_top1(market='korea', pick_date=None):
    """오늘의 Top-1 (전체 + 섹터별) 을 pending 으로 기록."""
    init_tables()

    pick_date = pick_date or datetime.now().strftime('%Y-%m-%d')
    signals = _scan_signals(market=market)
    if not signals:
        logger.info(f"[{pick_date}/{market}] 매수 시그널 없음 - 기록 스킵")
        return 0

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_connection()
    inserted = 0

    # (a) 전 섹터 통합 Top-1
    top = max(signals, key=lambda s: s['prob'])
    try:
        conn.execute(
            "INSERT OR IGNORE INTO top1_global "
            "(pick_date, market, ticker, name, sector, prob, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            [pick_date, market, top['ticker'], top['name'], top['sector'],
             float(top['prob']), now]
        )
        if conn.total_changes:
            inserted += 1
            logger.info(
                f"[global] {pick_date} {top['name']}({top['ticker']}) "
                f"prob={top['prob']:.3f} sector={top['sector']}"
            )
    except Exception as e:
        logger.error(f"global Top-1 INSERT 실패: {e}")

    # (b) 섹터별 Top-1
    by_sector = {}
    for s in signals:
        sec = s.get('sector', '')
        if sec not in by_sector or s['prob'] > by_sector[sec]['prob']:
            by_sector[sec] = s

    for sec, s in by_sector.items():
        try:
            conn.execute(
                "INSERT OR IGNORE INTO top1_sector "
                "(pick_date, market, ticker, name, sector, prob, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                [pick_date, market, s['ticker'], s['name'], sec,
                 float(s['prob']), now]
            )
            inserted += 1
        except Exception as e:
            logger.error(f"sector Top-1 INSERT 실패 {sec}: {e}")

    conn.commit()
    conn.close()
    logger.info(f"[{pick_date}/{market}] 기록 완료: 신규 {inserted}건 "
                f"(섹터 {len(by_sector)}개)")
    return inserted


# ────────────────────────── 2) settle ──────────────────────────

def _next_trading_dates(stock_table, ticker, base_date):
    """base_date 이후 첫 영업일과 그로부터 +HOLDING_DAYS 영업일을 반환.
    실제 종목 데이터에 있는 날짜 기준이므로 한국/미국 휴장일을 자동 처리한다.
    반환: (entry_date, exit_date) 또는 (None, None)."""
    conn = get_connection()
    try:
        df = pd.read_sql(
            f"SELECT date FROM {stock_table} "
            f"WHERE ticker=? AND date > ? ORDER BY date ASC LIMIT ?",
            conn, params=[ticker, base_date, HOLDING_DAYS + 1]
        )
    finally:
        conn.close()
    if len(df) < HOLDING_DAYS + 1:
        return None, None
    return df['date'].iloc[0], df['date'].iloc[HOLDING_DAYS]


def _lookup_open(stock_table, ticker, date):
    conn = get_connection()
    try:
        row = conn.execute(
            f"SELECT Open FROM {stock_table} WHERE ticker=? AND date=?",
            [ticker, date]
        ).fetchone()
    finally:
        conn.close()
    return float(row[0]) if row and row[0] else None


def _settle_one(conn, table, row, stock_table):
    pos_id, pick_date, market, ticker, name = row[:5]
    entry_date, exit_date = _next_trading_dates(stock_table, ticker, pick_date)
    if entry_date is None or exit_date is None:
        # 아직 5영업일 안 됨 → pending 유지
        return None

    entry_price = _lookup_open(stock_table, ticker, entry_date)
    exit_price  = _lookup_open(stock_table, ticker, exit_date)

    if entry_price is None or exit_price is None or entry_price <= 0:
        # 가격 데이터 누락 (상폐, 거래정지 등)
        conn.execute(
            f"UPDATE {table} SET status='failed', entry_date=?, exit_date=? WHERE id=?",
            [entry_date, exit_date, pos_id]
        )
        return 'failed'

    pnl = (exit_price - entry_price) / entry_price
    if pnl >= 0.02:
        target = 1
    elif pnl <= -0.02:
        target = -1
    else:
        target = 0

    conn.execute(
        f"UPDATE {table} "
        f"SET entry_date=?, entry_price=?, exit_date=?, exit_price=?, "
        f"    pnl_pct=?, target_class=?, status='settled' "
        f"WHERE id=?",
        [entry_date, entry_price, exit_date, exit_price, pnl, target, pos_id]
    )
    return 'settled'


def settle_pending(market='korea'):
    """status='pending' 행의 진입/청산 가격을 가능한 만큼 채움."""
    init_tables()
    stock_table = 'korea_stocks' if market == 'korea' else 'usa_stocks'
    conn = get_connection()
    settled = 0
    failed = 0
    still_pending = 0

    for table in ('top1_global', 'top1_sector'):
        rows = conn.execute(
            f"SELECT id, pick_date, market, ticker, name "
            f"FROM {table} WHERE status='pending' AND market=?",
            [market]
        ).fetchall()
        for row in rows:
            result = _settle_one(conn, table, row, stock_table)
            if result == 'settled':
                settled += 1
            elif result == 'failed':
                failed += 1
            else:
                still_pending += 1

    conn.commit()
    conn.close()
    logger.info(
        f"[{market}] settle 결과: settled={settled} failed={failed} "
        f"pending={still_pending}"
    )
    return settled, failed, still_pending


# ────────────────────────── 3) report ──────────────────────────

def _stats(df, label):
    print(f"\n[{label}] settled={len(df)}건")
    if df.empty:
        return
    avg = df['pnl_pct'].mean() * 100
    med = df['pnl_pct'].median() * 100
    std = df['pnl_pct'].std() * 100
    win = (df['pnl_pct'] > 0).mean() * 100
    hit2 = (df['target_class'] == 1).mean() * 100
    loss2 = (df['target_class'] == -1).mean() * 100
    cum = (1 + df['pnl_pct']).prod() - 1

    print(f"  평균 P/L     : {avg:+.3f}%")
    print(f"  중앙값       : {med:+.3f}%")
    print(f"  표준편차     : {std:.3f}%")
    print(f"  승률(>0)     : {win:.1f}%")
    print(f"  +2% hit율    : {hit2:.1f}%   (학습 타깃 양성 = target=+1)")
    print(f"  -2% loss율   : {loss2:.1f}%  (학습 타깃 음성 = target=-1)")
    print(f"  중립(-2~+2%) : {100 - hit2 - loss2:.1f}%")
    print(f"  누적복리수익 : {cum*100:+.2f}%")
    print(f"  최고 1건     : {df['pnl_pct'].max()*100:+.2f}%")
    print(f"  최악 1건     : {df['pnl_pct'].min()*100:+.2f}%")


def report():
    init_tables()
    conn = get_connection()
    try:
        g = pd.read_sql(
            "SELECT * FROM top1_global WHERE status='settled' ORDER BY pick_date",
            conn
        )
        s = pd.read_sql(
            "SELECT * FROM top1_sector WHERE status='settled' ORDER BY pick_date",
            conn
        )
        g_pend = pd.read_sql(
            "SELECT COUNT(*) AS c FROM top1_global WHERE status='pending'", conn
        )['c'][0]
        s_pend = pd.read_sql(
            "SELECT COUNT(*) AS c FROM top1_sector WHERE status='pending'", conn
        )['c'][0]
    finally:
        conn.close()

    print("=" * 70)
    print(" Top-1 paper-trade 통계")
    print("=" * 70)
    print(f"대기(pending): global={g_pend}건  sector={s_pend}건")
    _stats(g, "전 섹터 통합 Top-1")
    _stats(s, "섹터별 Top-1")

    if not g.empty:
        print(f"\n[최근 global 청산 5건]")
        recent = g.tail(5)
        for _, r in recent.iterrows():
            print(f"  {r['pick_date']} {r['name']}({r['ticker']}) "
                  f"{r['sector']:<8} prob={r['prob']:.3f} "
                  f"→ {r['pnl_pct']*100:+.2f}%")
    print("=" * 70)
