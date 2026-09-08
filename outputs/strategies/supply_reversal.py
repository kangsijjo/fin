# -*- coding: utf-8 -*-
"""
supply_reversal — 수급 역전(개인 이탈 + 외국인/기관 유입) 전략. (2026-09-08 사용자 아이디어)

사용자 설계(2026-09-08): "최근 20영업일 개인/기관/외국인 수급동향을 봐서, 개인이 0 밑으로
가는 시점(양→음 전환)과 외국인 또는 기관이 0 이상으로 올라가는 시점(음→양 전환)에 매수,
20영업일째 매도."

논리: 스마트머니(외국인/기관)가 개인 물량을 받기 시작하는 '주체 교체 초입'을 잡는다.
  - 개인 20일 누적 순매수가 **양수→음수로 교차**(개인 이탈 시작)
  - 외국인 또는 기관 20일 누적이 **음수→양수로 교차**(유입 시작)
  - 두 교차가 cross_window 거래일 이내에 함께 → 매수 신호
  - 20영업일 보유 후 종가 청산

룩어헤드 방지: 누적/교차는 당일 확정 수급으로 판정, 진입은 다음날 시가
  (_make_trades_for_signals entry_lag=1). 라이브도 전일 확정치로 판정 → 동일 제약.

데이터: df 에 individual_net / foreign_net / inst_net 필요(daily_loader 가 supply_demand
  에서 individual_net 을 조인). 개인 컬럼 없으면 안전 폴백(신호 0).

주의(2026-09-08): 손절은 데이터로 기각됐으므로 넣지 않는다(_make_trades_for_signals 사용).
  가격/추세 필터는 기본 미적용 — use_price_gate 로 옵션 비교.
"""
import pandas as pd

from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _make_trades_with_stops, _add_market_gate


class SupplyReversalStrategy(BaseStrategy):
    name = "supply_reversal"

    def __init__(self, accum_days=20, holding_days=20, cross_window=3,
                 smart_mode="or", min_tv=1_000_000_000, use_mkt=False,
                 use_price_gate=False, name=None):
        """
        accum_days   : 수급 누적 기간(영업일). 사용자 사양 20.
        holding_days : 진입 후 보유 영업일. 사용자 사양 20.
        cross_window : 개인 하향교차와 스마트 상향교차가 이 거래일 이내면 '동시'로 간주.
                       1 = 같은 날 둘 다 교차(엄격, 신호 적음). 3 = 3일 내(완화).
        smart_mode   : "or" = 외국인 또는 기관 유입 / "and" = 둘 다 유입(강한 신호, 적음).
        min_tv       : 최소 거래대금(유동성 필터).
        use_mkt      : 시장 강세 게이트(_add_market_gate).
        use_price_gate: True 면 진입일 종가가 20일 이동평균 위일 때만(추세 확인).
        """
        self.accum_days = accum_days
        self.holding_days = holding_days
        self.cross_window = cross_window
        self.smart_mode = smart_mode
        self.min_tv = min_tv
        self.use_mkt = use_mkt
        self.use_price_gate = use_price_gate
        if name:
            self.name = name

    def _fallback(self, df, costs):
        df = df.copy()
        df["signal"] = False
        return _make_trades_for_signals(
            df, holding_days=self.holding_days, strategy_name=self.name, costs=costs)

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()

        # 개인 데이터 없으면(백필 전) 조용한 크래시 대신 신호 0 폴백
        if "individual_net" not in df.columns or df["individual_net"].notna().sum() == 0:
            return self._fallback(df, costs)

        gb = df.groupby("code")
        N = self.accum_days

        # ── 20일 누적 수급(종목별) ──
        def _acc(col):
            return gb[col].transform(
                lambda s: s.fillna(0.0).rolling(N, min_periods=N).sum())
        df["_ind_acc"] = _acc("individual_net")
        df["_for_acc"] = _acc("foreign_net")
        df["_ins_acc"] = _acc("inst_net")

        # ── 부호 전환(교차) 감지: 전일 누적 부호 vs 당일 ──
        ind_prev = df.groupby("code")["_ind_acc"].shift(1)
        for_prev = df.groupby("code")["_for_acc"].shift(1)
        ins_prev = df.groupby("code")["_ins_acc"].shift(1)

        # 개인: 양(>0) → 음(<=0)  = 이탈 시작
        ind_cross_down = (ind_prev > 0) & (df["_ind_acc"] <= 0)
        # 외국인/기관: 음(<0) → 양(>=0)  = 유입 시작
        for_cross_up = (for_prev < 0) & (df["_for_acc"] >= 0)
        ins_cross_up = (ins_prev < 0) & (df["_ins_acc"] >= 0)

        if self.smart_mode == "and":
            smart_cross = for_cross_up & ins_cross_up
        else:
            smart_cross = for_cross_up | ins_cross_up

        df["_ind_cross"] = ind_cross_down.fillna(False).astype(int)
        df["_smart_cross"] = smart_cross.fillna(False).astype(int)

        # ── cross_window 내 동시성: 최근 window일에 각 교차가 있었나 ──
        w = max(1, self.cross_window)
        df["_ind_recent"] = df.groupby("code")["_ind_cross"].transform(
            lambda s: s.rolling(w, min_periods=1).max())
        df["_smart_recent"] = df.groupby("code")["_smart_cross"].transform(
            lambda s: s.rolling(w, min_periods=1).max())

        cond = (df["_ind_recent"] > 0) & (df["_smart_recent"] > 0)

        # 누적이 아직 min_periods 미만이면(초기 20일) 신호 금지
        cond = cond & df["_ind_acc"].notna() & df["_for_acc"].notna()

        # 유동성 필터
        if "trading_value" in df.columns:
            cond = cond & (df["trading_value"] >= self.min_tv)

        # 선택: 추세 확인(진입일 종가 >= 20일 이동평균)
        if self.use_price_gate:
            ma = df.groupby("code")["close"].transform(
                lambda s: s.rolling(20, min_periods=20).mean())
            cond = cond & (df["close"] >= ma)

        # 선택: 시장 강세 게이트
        if self.use_mkt:
            df = _add_market_gate(df)
            cond = cond & df["mkt_strong"]

        df["signal"] = cond.fillna(False)

        return _make_trades_for_signals(
            df, holding_days=self.holding_days, strategy_name=self.name, costs=costs)


class SupplyRatioStrategy(BaseStrategy):
    """수급 '비율 + 연속' 전략 — 원안(0 교차)의 대안 A/B/C 를 분리·결합 검증. (2026-09-08)

    원안의 약점 세 가지를 각각 고친다:
      A. 창 5일(20일 대신)   — 반응성. 이 시스템 IC 에서 5일 수급 비율이 이미 상위 피처.
      B. 비율 + 연속(0 교차 대신) — (외인+기관 W일 순매수 ÷ W일 거래대금)이 그날 교차단면
         상위 smart_top_pct% 인 상태가 consec일 연속. 0 교차의 노이즈·종목 규모 무시를 해결.
      C. 개인을 '강도'로(교차 대신) — 개인 W일 순매도 비율이 상위 ind_top_pct%.
         제로섬상 개인 조건은 외인+기관과 상관이 높아 독립 기여가 작을 것 — 그걸 데이터로 본다.

    mode: "smart"=B만 / "ind"=C만 / "both"=A+B+C 결합.
    같은 표에 나란히 놓아(ablation) '무엇이 기여했나'를 가른다. 결합만 보면 이유를 모른다.

    과적합 경계: 조건이 많고 신호가 적을수록 과거 우연에 맞춘다. consec/top_pct 는
    느슨하게(2일, 20%) 시작해 신호 수를 보며 좁힌다. 판정은 --verify 의 OOS 로.
    """
    name = "supply_ratio"

    def __init__(self, window=5, consec=2, smart_top_pct=20.0, ind_top_pct=20.0,
                 mode="both", holding_days=20, min_tv=1_000_000_000, name=None):
        self.window = window
        self.consec = consec
        self.smart_top_pct = smart_top_pct
        self.ind_top_pct = ind_top_pct
        self.mode = mode
        self.holding_days = holding_days
        self.min_tv = min_tv
        if name:
            self.name = name

    def _fallback(self, df, costs):
        df = df.copy()
        df["signal"] = False
        return _make_trades_for_signals(
            df, holding_days=self.holding_days, strategy_name=self.name, costs=costs)

    def backtest(self, df, costs):
        df = df.sort_values(["code", "date"]).copy()
        need_ind = self.mode in ("ind", "both")
        if need_ind and ("individual_net" not in df.columns
                         or df["individual_net"].notna().sum() == 0):
            return self._fallback(df, costs)
        if "trading_value" not in df.columns:
            return self._fallback(df, costs)

        gb = df.groupby("code")
        W = self.window

        def _rs(col):
            return gb[col].transform(
                lambda s: s.fillna(0.0).rolling(W, min_periods=W).sum())

        df["_for_w"] = _rs("foreign_net")
        df["_ins_w"] = _rs("inst_net")
        df["_tv_w"] = _rs("trading_value")
        tv = df["_tv_w"].where(df["_tv_w"] > 0)             # 0 나눗셈 방어
        df["_smart_ratio"] = (df["_for_w"] + df["_ins_w"]) / tv
        if need_ind:
            df["_ind_w"] = _rs("individual_net")
            df["_ind_ratio"] = (-df["_ind_w"]) / tv           # 순매도를 양수로: 클수록 강한 이탈

        # 그날 교차단면 상위 % (pct rank, 최대값이 가장 작은 pct)
        df["_smart_pct"] = df.groupby("date")["_smart_ratio"].rank(
            pct=True, ascending=False) * 100.0
        smart_ok = (df["_smart_pct"] <= self.smart_top_pct) & (df["_smart_ratio"] > 0)
        if need_ind:
            df["_ind_pct"] = df.groupby("date")["_ind_ratio"].rank(
                pct=True, ascending=False) * 100.0
            ind_ok = (df["_ind_pct"] <= self.ind_top_pct) & (df["_ind_ratio"] > 0)

        if self.mode == "smart":
            cond = smart_ok
        elif self.mode == "ind":
            cond = ind_ok
        else:
            cond = smart_ok & ind_ok

        # consec일 연속 충족
        df["_ok"] = cond.fillna(False).astype(int)
        k = max(1, self.consec)
        df["_run"] = df.groupby("code")["_ok"].transform(
            lambda s: s.rolling(k, min_periods=k).sum())
        sig = (df["_run"] >= k) & df["_smart_ratio"].notna()
        sig = sig & (df["trading_value"] >= self.min_tv)
        df["signal"] = sig.fillna(False)

        return _make_trades_for_signals(
            df, holding_days=self.holding_days, strategy_name=self.name, costs=costs)


class SupplyCrossoverStrategy(BaseStrategy):
    """수급선 '역전(상대 교차)' + 익절 — 사용자 원안의 **정정된** 해석. (2026-09-08)

    앞의 SupplyReversalStrategy 는 '개인이 0 밑 / 기관·외인이 0 위'(절대 0 교차)로 구현했는데,
    사용자가 스크린샷(두산에너빌리티 2026-08-26)으로 바로잡았다: 그날 기관 20일누적선
    (1,112,992)이 개인 20일누적선(317,193)을 **아래에서 위로 역전**했고 둘 다 0 위였다.
    즉 절대 0 이 아니라 **선끼리의 역전**이 신호다. 청산도 고정 20일이 아니라
    **20영업일 내 +2% 도달 시 익절, 미도달이면 20일째 종가**.

    신호: 기관 또는 외국인의 accum_days 누적 순매수가 개인 누적 순매수를 전일 이하 → 당일 초과.
    진입: 다음날 시가(entry_lag=1). 청산: take_profit_pct 지정가(일중 고가 기준) / 만기.
    take_profit_pct=None 이면 고정 보유 대조군(같은 신호로 TP 효과만 분리).

    주의: +2% 익절·무손절은 '작은 이익 다수 + 큰 손실 소수'의 음의 왜도 구조다.
    승률이 높게 나와도 평균수익·진짜 MDD 로 판정해야 한다. tp_fill="high" 는 일중 고가
    체결 가정이라 약간 낙관적 — 결과가 양수면 "close" 로 감도 재확인.
    """
    name = "supply_xover"

    def __init__(self, accum_days=20, holding_days=20, take_profit_pct=2.0, tp_fill="high",
                 smart_mode="or", min_tv=1_000_000_000, stop_loss_pct=None, name=None):
        self.accum_days = accum_days
        self.holding_days = holding_days
        self.take_profit_pct = take_profit_pct
        self.tp_fill = tp_fill
        self.stop_loss_pct = stop_loss_pct      # (2026-09-08) 익절+손절 스윕용. None=손절 없음
        self.smart_mode = smart_mode
        self.min_tv = min_tv
        if name:
            self.name = name

    def signal_df(self, df):
        """신호 계산만 — 청산 규칙(익절/손절) 스윕에서 재사용하도록 분리. (2026-09-08)
        반환 df 는 code/date 정렬 + 'signal' 컬럼. 개인 데이터 없으면 signal 전부 False."""
        df = df.sort_values(["code", "date"]).copy()
        if "individual_net" not in df.columns or df["individual_net"].notna().sum() == 0:
            df["signal"] = False
            return df

        gb = df.groupby("code")
        N = self.accum_days

        def _acc(col):
            return gb[col].transform(
                lambda s: s.fillna(0.0).rolling(N, min_periods=N).sum())
        df["_ind"] = _acc("individual_net")
        df["_for"] = _acc("foreign_net")
        df["_ins"] = _acc("inst_net")

        # 선 역전: 전일 (스마트 <= 개인) → 당일 (스마트 > 개인)
        def _cross_up(col):
            cur = df[col] > df["_ind"]
            prev = df.groupby("code")[col].shift(1) <= df.groupby("code")["_ind"].shift(1)
            return cur & prev
        ins_x = _cross_up("_ins")
        for_x = _cross_up("_for")
        cross = (ins_x & for_x) if self.smart_mode == "and" else (ins_x | for_x)

        cond = cross & df["_ind"].notna() & df["_for"].notna() & df["_ins"].notna()
        if "trading_value" in df.columns:
            cond = cond & (df["trading_value"] >= self.min_tv)
        df["signal"] = cond.fillna(False)
        return df

    def backtest(self, df, costs):
        df = self.signal_df(df)
        if self.take_profit_pct is None and self.stop_loss_pct is None:
            return _make_trades_for_signals(
                df, holding_days=self.holding_days, strategy_name=self.name, costs=costs)
        return _make_trades_with_stops(
            df, holding_days=self.holding_days, strategy_name=self.name, costs=costs,
            take_profit_pct=self.take_profit_pct, tp_fill=self.tp_fill,
            stop_loss_pct=self.stop_loss_pct)
