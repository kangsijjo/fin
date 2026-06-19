"""
리스크/자본관리 레이어
- target_volatility 기반 포지션 사이징
- 일일 손실 한도(kill switch)
정책 파라미터는 config.yaml 의 trading 섹션을 우선 사용하고, 없으면 기본값.
"""
from datetime import datetime
import pandas as pd

from src.config_db import get_connection
from src.logger import get_logger

logger = get_logger('risk')

# 기본값 (config.yaml 에서 오버라이드 가능)
DEFAULT_TARGET_VOL   = 0.02   # 일일 목표 변동성 2%
DEFAULT_MAX_WEIGHT   = 0.30   # 종목당 자본 비중 한도 30%
DEFAULT_DAILY_DD_KILL = 0.02  # 당일 누적손실 -2% 도달 시 신규매수 중단
DEFAULT_VOL_WINDOW   = 20     # 변동성 추정 윈도

# 안전한 변동성 하한 (0/극저 변동성에서 무한 레버리지 방지)
MIN_VOL = 0.005   # 0.5%


def _get_trading_cfg():
    try:
        from src.collector.config import TRADING_CONFIG
        return TRADING_CONFIG or {}
    except Exception:
        return {}


def estimate_daily_volatility(ticker, market='korea', window=DEFAULT_VOL_WINDOW):
    """최근 window 영업일 종가 기준 일일 수익률 표준편차."""
    table = 'korea_stocks' if market == 'korea' else 'usa_stocks'
    conn = get_connection()
    try:
        df = pd.read_sql(
            f"SELECT date, Close FROM {table} WHERE ticker=? ORDER BY date DESC LIMIT ?",
            conn, params=[ticker, window + 5]
        )
    finally:
        conn.close()

    if df.empty or len(df) < 5:
        return None
    df = df.sort_values('date')
    rets = df['Close'].pct_change().dropna()
    if rets.empty:
        return None
    vol = float(rets.std())
    return max(vol, MIN_VOL)


def position_size(cash_available, current_price, ticker, market='korea',
                  target_vol=None, max_weight=None):
    """
    변동성 기반 사이징:
        weight = min(target_vol / stock_vol, max_weight)
        quantity = floor(cash_available * weight / price)
    """
    cfg = _get_trading_cfg()
    target_vol = target_vol if target_vol is not None else cfg.get('target_vol', DEFAULT_TARGET_VOL)
    max_weight = max_weight if max_weight is not None else cfg.get('max_weight', DEFAULT_MAX_WEIGHT)

    if cash_available <= 0 or current_price <= 0:
        return 0

    vol = estimate_daily_volatility(ticker, market)
    if vol is None:
        # 변동성 미상 → 보수적으로 max_weight 의 절반
        weight = max_weight / 2
    else:
        weight = min(target_vol / vol, max_weight)

    notional = cash_available * weight
    qty = int(notional // current_price)
    logger.info(
        f"sizing {ticker}: vol={vol if vol else 'NA'} weight={weight:.3f} "
        f"cash={cash_available:,} → qty={qty}"
    )
    return max(qty, 0)


# ───────────────────────── 일일 손실 한도 ─────────────────────────

def _today_realized_pnl_pct():
    """오늘 청산된 포지션의 자본 대비 손익 합 (간단 근사: buy_price 기준)."""
    conn = get_connection()
    try:
        df = pd.read_sql(
            """SELECT buy_price, sell_price, quantity FROM positions
               WHERE status LIKE 'closed%' AND sell_date LIKE ?""",
            conn, params=[datetime.now().strftime('%Y-%m-%d') + '%']
        )
    except Exception:
        return 0.0
    finally:
        conn.close()

    if df is None or df.empty:
        return 0.0
    df = df.dropna(subset=['buy_price', 'sell_price', 'quantity'])
    if df.empty:
        return 0.0
    invested = (df['buy_price'] * df['quantity']).sum()
    pnl      = ((df['sell_price'] - df['buy_price']) * df['quantity']).sum()
    if invested <= 0:
        return 0.0
    return float(pnl / invested)


def daily_loss_limit_hit(threshold=None):
    """오늘 실현손익이 threshold(음수) 이하면 True. 신규 매수 차단용."""
    cfg = _get_trading_cfg()
    threshold = threshold if threshold is not None else cfg.get('daily_loss_limit', DEFAULT_DAILY_DD_KILL)
    pnl = _today_realized_pnl_pct()
    if pnl <= -abs(threshold):
        logger.warning(f"일일 손실 한도 도달: pnl={pnl*100:.2f}% (limit={-abs(threshold)*100:.2f}%)")
        return True
    return False
