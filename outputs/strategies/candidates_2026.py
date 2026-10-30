"""
candidates_2026.py — 2026-10-31 새 매매법 후보 13종 (설명서 §90). 엔진 규칙 그대로: 신호일 종가 판정 → 다음날 시가 진입 →
보유일 만기 종가(비용·상한가 시초가 미체결·분할/정리매매 제외는 공통 처리). 파라미터는 **시험 전에 고정**(결과 보고 바꾸지 않음).

근거(조사)
  A 수급 강도(시가총액 대비) — Kang(2025, arXiv 2512.14134, 한국 2020~2024): 기관 매수 강도를 거래대금이 아니라 **시가총액**으로
    나누면 강도 상위일수록 50일 누적 초과수익이 단조 증가(우리 supply_* 는 거래대금 기준).
  B 추세 속 눌림 반등 — Connors RSI(2)·IBS(실무 규칙). 한국은 1983~2023 전체로 단기 반전이 모멘텀보다 우세(Kang·Ryu·Webb 2025),
    단 비용 차감 뒤 사라진다는 연구도 있어 200일선 위(추세) 종목으로 한정.
  C 추세 확인형 돌파 — 미너비니 추세 템플릿(+상대강도·돌파), NR7(Crabel 변동성 수축), 포켓 피벗(O'Neil/Kacher),
    일목균형표 구름대 돌파, ADX 추세 시작(Wilder). 실무 규칙 — 공개된 독립 검증이 거의 없어 직접 시험.
  D 한국 시장 특이 — 상한가 다음날(가격제한이 가격 발견을 다음날로 미룸·자석 효과), 저변동성(개인의 복권주 선호로
    한국에서 변동성 큰 종목이 덜 오른다는 연구 다수 — 200일선 위 + 20일 변동성 하위 10%, 월초).
제외: 래리 윌리엄스 변동성 돌파 — 장중 돌파가에 사야 해 지금의 09:00 매수·15:21 매도 구조로 못 따라함(사용자 결정).
"""

import numpy as np
import pandas as pd

from .base import BaseStrategy
from ._swing_base import _make_trades_for_signals, _add_market_gate

MIN_TV = 3_000_000_000          # 신호일 거래대금 30억 이상(다른 엔진 전략과 같은 관례)


def _g(df):
    return df.groupby("code", sort=False)


def _roll(df, col, n, fn="mean", shift=0):
    """종목별 이동 창(n) 계산 — 전체 열에 한 번 굴린 뒤 종목 경계를 넘는 창(종목 안 위치 < n−1+shift)을 지운다.
    groupby().rolling() 보다 빠르고 메모리가 작다(전 종목 300만 행에서 일목균형표가 3GB 를 넘던 것)."""
    s = _g(df)[col].shift(shift) if shift else df[col]
    out = getattr(s.rolling(n, min_periods=n), fn)()
    return out.where(df["_pos"] >= n - 1 + shift)


def _ewm(df, s, alpha, min_periods):
    return s.groupby(df["code"], sort=False).transform(
        lambda x: x.ewm(alpha=alpha, adjust=False, min_periods=min_periods).mean())


def _prep(df):
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    df["_pos"] = df.groupby("code", sort=False).cumcount()
    return df


class _Cand(BaseStrategy):
    timeframe = "daily"
    holding_days = 20

    def _finish(self, df, sig):
        if getattr(self, "use_mkt", False):
            df = _add_market_gate(df)
            sig = sig & df["mkt_strong"].fillna(False)
        df["signal"] = sig.fillna(False) & (df["trading_value"] >= self.min_tv)
        return _make_trades_for_signals(df, holding_days=self.holding_days, strategy_name=self.name, costs=self.costs)

    def backtest(self, df, costs):
        self.costs = costs
        return self._run(_prep(df))


# ── A. 수급 강도(시가총액 대비) ───────────────────────────────────────────────

class FlowMcapStrategy(_Cand):
    """window 일 순매수 합 ÷ 시가총액 — 그날 거래대금 30억↑ 종목 중 상위 top_pct% 에 새로 들어온 날(어제는 아니었음)."""
    name = "flow_mcap"

    def __init__(self, who="inst", window=5, top_pct=2.0, holding_days=20, min_tv=MIN_TV, use_market_filter=False, name=None):
        self.who, self.window, self.top_pct = who, window, top_pct
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        cols = ["inst_net"] if self.who == "inst" else ["inst_net", "foreign_net"]
        if any(c not in df.columns for c in cols) or "market_cap" not in df.columns:
            df["signal"] = False
            return _make_trades_for_signals(df, holding_days=self.holding_days, strategy_name=self.name, costs=self.costs)
        flow = sum(df[c].fillna(0.0) for c in cols)
        df["_flow"] = flow
        acc = _roll(df, "_flow", self.window, "sum")
        cap = df["market_cap"].where(df["market_cap"] > 0)
        df["_int"] = acc / cap
        elig = df["trading_value"] >= self.min_tv
        rk = df["_int"].where(elig).groupby(df["date"]).rank(pct=True, ascending=False) * 100
        top = (rk <= self.top_pct) & (df["_int"] > 0)
        prev = top.groupby(df["code"], sort=False).shift(1, fill_value=False).astype(bool)
        return self._finish(df, top & ~prev)


# ── B. 추세 속 눌림 반등 ───────────────────────────────────────────────────────

class Rsi2PullbackStrategy(_Cand):
    """Connors: 종가 > 200일선, RSI(2) < rsi_max → 다음날 시가 매수, holding_days 보유(원래의 '5일선 위 청산' 대신 고정 보유)."""
    name = "rsi2_pb"

    def __init__(self, rsi_period=2, rsi_max=10.0, ma_period=200, holding_days=5, min_tv=MIN_TV, use_market_filter=False, name=None):
        self.rsi_period, self.rsi_max, self.ma_period = rsi_period, rsi_max, ma_period
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        d = _g(df)["close"].diff()
        a = 1.0 / self.rsi_period
        up = _ewm(df, d.clip(lower=0), a, self.rsi_period)
        dn = _ewm(df, (-d).clip(lower=0), a, self.rsi_period)
        rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        rsi = rsi.where(dn > 0, 100.0)
        ma = _roll(df, "close", self.ma_period)
        return self._finish(df, (df["close"] > ma) & (rsi < self.rsi_max))


class IbsPullbackStrategy(_Cand):
    """IBS=(종가−저가)/(고가−저가) < ibs_max (종가가 그날 바닥 근처) + 종가 > 200일선 + 그날 하락 → holding_days 보유."""
    name = "ibs_pb"

    def __init__(self, ibs_max=0.15, ma_period=200, holding_days=3, min_tv=MIN_TV, use_market_filter=False, name=None):
        self.ibs_max, self.ma_period = ibs_max, ma_period
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        rng = (df["high"] - df["low"]).where(df["high"] > df["low"])
        ibs = (df["close"] - df["low"]) / rng
        ma = _roll(df, "close", self.ma_period)
        down = df["close"] < _g(df)["close"].shift(1)
        return self._finish(df, (df["close"] > ma) & (ibs < self.ibs_max) & down)


# ── C. 추세 확인형 돌파 ───────────────────────────────────────────────────────

class TrendTemplateStrategy(_Cand):
    """미너비니 추세 템플릿: 종가>50>150>200일선, 200일선이 20일 전보다 위, 52주 저점의 1.3배↑, 52주 고점의 75%↑,
    상대강도(252일 수익률 그날 순위) 상위 30% — 그리고 오늘 20일 종가 신고가 돌파 + 거래량 50일 평균의 1.5배↑."""
    name = "trend_tmpl"

    def __init__(self, lookback=252, rs_min=70.0, breakout=20, vol_mult=1.5, holding_days=20, min_tv=MIN_TV,
                 use_market_filter=False, name=None):
        self.lookback, self.rs_min, self.breakout, self.vol_mult = lookback, rs_min, breakout, vol_mult
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        c = df["close"]
        m50, m150, m200 = _roll(df, "close", 50), _roll(df, "close", 150), _roll(df, "close", 200)
        m200_prev = m200.groupby(df["code"], sort=False).shift(20)
        lo, hi = _roll(df, "close", self.lookback, "min"), _roll(df, "close", self.lookback, "max")
        ret = c / _g(df)["close"].shift(self.lookback) - 1
        rs = ret.groupby(df["date"]).rank(pct=True) * 100
        prev_hi = _roll(df, "close", self.breakout, "max", shift=1)
        vavg = _roll(df, "volume", 50, "mean", shift=1)
        tmpl = (c > m50) & (m50 > m150) & (m150 > m200) & (m200 > m200_prev) & (c >= lo * 1.3) & (c >= hi * 0.75) & (rs >= self.rs_min)
        trig = (c > prev_hi) & (df["volume"] >= vavg * self.vol_mult)
        return self._finish(df, tmpl & trig)


class Nr7BreakoutStrategy(_Cand):
    """어제가 NR7(최근 7일 중 고저폭이 가장 좁음) → 오늘 종가가 어제 고가 위 + 종가 > 50일선."""
    name = "nr7_bk"

    def __init__(self, nr=7, ma_period=50, holding_days=10, min_tv=MIN_TV, use_market_filter=False, name=None):
        self.nr, self.ma_period = nr, ma_period
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        df["_rng"] = df["high"] - df["low"]
        nr = df["_rng"] <= _roll(df, "_rng", self.nr, "min")
        nr_y = nr.groupby(df["code"], sort=False).shift(1, fill_value=False).astype(bool)
        hi_y = _g(df)["high"].shift(1)
        ma = _roll(df, "close", self.ma_period)
        return self._finish(df, nr_y & (df["close"] > hi_y) & (df["close"] > ma))


class PocketPivotStrategy(_Cand):
    """포켓 피벗: 오늘 상승 + 거래량이 지난 10일 '하락일 거래량' 최대보다 큼 + 종가 > 50일선 + 10일선 위 5% 이내(과열 아님)."""
    name = "pocket_pivot"

    def __init__(self, window=10, ma_period=50, ext_max=5.0, holding_days=10, min_tv=MIN_TV, use_market_filter=False, name=None):
        self.window, self.ma_period, self.ext_max = window, ma_period, ext_max
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        pc = _g(df)["close"].shift(1)
        up = df["close"] > pc
        df["_dv"] = df["volume"].where(df["close"] < pc, 0.0)
        maxdv = _roll(df, "_dv", self.window, "max", shift=1)
        m10, m50 = _roll(df, "close", 10), _roll(df, "close", self.ma_period)
        near = (df["close"] >= m10) & (df["close"] <= m10 * (1 + self.ext_max / 100))
        return self._finish(df, up & (df["volume"] > maxdv) & (maxdv > 0) & (df["close"] > m50) & near)


class IchimokuBreakoutStrategy(_Cand):
    """일목균형표: 오늘 종가가 구름대 위로 올라섬(어제는 아래/안) + 전환선 > 기준선 + 후행스팬(종가 > 26일 전 종가)."""
    name = "ichimoku_bk"

    def __init__(self, tenkan=9, kijun=26, senkou=52, holding_days=20, min_tv=MIN_TV, use_market_filter=False, name=None):
        self.tenkan, self.kijun, self.senkou = tenkan, kijun, senkou
        self.lookback = senkou + kijun                      # 신호 창(분할·병합 필터·워밍업)
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        def mid(n):
            return (_roll(df, "high", n, "max") + _roll(df, "low", n, "min")) / 2
        tk, kj = mid(self.tenkan), mid(self.kijun)
        df["_sa"] = (tk + kj) / 2
        df["_sb"] = mid(self.senkou)
        sa = _g(df)["_sa"].shift(self.kijun)
        sb = _g(df)["_sb"].shift(self.kijun)
        top = np.maximum(sa, sb)
        df["_top"] = top
        top_y = _g(df)["_top"].shift(1)
        pc = _g(df)["close"].shift(1)
        lag = _g(df)["close"].shift(self.kijun)
        return self._finish(df, (df["close"] > top) & (pc <= top_y) & (tk > kj) & (df["close"] > lag))


class AdxTrendStrategy(_Cand):
    """ADX(14) 가 오늘 level(25) 위로 올라섬 + +DI > −DI + 종가 > 50일선 — 추세가 막 시작될 때."""
    name = "adx_start"

    def __init__(self, period=14, level=25.0, ma_period=50, holding_days=20, min_tv=MIN_TV, use_market_filter=False, name=None):
        self.period, self.level, self.ma_period = period, level, ma_period
        self.lookback = max(ma_period, 4 * period)
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        g = _g(df)
        ph, pl, pc = g["high"].shift(1), g["low"].shift(1), g["close"].shift(1)
        upm, dnm = df["high"] - ph, pl - df["low"]
        pdm = upm.where((upm > dnm) & (upm > 0), 0.0)
        ndm = dnm.where((dnm > upm) & (dnm > 0), 0.0)
        tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
        a, n = 1.0 / self.period, self.period
        atr = _ewm(df, tr, a, n)
        pdi = 100 * _ewm(df, pdm, a, n) / atr.replace(0, np.nan)
        ndi = 100 * _ewm(df, ndm, a, n) / atr.replace(0, np.nan)
        dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
        adx = _ewm(df, dx, a, n)
        df["_adx"] = adx
        adx_y = _g(df)["_adx"].shift(1)
        ma = _roll(df, "close", self.ma_period)
        return self._finish(df, (adx >= self.level) & (adx_y < self.level) & (pdi > ndi) & (df["close"] > ma))


# ── D. 한국 시장 특이 ──────────────────────────────────────────────────────────

class UpperLimitNextStrategy(_Cand):
    """오늘 상한가 마감(등락률 ≥ limit_pct) → 다음날 시가 매수(시가부터 상한가면 못 산 것으로 봄 — 공통 규칙) → holding_days 보유."""
    name = "uplimit_next"

    def __init__(self, limit_pct=29.5, holding_days=2, min_tv=MIN_TV, use_market_filter=False, name=None):
        self.limit_pct = limit_pct
        self.lookback = 2
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        return self._finish(df, df["change_pct"] >= self.limit_pct)


class LowVolTrendStrategy(_Cand):
    """매달 첫 거래일: 종가 > 200일선 + 20일 평균 거래대금 30억↑ 종목 중 20일 변동성 하위 bottom_pct% → holding_days 보유."""
    name = "lowvol_trend"

    def __init__(self, vol_window=20, ma_period=200, bottom_pct=10.0, holding_days=20, min_tv=MIN_TV,
                 use_market_filter=False, name=None):
        self.vol_window, self.ma_period, self.bottom_pct = vol_window, ma_period, bottom_pct
        self.holding_days, self.min_tv, self.use_mkt = holding_days, min_tv, use_market_filter
        if name:
            self.name = name

    def _run(self, df):
        df["_r"] = _g(df)["close"].pct_change(fill_method=None)
        vol = _roll(df, "_r", self.vol_window, "std")
        tv20 = _roll(df, "trading_value", self.vol_window)
        ma = _roll(df, "close", self.ma_period)
        dates = pd.Series(sorted(df["date"].astype(str).unique()))
        first = set(dates[dates.str[:6] != dates.shift(1).fillna("").str[:6]])
        elig = (df["close"] > ma) & (tv20 >= self.min_tv) & vol.notna() & df["date"].astype(str).isin(first)
        rk = vol.where(elig).groupby(df["date"]).rank(pct=True) * 100
        return self._finish(df, elig & (rk <= self.bottom_pct))


def candidates():
    """시험 대상 13종(파라미터 고정 — 2026-10-31)."""
    return [
        FlowMcapStrategy(who="inst", window=5, name="inst_mcap5"),
        FlowMcapStrategy(who="smart", window=5, name="smart_mcap5"),
        FlowMcapStrategy(who="smart", window=5, use_market_filter=True, name="smart_mcap5_mkt"),
        Rsi2PullbackStrategy(name="rsi2_pb"),
        Rsi2PullbackStrategy(use_market_filter=True, name="rsi2_pb_mkt"),
        IbsPullbackStrategy(name="ibs_pb"),
        TrendTemplateStrategy(name="trend_tmpl"),
        Nr7BreakoutStrategy(name="nr7_bk"),
        PocketPivotStrategy(name="pocket_pivot"),
        IchimokuBreakoutStrategy(name="ichimoku_bk"),
        AdxTrendStrategy(name="adx_start"),
        UpperLimitNextStrategy(name="uplimit_next"),
        LowVolTrendStrategy(name="lowvol_trend"),
    ]
