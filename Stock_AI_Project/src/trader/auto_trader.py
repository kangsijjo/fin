import os
import pickle
import pandas as pd
import numpy as np
import sqlite3
import time
from datetime import datetime
from dotenv import load_dotenv
from .kis_api import KISTrader
from .position_manager import (
    init_position_table, add_position, get_open_positions,
    check_stop_loss_take_profit, get_performance,
    liquidate_on_sell_signal
)
from .risk import position_size, daily_loss_limit_hit
from src.config_db import get_db_path
from src.logger import get_logger

load_dotenv()
logger = get_logger('auto_trader')
DB_PATH = get_db_path()

from src.config_db import get_connection

def get_latest_features(ticker, market='korea'):
    """indicators 테이블에서 최신 피처 1행 조회"""
    from src.processor.indicators import FEATURE_COLS
    
    conn = get_connection()
    table = 'korea_indicators' if market == 'korea' else 'usa_indicators'

    try:
        df = pd.read_sql(
            f"""SELECT * FROM {table} 
                WHERE ticker=? 
                ORDER BY date DESC 
                LIMIT 1""",
            conn, params=[ticker]
        )
        conn.close()

        if df.empty:
            return None

        # 모델 피처 정렬. 누락된 컬럼은 0 으로 채워 항상 FEATURE_COLS 순서로 반환.
        # (수급 데이터 백필 중이거나 옛 indicators 테이블 사용 시 호환)
        missing_cols = [c for c in FEATURE_COLS if c not in df.columns]
        if missing_cols:
            logger.debug(f"{ticker} 누락 피처 (0 처리): {missing_cols}")
            for c in missing_cols:
                df[c] = 0.0

        # SQLite 에서 NULL 섞인 컬럼은 object 타입으로 올라옴 → LightGBM 거부.
        # 모든 피처를 numeric 으로 강제 변환, 변환 실패/NaN 은 0 처리.
        feats = df[FEATURE_COLS].apply(pd.to_numeric, errors='coerce').fillna(0.0)
        return feats

    except Exception as e:
        logger.error(f"피처 조회 실패 {ticker}: {e}")
        conn.close()
        return None
    
def load_model(sector):
    """
    모델 로드. 신/구 두 가지 저장 포맷 모두 지원:
      - 신: {'model': lgb, 'features': [...]}
      - 구: lgb 단독 객체
    반환: (model, feature_names or None)
    """
    model_path = f'src/models/saved/{sector}_model.pkl'
    if not os.path.exists(model_path):
        logger.warning(f"모델 없음: {model_path}")
        return None, None
    with open(model_path, 'rb') as f:
        obj = pickle.load(f)
    if isinstance(obj, dict) and 'model' in obj:
        return obj['model'], obj.get('features')
    return obj, None

def scan_all_sectors():
    """전체 섹터 스캔 → 매수 신호 종목 반환.
    매수/매도 임계값은 config.yaml 의 model.prob_threshold 를 우선 사용한다.
    """
    try:
        from src.collector.config import MODEL_CONFIG
        prob_threshold = float(MODEL_CONFIG.get('prob_threshold', 0.6))
    except Exception:
        prob_threshold = 0.6

    usa_sectors = ['Industrials', 'Financials', 'Information Technology',
                   'Health Care', 'Consumer Discretionary', 'Consumer Staples',
                   'Utilities', 'Real Estate', 'Materials',
                   'Communication Services', 'Energy']

    model_dir = 'src/models/saved'
    all_signals = []

    model_files = [f for f in os.listdir(model_dir) if f.endswith('_model.pkl')]

    for model_file in model_files:
        sector = model_file.replace('_model.pkl', '')
        market = 'usa' if sector in usa_sectors else 'korea'

        try:
            with open(f'{model_dir}/{model_file}', 'rb') as f:
                obj = pickle.load(f)
            if isinstance(obj, dict) and 'model' in obj:
                model = obj['model']
                expected_features = obj.get('features')
            else:
                model = obj
                expected_features = None
        except Exception as e:
            logger.error(f"모델 로드 실패 {sector}: {e}")
            continue

        conn = get_connection()
        table = 'korea_stocks' if market == 'korea' else 'usa_stocks'
        try:
            tickers = pd.read_sql(
                f"SELECT DISTINCT ticker, name FROM {table} WHERE sector=?",
                conn, params=[sector]
            ).values.tolist()
        except Exception as e:
            logger.error(f"종목 조회 실패 {sector}: {e}")
            conn.close()
            continue
        conn.close()

        for ticker, name in tickers:
            features = get_latest_features(ticker, market)
            if features is None:
                continue
            try:
                # 모델이 학습 때 본 피처 이름/순서를 그대로 강제.
                # LightGBM은 컬럼 순서로 매칭하므로 명시적 reindex 가 안전.
                expected = (
                    expected_features
                    or getattr(model, 'feature_name_', None)
                    or getattr(model, 'feature_names_in_', None)
                )
                if expected is not None:
                    missing = [c for c in expected if c not in features.columns]
                    for c in missing:
                        features[c] = 0.0          # 새 피처는 0 으로 안전 대체
                    features = features.reindex(columns=list(expected))
                elif model.n_features_in_ != features.shape[1]:
                    logger.warning(
                        f"{ticker} 피처 개수 미스매치(model={model.n_features_in_}, "
                        f"input={features.shape[1]}). 스킵.")
                    continue
                probs = model.predict_proba(features)[0]
                # predict_proba 의 열 순서는 model.classes_ 를 따른다.
                # 학습 데이터에 특정 클래스가 없으면 [0,1,2] 가정이 깨져
                # 매도확률을 매수확률로 잘못 읽을 수 있음 (backtest.py 와 동일 방식).
                classes = list(getattr(model, 'classes_', [0, 1, 2]))
                buy_prob  = probs[classes.index(1)] if 1 in classes else 0.0
                sell_prob = probs[classes.index(2)] if 2 in classes else 0.0

                if buy_prob >= prob_threshold:
                    all_signals.append({
                        'ticker': ticker,
                        'name': name,
                        'sector': sector,
                        'market': market,
                        'prob': buy_prob,
                        'signal': 'buy'
                    })
                elif sell_prob >= prob_threshold:
                    all_signals.append({
                        'ticker': ticker,
                        'name': name,
                        'sector': sector,
                        'market': market,
                        'prob': sell_prob,
                        'signal': 'sell'
                    })
            except Exception as e:
                logger.error(f"예측 실패 {ticker}: {e}")
                continue

    all_signals.sort(key=lambda x: x['prob'], reverse=True)
    return all_signals

def run_auto_trader(mock=True, market_filter=None):
    """자동매매 실행"""
    init_position_table()

    logger.info(f"자동매매 시작 - 모드: {'모의투자' if mock else '실전투자'}")

    trader = KISTrader(mock=mock)
    if not trader.get_token():
        logger.error("토큰 발급 실패")
        return

    # 1. 손절/익절 체크 먼저 (해당 시장 포지션만)
    logger.info("손절/익절 체크 중...")
    check_stop_loss_take_profit(trader, market=market_filter)

    # 2. 전체 섹터 스캔
    logger.info("전체 섹터 스캔 중...")
    signals = scan_all_sectors()

    # 시장 필터 적용
    if market_filter:
        signals = [s for s in signals if s['market'] == market_filter]

    # 매수/매도 신호 분리 (로그 전에 먼저 분리해야 정확한 카운트 표시 가능)
    buy_signals  = [s for s in signals if s['signal'] == 'buy']
    sell_signals = [s for s in signals if s['signal'] == 'sell']

    logger.info(f"스캔 결과 - 매수 신호: {len(buy_signals)}개 / 매도 신호: {len(sell_signals)}개")
    for i, s in enumerate(buy_signals[:10]):
        logger.info(f"  매수 {i+1}. [{s['sector']}] {s['name']} ({s['ticker']}) - {s['prob']:.2%}")

    if not signals:
        logger.info("유효 신호 없음")
        return

    logger.info(f"매도 신호: {len(sell_signals)}개")

    # 모델 sell 신호가 뜬 보유종목 조기청산 (5일 룰보다 우선).
    # 2026-06-11 적용 — compare_rules.py 백테스트로 수익 기여 사후 검증 필요.
    sell_probs = {s['ticker']: s['prob'] for s in sell_signals}
    liquidate_on_sell_signal(trader, sell_probs, market=market_filter)

    # 일일 손실 한도 도달 시 신규 매수 차단 (kill switch)
    if daily_loss_limit_hit():
        logger.warning("일일 손실 한도 도달 - 신규 매수 중단")
        get_performance()
        return

    # 중복 보유 가드: 이미 보유 중인 종목은 매수 후보에서 제외.
    # 같은 종목을 매일 추가 매수하면 자본관리 비중이 무너지고
    # 보유기간(5영업일) 룰과 충돌한다.
    open_pos = get_open_positions()
    held_tickers = set(open_pos['ticker'].astype(str).tolist()) if not open_pos.empty else set()
    if held_tickers:
        before = len(buy_signals)
        buy_signals = [s for s in buy_signals if s['ticker'] not in held_tickers]
        filtered = before - len(buy_signals)
        if filtered:
            logger.info(f"중복 보유 제외: {filtered}개 (보유 중 {len(held_tickers)}개)")

    # Top-N 매수 — config.yaml 의 trading.top_n 사용 (기본 1)
    # 2026-06-08 백테스트 결과: Top-1 이 Top-3 보다 누적수익 우수 (compare_rules.py).
    try:
        from src.collector.config import TRADING_CONFIG
        top_n = int(TRADING_CONFIG.get('top_n', 1))
    except Exception:
        top_n = 1
    top_signals = buy_signals[:top_n]

    for s in top_signals:
        try:
            current_price = trader.get_current_price(s['ticker'], market=s['market'])
            if current_price is None or current_price <= 0:
                continue

            # 주문가능현금 조회 (예수금 dnca_tot_amt 는 D+2 미정산 포함이라
            # 연속 매수 시 같은 현금을 중복으로 잡음 → 종목/가격 기준 TR 사용).
            # 매수마다 재조회하므로 직전 체결분이 자동 반영된다.
            cash_available = trader.get_orderable_cash(
                s['ticker'], current_price, market=s['market'])
            if cash_available is None or cash_available <= 0:
                logger.info(f"주문가능현금 없음/조회실패 - 스킵: {s['ticker']}")
                continue
            per_slot_cash = cash_available // max(len(top_signals), 1)

            quantity = position_size(
                cash_available=per_slot_cash,
                current_price=current_price,
                ticker=s['ticker'],
                market=s['market'],
            )
            if quantity <= 0:
                logger.info(f"사이징 결과 0주 - 스킵: {s['ticker']}")
                continue

            # 사이징에 쓴 현재가를 주문가로 그대로 전달.
            # (안 주면 buy() 가 현재가를 재조회 → DB buy_price 와 주문가 불일치 가능)
            if trader.buy(s['ticker'], quantity, price=current_price,
                          market=s['market']):
                # KIS 모의는 보통 즉시체결이지만 비동기 반영 여지가 있어 1초 대기.
                # 이후 실제 잔고를 조회해 체결이 반영된 경우에만 add_position.
                # 이 sync 가 없으면, 주문은 갔는데 체결이 안 된 경우 DB 에만
                # 'open' 포지션이 남아 손절 폴링 시 매도 실패 무한반복(orphan) 이 발생한다.
                time.sleep(1.0)
                holdings = trader.get_holdings(market=s['market'])
                if holdings is None:
                    # 잔고 조회 자체가 실패 - 일시적 오류로 보고 보수적으로 등록.
                    # (안 등록하면 진짜 보유 중인데 손절 모니터가 못 잡는 위험)
                    logger.warning(
                        f"매수 후 잔고 조회 실패 - 일단 포지션 등록 진행: {s['ticker']}"
                    )
                elif s['ticker'] not in holdings:
                    logger.warning(
                        f"매수 주문 OK 인데 잔고 미반영 → 포지션 미등록: "
                        f"{s['name']} ({s['ticker']}) "
                        f"(체결 실패 또는 지연 가능성)"
                    )
                    time.sleep(0.5)
                    continue

                add_position(
                    ticker=s['ticker'],
                    name=s['name'],
                    sector=s['sector'],
                    market=s['market'],
                    quantity=quantity,
                    buy_price=current_price,
                    target_profit=0.10,
                    stop_loss=0.0,    # SL OFF (2026-06-08 백테스트 결정. compare_rules.py 참조)
                    holding_days=5,
                    lgbm_prob=float(s.get('prob', 0.0)),
                )
                logger.info(f"매수 완료: {s['name']} ({s['ticker']}) "
                           f"{quantity}주 @ {current_price:,} - {s['prob']:.2%}")
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"매수 실패 {s['ticker']}: {e}")
            continue

    # 4. 성과 출력
    get_performance()

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="KIS 자동매매")
    p.add_argument('--market', default='korea', choices=['korea', 'usa'],
                   help="매매 대상 시장 (스케줄러가 명시적으로 전달)")
    args = p.parse_args()
    run_auto_trader(mock=True, market_filter=args.market)
