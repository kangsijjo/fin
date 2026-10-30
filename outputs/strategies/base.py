"""
다중 전략 통합 베이스.

- StrategyTrade: 모든 전략이 반환하는 공통 매매 dataclass
- BaseStrategy: 전략 추상. backtest(df, costs) → list[StrategyTrade]
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class StrategyTrade:
    strategy: str           # 전략명
    code: str               # 종목코드
    entry_date: str         # YYYYMMDD
    entry_price: float
    exit_date: str
    exit_price: float
    holding_days: int
    gross_pct: float        # 비용 차감 전 수익률
    cost_pct: float         # 비용 (수수료+세금+슬리피지)
    net_pct: float          # 비용 차감 후
    exit_reason: str = "default"
    score: float = 0.0      # 신호 우선순위 (신호일 거래대금 등) — 슬롯 부족 시 랭킹용


import functools
import threading

# [2026-10-23] 신호 계산 창(lookback) 안에 액면분할·병합·감자가 낀 신호는 제외한다.
#   가격은 수정주가가 아니라 원시 가격이라, 예컨대 1:10 병합 뒤엔 500일 고가·이동평균·RSI 가 모두 왜곡된다
#   (500일 신고가 580건: 평균 +0.27% vs 정상 +1.29%). 모든 전략의 backtest() 를 여기서 한 번 감싸서
#   백테스트·연구 스크립트·강도 학습 어디서 불러도 같은 규칙이 적용되게 한다(실매매는 engine_signals 가 같은 규칙).
#   CA_LOOKBACK_FILTER=False 로 끄면 예전 결과를 재현할 수 있다.
CA_LOOKBACK_FILTER = True
_tls = threading.local()


def _wrap_backtest(fn):
    @functools.wraps(fn)
    def backtest(self, df, costs, *a, **k):
        depth = getattr(_tls, "depth", 0)
        _tls.depth = depth + 1
        try:
            trades = fn(self, df, costs, *a, **k)
        finally:
            _tls.depth = depth
        if depth or not CA_LOOKBACK_FILTER or not trades or getattr(self, "timeframe", "daily") != "daily":
            return trades
        try:
            from ._swing_base import drop_ca_lookback_trades, signal_lookback
            return drop_ca_lookback_trades(trades, df, signal_lookback(self))
        except Exception:
            return trades
    backtest._ca_wrapped = True
    return backtest


class BaseStrategy:
    """모든 전략의 부모. 자식은 backtest(df, costs) 구현."""
    name: str = "base"
    timeframe: str = "daily"   # "daily" | "minute"

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        f = cls.__dict__.get("backtest")
        if f is not None and not getattr(f, "_ca_wrapped", False):
            cls.backtest = _wrap_backtest(f)

    def backtest(self, df, costs) -> List[StrategyTrade]:
        """
        df: 일봉 통합 DataFrame
            컬럼: code, date(YYYYMMDD), open, high, low, close, volume,
                  trading_value(거래대금), change_pct(등락률), market_cap(시가총액),
                  foreign_net, inst_net
        costs: dict with keys ['fee_pct', 'tax_pct', 'slip_pct', 'total_pct']
        """
        raise NotImplementedError(f"{self.name}.backtest 미구현")
