import pandas as pd
import pandas_ta_classic as ta
import sqlite3
import os
import time
from src.config_db import get_db_path, get_connection
from src.logger import get_logger

logger = get_logger('indicators')
DB_PATH = get_db_path()

def add_indicators(df, macro_df=None, news_df=None, news_weight=0.5, sd_df=None,
                   credit_df=None, lending_df=None):
    """전체 피처 계산.
    기본 21개 (기술 15 + 거시 5 + 뉴스 1) + 수급 3 (foreign_net_5d, inst_net_5d, short_ratio)
    + 키움 2 (credit_ratio, lending_chg_5d) = 최대 26개.
    sd_df/credit_df/lending_df 가 None/empty 면 해당 피처는 0 으로 채워 호환 유지.
    """
    df = df.copy()
    # date 컬럼이 'YYYY-MM-DD' 와 'YYYY-MM-DD HH:MM:SS' 두 포맷으로 섞여 들어와도
    # 안전하게 처리되도록 앞 10자만 유지하여 통일한다.
    df['date'] = df['date'].astype(str).str[:10]
    df = df.sort_values('date').reset_index(drop=True)

    # === 기술지표 15개 ===
    # 1-3. 이동평균 이격도
    df['ma5']  = ta.sma(df['Close'], length=5)
    df['ma20'] = ta.sma(df['Close'], length=20)
    df['ma60'] = ta.sma(df['Close'], length=60)
    df['ma5_ratio']  = df['Close'] / df['ma5']
    df['ma20_ratio'] = df['Close'] / df['ma20']
    df['ma60_ratio'] = df['Close'] / df['ma60']

    # 4-6. 변화율
    df['return_1d']  = df['Close'].pct_change(1)
    df['return_5d']  = df['Close'].pct_change(5)
    df['return_20d'] = df['Close'].pct_change(20)

    # 7. RSI
    df['rsi'] = ta.rsi(df['Close'], length=14)

    # 8-10. MACD
    macd = ta.macd(df['Close'])
    if macd is not None:
        df['macd']        = macd['MACD_12_26_9']
        df['macd_signal'] = macd['MACDs_12_26_9']
        df['macd_hist']   = macd['MACDh_12_26_9']
    else:
        df['macd'] = df['macd_signal'] = df['macd_hist'] = 0.0

    # 11-12. 볼린저 밴드
    # pandas-ta-classic 반환 컬럼 순서: [BBL(Lower), BBM(Mid), BBU(Upper), BBB, BBP]
    # 기존 코드는 [0]=BBL 을 bb_upper 로, [2]=BBU 를 bb_lower 로 잘못 라벨링했음.
    # 라벨 정상화 (leakage 는 아니지만 의미가 거꾸로였음).
    bb = ta.bbands(df['Close'], length=20)
    if bb is not None:
        bb_cols = bb.columns.tolist()
        df['bb_lower'] = bb[bb_cols[0]]   # BBL (정상)
        df['bb_upper'] = bb[bb_cols[2]]   # BBU (정상)
    else:
        df['bb_upper'] = df['bb_lower'] = 0.0

    # 13. 변동성
    df['volatility'] = df['return_1d'].rolling(20).std()

    # 14. 거래량 비율
    df['vol_ratio'] = df['Volume'] / df['Volume'].rolling(20).mean()

    # 15. 거래량 급증
    df['vol_spike'] = (df['Volume'] > df['Volume'].rolling(20).mean() * 2).astype(int)

    # === 거시지표 5개 ===
    if macro_df is not None and not macro_df.empty:
        df['date'] = pd.to_datetime(df['date'])
        macro_df['date'] = pd.to_datetime(macro_df['date'])
        macro_pivot = macro_df.pivot(
            index='date', columns='indicator', values='change_pct'
        ).sort_index().ffill()

        # ── lookahead bias 차단 ──
        # NASDAQ/SOX/VIX/SP500 의 t일 마감은 한국 시간 t+1일 새벽에 확정된다.
        # 따라서 한국 t일 거래일의 features 에 미국 t일 macro 를 그대로 매핑하면
        # 미래 정보 누수. shift(1) 로 직전 거래일 macro 만 사용한다.
        # KOSPI/KRW_USD 도 동일하게 1일 lag (보수적 통일).
        macro_pivot = macro_pivot.shift(1)

        for col, feat in [
            ('NASDAQ',  'nasdaq_chg'),
            ('SOX',     'sox_chg'),
            ('KRW_USD', 'krw_usd_chg'),
            ('VIX',     'vix_chg'),
            ('KOSPI',   'kospi_chg'),
        ]:
            if col in macro_pivot.columns:
                df[feat] = df['date'].map(macro_pivot[col]).ffill().fillna(0.0)
            else:
                df[feat] = 0.0
    else:
        for feat in ['nasdaq_chg', 'sox_chg', 'krw_usd_chg', 'vix_chg', 'kospi_chg']:
            df[feat] = 0.0

    # === 뉴스 감성 1개 ===
    df['news_sentiment'] = 0.0
    if news_df is not None and not news_df.empty:
        try:
            news_df = news_df.copy()
            news_df['pubDate'] = pd.to_datetime(news_df['pubDate'], errors='coerce')
            news_df = news_df.dropna(subset=['pubDate'])
            news_df['date'] = news_df['pubDate'].dt.normalize()

            daily_sentiment = news_df.groupby(['ticker', 'date'])['sentiment'].mean()

            def map_sentiment(row):
                try:
                    return daily_sentiment.loc[(row['ticker'], row['date'])]
                except:
                    return 0.0

            df['date'] = pd.to_datetime(df['date'])
            df['news_sentiment'] = df.apply(map_sentiment, axis=1) * news_weight
        except Exception as e:
            logger.warning(f"뉴스 감성 매핑 실패: {e}")
            df['news_sentiment'] = 0.0

    # === 수급 3개 (KRX 외국인/기관 + 공매도) ===
    # sd_df 가 비어 있으면 0 으로 채움 (백필 안 된 종목/기간 호환).
    sd_cols = ['foreign_net_5d', 'inst_net_5d', 'short_ratio']
    if sd_df is not None and not sd_df.empty:
        try:
            # 이 add_indicators 호출은 한 ticker 의 데이터.
            # 그 ticker 의 수급만 뽑아 시간순 정렬 후 5일 누적.
            ticker_id = df['ticker'].iloc[0] if 'ticker' in df.columns else None
            if ticker_id is not None:
                ts = sd_df[sd_df['ticker'] == ticker_id].copy()
            else:
                ts = sd_df.copy()
            if not ts.empty:
                ts['date'] = pd.to_datetime(ts['date'])
                ts = ts.sort_values('date')
                ts['foreign_net_5d'] = ts['foreign_net_value'].rolling(5, min_periods=1).sum()
                ts['inst_net_5d']    = ts['institution_net_value'].rolling(5, min_periods=1).sum()
                ts['short_ratio']    = ts['short_balance_ratio']
                df['date'] = pd.to_datetime(df['date'])
                df = df.merge(
                    ts[['date', 'foreign_net_5d', 'inst_net_5d', 'short_ratio']],
                    on='date', how='left'
                )
                # short_ratio 는 현재 항상 NULL(Phase 2 후속)이라 merge 후 object dtype 으로
                # 추론되어 fillna 시 암묵적 다운캐스팅 경고가 뜬다.
                # pd.to_numeric 으로 명시적 float 변환 후 fillna → 경고 제거.
                for c in sd_cols:
                    df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0.0)
            else:
                for c in sd_cols:
                    df[c] = 0.0
        except Exception as e:
            logger.warning(f"수급 피처 매핑 실패(0 처리): {e}")
            for c in sd_cols:
                df[c] = 0.0
    else:
        for c in sd_cols:
            df[c] = 0.0

    # === 키움 신용/대차 2개 (ka10013 / ka20068) ===
    # credit_ratio   : 신용잔고율(%) — 개인 레버리지 과열 지표
    # lending_chg_5d : 대차잔고 5일 변화율 — 공매도 압력 프록시 (scale-free)
    ticker_id = df['ticker'].iloc[0] if 'ticker' in df.columns else None

    def _merge_kiwoom(src_df, value_col, feat_name, transform=None):
        df_local_col_added = False
        if src_df is not None and not src_df.empty and ticker_id is not None:
            try:
                ts = src_df[src_df['ticker'] == ticker_id].copy()
                if not ts.empty:
                    ts['date'] = pd.to_datetime(ts['date'])
                    ts = ts.sort_values('date')
                    if transform is not None:
                        ts[feat_name] = transform(ts)
                    else:
                        ts[feat_name] = ts[value_col]
                    nonlocal_df = df  # 가독성용 별칭 (동일 객체)
                    nonlocal_df['date'] = pd.to_datetime(nonlocal_df['date'])
                    merged = nonlocal_df.merge(
                        ts[['date', feat_name]], on='date', how='left')
                    merged[feat_name] = pd.to_numeric(
                        merged[feat_name], errors='coerce').fillna(0.0)
                    return merged, True
            except Exception as e:
                logger.warning(f"{feat_name} 매핑 실패(0 처리): {e}")
        return df, df_local_col_added

    df, ok = _merge_kiwoom(credit_df, 'credit_remain_rt', 'credit_ratio')
    if not ok and 'credit_ratio' not in df.columns:
        df['credit_ratio'] = 0.0

    df, ok = _merge_kiwoom(
        lending_df, None, 'lending_chg_5d',
        transform=lambda ts: ts['lending_balance'].pct_change(5)
                               .replace([float('inf'), float('-inf')], 0.0))
    if not ok and 'lending_chg_5d' not in df.columns:
        df['lending_chg_5d'] = 0.0

    # FinRL 환경을 위해 날짜 포맷을 다시 문자열로 통일
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    return df

# 전체 피처 컬럼 (순서 중요!)
FEATURE_COLS = [
    'return_1d', 'return_5d', 'return_20d',
    'ma5_ratio', 'ma20_ratio', 'ma60_ratio',
    'rsi', 'vol_ratio', 'volatility',
    'macd', 'macd_signal', 'macd_hist',
    'bb_upper', 'bb_lower', 'vol_spike',
    'nasdaq_chg', 'sox_chg', 'krw_usd_chg',
    'vix_chg', 'kospi_chg',
    'news_sentiment',
    # 신규: KRX 수급 (Phase 2)
    'foreign_net_5d', 'inst_net_5d', 'short_ratio',
    # 신규: 키움 신용/대차 (2026-06-12, Phase 3)
    'credit_ratio', 'lending_chg_5d',
]

def load_macro():
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT * FROM macro_indicators", conn)
    except:
        df = pd.DataFrame()
    conn.close()
    return df

def load_supply_demand(market='korea'):
    """KRX 외국인/기관 + 공매도 잔량 데이터 로드. 한국만 지원."""
    if market != 'korea':
        return pd.DataFrame()
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT * FROM supply_demand", conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


def load_kiwoom_extra(market='korea'):
    """키움 신용잔고/대차거래 데이터 로드. 한국만 지원.
    반환: (credit_df, lending_df) — 테이블 없으면 빈 DataFrame."""
    if market != 'korea':
        return pd.DataFrame(), pd.DataFrame()
    conn = get_connection()
    try:
        credit_df = pd.read_sql("SELECT * FROM credit_balance", conn)
    except Exception:
        credit_df = pd.DataFrame()
    try:
        lending_df = pd.read_sql("SELECT * FROM stock_lending", conn)
    except Exception:
        lending_df = pd.DataFrame()
    conn.close()
    return credit_df, lending_df


def load_news(sector, market='korea'):
    conn = get_connection()
    table = 'korea_stocks' if market == 'korea' else 'usa_stocks'
    try:
        tickers = pd.read_sql(
            f"SELECT DISTINCT ticker FROM {table} WHERE sector=?",
            conn, params=[sector]
        )['ticker'].tolist()
        
        if not tickers:
            conn.close()
            return pd.DataFrame()
            
        placeholders = ','.join(['?'] * len(tickers))
        news_df = pd.read_sql(
            f"SELECT ticker, pubDate, sentiment FROM news WHERE ticker IN ({placeholders})",
            conn, params=tickers
        )
    except:
        news_df = pd.DataFrame()
    conn.close()
    return news_df

def _ensure_indicator_schema(conn, ind_table):
    """기존 indicators 테이블에 누락된 FEATURE_COLS 컬럼을 ALTER TABLE 로 추가.

    배경: FEATURE_COLS 가 21개 → 24개로 확장됐을 때(수급 3개 추가),
    이미 만들어진 indicators 테이블엔 새 컬럼이 없어
    to_sql(if_exists='append') 가 "no such column" 으로 실패한다.
    이 헬퍼가 부족한 컬럼만 채워 멱등적으로 마이그레이션.

    테이블이 아직 없는 경우는 to_sql 이 알아서 24컬럼 풀스키마로 만들므로 무동작.
    """
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({ind_table})").fetchall()]
    except Exception:
        cols = []
    if not cols:
        return  # 테이블 없음 → to_sql 이 createTable 로 생성

    added = []
    for c in FEATURE_COLS:
        if c not in cols:
            try:
                conn.execute(f"ALTER TABLE {ind_table} ADD COLUMN {c} REAL")
                added.append(c)
            except Exception as e:
                logger.warning(f"{ind_table} 컬럼 추가 실패 {c}: {e}")
    if added:
        conn.commit()
        logger.info(f"{ind_table} 스키마 마이그레이션: {added} 추가")


def process_sector(sector, market='korea'):
    """섹터 전체 종목 24개 피처를 메모리에서 일괄(Batch) 계산 후 DB에 한 번에 저장 (FinRL 표준화)"""
    try:
        from src.collector.config import NEWS_WEIGHT
    except ImportError:
        NEWS_WEIGHT = 0.5

    conn = get_connection()
    stock_table = 'korea_stocks' if market == 'korea' else 'usa_stocks'
    ind_table = 'korea_indicators' if market == 'korea' else 'usa_indicators'

    # 옛 스키마(21개 컬럼) 테이블에 신규 컬럼 자동 추가
    _ensure_indicator_schema(conn, ind_table)

    logger.info(f"{sector} 섹터 24개 피처 Batch Processing 시작 (FinRL 최적화)")
    start_time = time.time()

    # 1. 해당 섹터의 모든 원본 데이터를 한 번에 메모리로 로드 (I/O 병목 제거)
    logger.info("1. 전체 주가 데이터 DB 로드 중...")
    try:
        df_raw = pd.read_sql(f"SELECT * FROM {stock_table} WHERE sector=?", conn, params=[sector])
    except Exception as e:
        logger.error(f"DB 읽기 실패: {e}")
        conn.close()
        return

    if df_raw.empty:
        logger.warning(f"{sector} 섹터의 주가 데이터가 없습니다.")
        conn.close()
        return

    tickers = df_raw['ticker'].unique().tolist()
    logger.info(f"-> 총 {len(tickers)}개 종목, {len(df_raw)}행 로드 완료.")

    # 2. 거시지표 & 뉴스 & 수급 데이터 로드
    logger.info("2. 거시/뉴스/수급 데이터 로드 중...")
    macro_df = load_macro()
    news_df = load_news(sector, market)
    sd_df = load_supply_demand(market)
    if sd_df.empty:
        logger.info("  수급 데이터 없음 - foreign_net_5d/inst_net_5d/short_ratio 는 0 처리")
    else:
        logger.info(f"  수급 데이터 {len(sd_df):,}행 로드")
    credit_df, lending_df = load_kiwoom_extra(market)
    if credit_df.empty and lending_df.empty:
        logger.info("  키움 신용/대차 데이터 없음 - credit_ratio/lending_chg_5d 는 0 처리")
    else:
        logger.info(f"  키움 신용 {len(credit_df):,}행 / 대차 {len(lending_df):,}행 로드")

    # 3. 그룹별 지표 일괄 계산 (Pandas Groupby)
    logger.info("3. 메모리 상에서 24개 피처 일괄 연산 중 (Batch)...")
    processed_dfs = []

    for ticker, group in df_raw.groupby('ticker'):
        try:
            processed_g = add_indicators(group, macro_df, news_df, NEWS_WEIGHT, sd_df,
                                         credit_df=credit_df, lending_df=lending_df)
            processed_dfs.append(processed_g)
        except Exception as e:
            logger.error(f"  {ticker} 지표 계산 실패: {e}")

    if not processed_dfs:
        logger.error("계산된 지표 데이터가 없습니다.")
        conn.close()
        return

    # 빈/all-NA 컬럼만 있는 DataFrame 을 concat 에 섞으면 pandas FutureWarning
    # ("empty or all-NA entries deprecated") 가 발생.
    # FEATURE_COLS 를 명시적으로 float64 캐스팅해 dtype 추론 경고를 원천 차단.
    valid_dfs = []
    for d in processed_dfs:
        if d is None or d.empty:
            continue
        for c in FEATURE_COLS:
            if c in d.columns:
                d[c] = d[c].astype('float64')
        valid_dfs.append(d)
    df_final = pd.concat(valid_dfs, ignore_index=True) if valid_dfs else pd.DataFrame()

    # UNIQUE(ticker, date) 인덱스가 있는 경우를 대비한 방어:
    # 원본 stocks 에 중복이 있거나 ticker 가 여러 sector 에 걸치면 중복 행이 생길 수 있다.
    if not df_final.empty:
        df_final = df_final.drop_duplicates(subset=['ticker', 'date'], keep='last')

    # 4. 일괄 DB 저장 (Bulk Insert)
    logger.info(f"4. 가공 완료된 {len(df_final)}행 DB 일괄 저장 중...")
    try:
        # 기존 섹터 데이터 삭제 (중복 방지)
        placeholders = ','.join(['?'] * len(tickers))
        conn.execute(f"DELETE FROM {ind_table} WHERE ticker IN ({placeholders})", tickers)
    except Exception as e:
        pass # 테이블이 없으면 무시하고 생성

    df_final.to_sql(ind_table, conn, if_exists='append', index=False)
    conn.commit()
    conn.close()

    elapsed = time.time() - start_time
    logger.info(f"🎉 {sector} 섹터 Batch 지표 생성 완료! (소요 시간: {elapsed:.2f}초)")

if __name__ == "__main__":
    import sys
    from src.collector.config import ACTIVE_SECTOR, KOREA_SECTORS, USA_SECTORS
    usa_sectors = list(USA_SECTORS) if isinstance(USA_SECTORS, (list, tuple)) else list(USA_SECTORS.keys())

    def _market_of(sec):
        return 'usa' if sec in usa_sectors else 'korea'

    cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if cmd == 'all':
        logger.info("🚀 전 섹터 일괄 지표 생성")
        for s in list(KOREA_SECTORS) + usa_sectors:
            process_sector(s, _market_of(s))
    elif cmd:
        # 특정 섹터 지정
        process_sector(cmd, _market_of(cmd))
    else:
        # 인자 없으면 현재 활성 섹터만 (기존 동작 유지)
        process_sector(ACTIVE_SECTOR, _market_of(ACTIVE_SECTOR))
