"""
장중 폴링 기반 손절/익절 모니터 (2a 단계)

목적
----
KIS API 자체에는 진정한 OCO 가 없으므로, 보유 종목의 현재가를
일정 간격으로 폴링하여 손절/익절/시간청산 룰을 즉시 트리거한다.

설계
----
- 한국장: 09:00 ~ 15:30
- 미국장(해외 KIS): 23:30 ~ 06:00 (서머타임 ±1h)  → 별도 옵션
- 기본 폴링 간격 60초. KIS rate limit(일반 20 req/s) 대비 충분히 여유.
- 토큰 만료 추정 시 자동 재발급(get_token 재호출)
- 종료: 장 마감 시각 도달 또는 Ctrl+C

한계 (정직하게)
----------------
- 폴링이라 갭 발생 후 최대 interval_sec 만큼은 노출됨.
- 진정한 시장가 즉시 보호가 필요하면 2b(예약/조건부 매도 TR) 로 추가 진행.
"""
import time
import sys
import argparse
from datetime import datetime, time as dtime, timedelta

from src.logger import get_logger
from .kis_api import KISTrader
from .position_manager import check_stop_loss_take_profit, get_open_positions

logger = get_logger('intraday_monitor')

# 시장 운영 시간 (현지/한국 기준 KST)
MARKET_HOURS = {
    'korea': (dtime(9, 0),  dtime(15, 30)),
    # KIS 해외 정규장(미 동부 23:30~06:00 KST 근사). 정확한 처리는 추후 보강.
    'usa':   (dtime(23, 30), dtime(6, 0)),
}

# 토큰 발급 후 재발급 주기 (KIS 토큰 수명 24h, 보수적으로 6h 마다 재발급)
TOKEN_REFRESH_SEC = 6 * 60 * 60


def _within_market_hours(market):
    start, end = MARKET_HOURS[market]
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    # 자정 걸치는 미국장
    return now >= start or now <= end


def run(mock=True, market='korea', interval_sec=60, max_runtime_min=None):
    """
    파라미터
    --------
    mock           : 모의/실전
    market         : 'korea' | 'usa'
    interval_sec   : 폴링 간격(초). 갭 보호 vs API 사용량의 트레이드오프.
    max_runtime_min: 최대 동작 분. None 이면 장 마감까지.
    """
    logger.info(
        f"장중 모니터 시작: market={market} mock={mock} interval={interval_sec}s"
    )

    trader = KISTrader(mock=mock)
    if not trader.get_token():
        logger.error("토큰 발급 실패 - 종료")
        return
    last_token_at = time.time()

    end_at = None
    if max_runtime_min is not None:
        end_at = datetime.now() + timedelta(minutes=max_runtime_min)

    while True:
        try:
            # 종료 조건
            if end_at is not None and datetime.now() >= end_at:
                logger.info("최대 동작 시간 도달 - 종료")
                return
            if not _within_market_hours(market):
                logger.info("장 마감 시각 - 모니터 종료")
                return

            # 토큰 재발급
            if time.time() - last_token_at > TOKEN_REFRESH_SEC:
                logger.info("토큰 주기 재발급")
                if trader.get_token():
                    last_token_at = time.time()

            # 보유 포지션이 없으면 폴링 빈도를 낮춰 자원 절약
            positions = get_open_positions()
            if positions.empty:
                logger.info("보유 포지션 없음 - 5분 대기")
                time.sleep(min(300, interval_sec * 5))
                continue

            # 해당 시장 포지션만 체크 (한국장 모니터가 미국 포지션을
            # 폐장 시세로 시간청산하는 것 방지)
            check_stop_loss_take_profit(trader, market=market)

        except KeyboardInterrupt:
            logger.info("사용자 중단 - 종료")
            return
        except Exception as e:
            # 일시 네트워크 오류는 다음 사이클에서 재시도
            logger.error(f"모니터 사이클 오류: {e}", exc_info=True)

        time.sleep(interval_sec)


def _parse_args():
    p = argparse.ArgumentParser(description="KIS 장중 폴링 모니터")
    p.add_argument('--market',   default='korea', choices=['korea', 'usa'])
    p.add_argument('--mock',     action='store_true', default=True)
    p.add_argument('--real',     dest='mock', action='store_false',
                   help='실전투자 모드 (주의: 실제 주문 발생)')
    p.add_argument('--interval', type=int, default=60, help='폴링 간격(초)')
    p.add_argument('--minutes',  type=int, default=None,
                   help='최대 동작 분. 미지정 시 장 마감까지')
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(mock=args.mock, market=args.market,
        interval_sec=args.interval, max_runtime_min=args.minutes)
