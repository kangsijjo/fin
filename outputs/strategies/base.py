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


class BaseStrategy:
    """모든 전략의 부모. 자식은 backtest(df, costs) 구현."""
    name: str = "base"
    timeframe: str = "daily"   # "daily" | "minute"

    def backtest(self, df, costs) -> List[StrategyTrade]:
        """
        df: 일봉 통합 DataFrame
            컬럼: code, date(YYYYMMDD), open, high, low, close, volume,
                  trading_value(거래대금), change_pct(등락률), market_cap(시가총액),
                  foreign_net, inst_net
        costs: dict with keys ['fee_pct', 'tax_pct', 'slip_pct', 'total_pct']
        """
        raise NotImplementedError(f"{self.name}.backtest 미구현")
