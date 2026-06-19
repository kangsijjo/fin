"""
PortfolioStrategy — 여러 BaseStrategy 인스턴스를 결합.

설계:
- 각 sub-strategy 의 매매를 단순 합집합으로 결합 (중복 진입 허용)
- 라벨은 포트폴리오명으로 통일
- evaluate() 시 매매 빈도 큰 sub-strategy 자연 가중치
- 동시보유/자본분배 모델링은 별도 (다음 단계 = "자본 사이즈 모델링")

실전 의도:
  - "3개 전략을 각자 자본 1/3 로 동시 운용"
  - 같은 종목 같은 날 중복 신호 시 그만큼 매수 (자본 X3)
"""

from typing import List
from .base import BaseStrategy, StrategyTrade


class PortfolioStrategy(BaseStrategy):
    name = "portfolio"
    timeframe = "daily"

    def __init__(self, strategies: List[BaseStrategy], name=None):
        self.strategies = strategies
        if name:
            self.name = name

    def backtest(self, df, costs) -> List[StrategyTrade]:
        all_trades = []
        for s in self.strategies:
            try:
                trades = s.backtest(df, costs)
            except Exception as e:
                print(f"  [portfolio sub-fail] {s.name}: {e}")
                continue
            for t in trades:
                # 원 strategy 라벨 보존하되 portfolio 명으로 덮어씌움 (평가 통일)
                t.strategy = self.name
                all_trades.append(t)
        return all_trades
