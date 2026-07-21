"""
전략 — 외국인+기관 순매수 상위 지속 × 외국인 지분율 상승세 (2단 필터).  (2026-07-21 신설)

사용자 설계(2026-07-21): "외국인+기관 순매수가 5거래일 연속 상위 top100 에 든 종목에
      대해서만 비율추종 — 외국인 지분율이 2~3주 상승세면 매매".

가설: 순수 지분율 신호는 너무 넓어 엣지가 약했다(백테스트로 확인, 평균 −0.14%~+0.79%).
      → '강한 지속 수급'(외국인+기관 합산 순매수 top100 을 flow_consec일 연속)으로 고확신
      종목만 남긴 뒤, 그 안에서 '지분율 2~3주 상승세'를 방아쇠로 쓰면 플로우의 엣지 +
      비중의 확신이 결합된다. 플로우로 거르고 비중으로 트리거하는 2단 구조.

- 1단(수급 지속 게이트): comb = 외국인순매수 + 기관순매수. 일자별 comb 내림차순 순위가
    top_n 이내인 상태가 flow_consec 거래일 연속. (both_positive=True 면 둘 다 순매수여야)
- 2단(비율추종 트리거): 외국인 지분율이 ratio_up_days(거래일) 전보다 ratio_delta_min(%p)
    이상 상승(= 2~3주 상승세).
- 거래대금 게이트 + (옵션) 시장강세. 진입=다음날 시가 / 청산=holding_days 후 종가.

주의: df 에 foreign_net, inst_net, foreign_holding_ratio 컬럼 필요. 지분율 없으면(백필 전)
      신호 0건 안전폴백.
"""

import pandas as pd
from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops, _add_market_gate

RATIO_COL = "foreign_holding_ratio"


class ForeignInstFlowRatioStrategy(BaseStrategy):
    name = "for_inst_flow_ratio"
    timeframe = "daily"

    def __init__(self, top_n=100, flow_consec=5, ratio_up_days=15,
                 ratio_delta_min=0.0, both_positive=False,
                 holding_days=20, min_trading_value=1_000_000_000,
                 use_market_filter=False, stop_loss_pct=None, name=None):
        self.top_n = top_n                    # 순매수 상위 몇 위까지
        self.flow_consec = flow_consec        # 상위 top_n 연속 며칠
        self.ratio_up_days = ratio_up_days    # 지분율 상승 관찰 창(거래일, 2~3주=10~15)
        self.ratio_delta_min = ratio_delta_min
        self.both_positive = both_positive    # 외국인·기관 둘 다 순매수 요구
        self.holding_days = holding_days
        self.min_tv = min_trading_value
        self.use_mkt = use_market_filter
        self.stop_loss_pct = stop_loss_pct
        if name:
            self.name = name

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        # 지분율 없으면 안전 폴백(백필 전 조용한 크래시 방지)
        if RATIO_COL not in df.columns:
            df["signal"] = False
            return _make_trades_for_signals(
                df, holding_days=self.holding_days, strategy_name=self.name, costs=costs)

        fnet = df["foreign_net"].fillna(0) if "foreign_net" in df.columns else 0.0
        inet = df["inst_net"].fillna(0) if "inst_net" in df.columns else 0.0
        df["_comb"] = fnet + inet

        # ── 1단: 일자별 합산 순매수 순위 → top_n 이내인지 → flow_consec일 연속 ──
        # 순위는 '그 날 전종목 교차단면'에서 매긴다(진입시점에 알 수 있는 정보 — 당일 확정
        # 수급 기준. 라이브는 전일 확정치로 판정하므로 룩어헤드 아님).
        df["_rank"] = df.groupby("date")["_comb"].rank(ascending=False, method="min")
        in_top = df["_rank"] <= self.top_n
        if self.both_positive:
            in_top = in_top & (fnet > 0) & (inet > 0)
        df["_intop"] = in_top.astype(int)
        # 연속 판정: 같은 code 안에서 최근 flow_consec개가 모두 top → 합이 flow_consec
        df["_consec"] = df.groupby("code")["_intop"].transform(
            lambda x: x.rolling(self.flow_consec, min_periods=self.flow_consec).sum()
        )
        flow_gate = df["_consec"] >= self.flow_consec

        # ── 2단: 외국인 지분율 상승세(ratio_up_days 전 대비 상승폭) ──
        df["_ratio_chg"] = df.groupby("code")[RATIO_COL].transform(
            lambda x: x - x.shift(self.ratio_up_days)
        )
        ratio_up = (df["_ratio_chg"] >= self.ratio_delta_min) & df["_ratio_chg"].notna()

        if self.use_mkt:
            df = _add_market_gate(df)

        df["signal"] = (
            flow_gate &
            ratio_up &
            df[RATIO_COL].notna() &
            (df["trading_value"] >= self.min_tv) &
            (df["mkt_strong"] if self.use_mkt else True)
        )

        if self.stop_loss_pct is not None:
            return _make_trades_with_stops(
                df, holding_days=self.holding_days, strategy_name=self.name,
                costs=costs, stop_loss_pct=self.stop_loss_pct)
        return _make_trades_for_signals(
            df, holding_days=self.holding_days, strategy_name=self.name, costs=costs)
