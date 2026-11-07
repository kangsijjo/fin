# -*- coding: utf-8 -*-
"""
ai_features.py — [§97] 개선 AI 의 가격·거래 피처 20개. 학습(ai_model.load_tables)·라이브(strength_logger)·
기간 백테스트(period_backtest)·설정 탐색(setting_sweep)이 모두 이 함수로 계산한다(학습·라이브 잣대 일치).

빌려 온 근거(검증된 것만)
  · Gu·Kelly·Xiu(2020, Review of Financial Studies) — 나무·신경망 모형에서 가장 강한 예측 변수 묶음이
    모멘텀(최근 수익률)·유동성(거래대금·Amihud)·변동성·규모(시가총액).
  · Qlib Alpha158 — 봉 모양(KMID·KUP·KLOW)·이동평균 거리·RSV.
  · §97 샌드박스 워크포워드(2021~2026, 59전략): 이 피처 + 이익확률 분류 앙상블로 같은 날 상위 2개 효과가
    전 기간·최근 3년 모두 현행보다 높았다(설명서 §97).

규칙
  · 모든 값은 그날 종가까지 알 수 있는 것만 쓴다(신호 = 그날 종가, 진입 = 다음 날 시가).
  · 거래가 없던 날(시가·고가·저가 0 — 거래정지·무거래, 종가만 이월된 행)은 빼고 계산한다 → 창은 '거래된 날' 기준,
    재개일 수익률 = 마지막 거래일 종가 대비. (0 을 그대로 두면 250일 저가가 0 이 돼 값이 깨진다.)
  · 가장 긴 창 = 250거래일 → 종목마다 마지막 HISTORY 행만 있어도 마지막 날 값은 전체로 잰 값과 같다(테스트로 고정).
"""
import numpy as np
import pandas as pd

VERSION = 1             # 정의가 바뀌면 올린다(ai_model 워크포워드 캐시 서명에 들어감)
PRICE_FEATS = ("ret_5", "ret_20", "ret_60", "ret_120", "vol_20", "vol_60", "log_tv20", "log_mcap", "amihud_20",
               "dist_high250", "dist_low250", "ma20_gap", "ma60_gap", "rsv_20", "gap_1d", "kmid", "kup", "klow",
               "skew_20", "turn_ratio_5_60")
LABELS = {
    "ret_5": "5일 수익률", "ret_20": "20일 수익률", "ret_60": "60일 수익률", "ret_120": "120일 수익률",
    "vol_20": "20일 변동성", "vol_60": "60일 변동성", "log_tv20": "20일 평균 거래대금(로그)",
    "log_mcap": "시가총액(로그)", "amihud_20": "비유동성(Amihud 20일)", "dist_high250": "250일 고가 대비",
    "dist_low250": "250일 저가 대비", "ma20_gap": "20일 이동평균 대비", "ma60_gap": "60일 이동평균 대비",
    "rsv_20": "20일 고저 범위 안 위치", "gap_1d": "당일 시가 갭", "kmid": "당일 몸통(종가-시가)",
    "kup": "윗꼬리 비율", "klow": "아랫꼬리 비율", "skew_20": "20일 수익률 왜도", "turn_ratio_5_60": "거래대금 5일/60일",
}
HISTORY = 300           # 라이브: 종목마다 거래된 날 마지막 300행(가장 긴 창 250 + 여유)
_NUM = ("open", "high", "low", "close", "trading_value", "market_cap")


def _d8(s):
    return s.astype(str).str.replace("-", "", regex=False).str[:8]


def _prep(daily, codes=None):
    """일봉(load_macro_daily 형식: code·date·open·high·low·close·trading_value·market_cap) → 계산용 표.
    codes 가 있으면 그 종목만. 거래 없던 날(시가·고가·저가·종가 중 0 이하) 행 제외, (종목, 날짜) 정렬·중복 제거."""
    cols = ["code", "date"] + [c for c in _NUM if c in daily.columns]
    if codes is not None:
        codes = {str(c).zfill(6) for c in codes}
        daily = daily.loc[daily["code"].astype(str).str.zfill(6).isin(codes), cols]
    P = daily[cols].copy()
    P["code"] = P["code"].astype(str).str.zfill(6)
    P["date"] = _d8(P["date"])
    for c in _NUM:
        P[c] = pd.to_numeric(P[c], errors="coerce").astype("float64") if c in P.columns else np.nan
    P = P[(P["open"] > 0) & (P["high"] > 0) & (P["low"] > 0) & (P["close"] > 0)]
    P = P.sort_values(["code", "date"], kind="mergesort").drop_duplicates(["code", "date"], keep="last")
    return P.reset_index(drop=True)


def _compute(P):
    """_prep 결과 → DataFrame[code, date, PRICE_FEATS…](float32)."""
    out = P[["code", "date"]].copy()
    if P.empty:
        for f in PRICE_FEATS:
            out[f] = pd.Series(dtype="float32")
        return out
    key = P["code"]
    c, o, h, lo = P["close"], P["open"], P["high"], P["low"]
    prev = c.groupby(key, sort=False).shift(1)
    ret = c / prev - 1

    def roll(s, w, fn):
        r = s.groupby(key, sort=False).rolling(w, min_periods=max(3, w // 2))
        return getattr(r, fn)().reset_index(level=0, drop=True)

    F = {}
    for w in (5, 20, 60, 120):
        F[f"ret_{w}"] = (c / c.groupby(key, sort=False).shift(w) - 1) * 100
    F["vol_20"] = roll(ret, 20, "std") * 100
    F["vol_60"] = roll(ret, 60, "std") * 100
    tv = P["trading_value"].where(P["trading_value"] > 0)
    F["log_tv20"] = np.log(roll(tv, 20, "mean"))
    F["log_mcap"] = np.log(P["market_cap"].where(P["market_cap"] > 0))
    F["amihud_20"] = roll(ret.abs() / tv * 1e9, 20, "mean")
    F["dist_high250"] = (c / roll(h, 250, "max") - 1) * 100
    F["dist_low250"] = (c / roll(lo, 250, "min") - 1) * 100
    F["ma20_gap"] = (c / roll(c, 20, "mean") - 1) * 100
    F["ma60_gap"] = (c / roll(c, 60, "mean") - 1) * 100
    hi20, lo20 = roll(h, 20, "max"), roll(lo, 20, "min")
    F["rsv_20"] = (c - lo20) / (hi20 - lo20)
    F["gap_1d"] = (o / prev - 1) * 100
    rng = (h - lo).where(h > lo)
    F["kmid"] = (c - o) / o * 100
    F["kup"] = (h - np.maximum(o, c)) / rng
    F["klow"] = (np.minimum(o, c) - lo) / rng
    F["skew_20"] = roll(ret, 20, "skew")
    F["turn_ratio_5_60"] = roll(tv, 5, "mean") / roll(tv, 60, "mean")
    for f in PRICE_FEATS:
        out[f] = F[f].replace([np.inf, -np.inf], np.nan).astype("float32")
    return out


def panel(daily, codes=None):
    """일봉 → 종목·날짜마다 가격 피처 20개(거래된 날만)."""
    return _compute(_prep(daily, codes))


def attach(D, daily=None, pan=None):
    """D(code·date 열 — date = 신호일) 에 가격 피처 20개 열을 더해 돌려준다(D 를 직접 바꿈 — 큰 학습표를 복사하지 않으려고).
    pan = 미리 계산한 panel(daily)(여러 번 붙일 때 한 번만 계산), 없으면 daily 로 D 의 종목만 계산.
    행 순서·개수 그대로. 그날 거래가 없었거나 일봉에 없는 쌍은 결측(NaN — 모델이 결측 그대로 처리)."""
    if len(D) == 0:
        for f in PRICE_FEATS:
            D[f] = np.array([], dtype="float32")
        return D
    key = pd.DataFrame({"code": D["code"].astype(str).str.zfill(6).to_numpy(), "date": _d8(D["date"]).to_numpy()})
    M = key.merge(pan if pan is not None else panel(daily, set(key["code"])), on=["code", "date"], how="left")
    if len(M) != len(D):
        raise RuntimeError(f"가격 피처 붙이기 행 수 불일치 {len(M)} != {len(D)}")
    for f in PRICE_FEATS:
        D[f] = M[f].to_numpy()
    return D


def at_date(daily, codes, date):
    """라이브: {종목: {피처: 값 또는 None}} — date(신호일) 행이 있는 종목만. 종목마다 date 이전 거래된 날 HISTORY 행만으로 계산
    (전체로 잰 값과 같음). 날짜가 없거나 계산 못 한 종목은 빠진다(채점은 결측 그대로)."""
    d8 = str(date).replace("-", "")[:8]
    P = _prep(daily, codes)
    P = P[P["date"] <= d8]
    if P.empty:
        return {}
    P = P.groupby("code", sort=False).tail(HISTORY).reset_index(drop=True)
    F = _compute(P)
    F = F[F["date"] == d8]
    return {r[0]: {f: (None if v != v else float(v)) for f, v in zip(PRICE_FEATS, r[2:])}
            for r in F.itertuples(index=False, name=None)}
