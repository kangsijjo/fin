# -*- coding: utf-8 -*-
"""§90 — 새 매매법 후보 13종(strategies/candidates_2026): 시험만 하고 기준 미달로 엔진에는 등록하지 않음."""
import numpy as np
import pandas as pd

from strategies import candidates_2026 as C


def test_thirteen_unique_and_not_registered():
    import strategy_engine as E
    cs = C.candidates()
    names = [s.name for s in cs]
    assert len(cs) == 13 and len(set(names)) == 13
    eng = {s.name for s in E.ALL_STRATEGIES}
    assert not (set(names) & eng), "사전 기준(전체 CAGR>0 · 최근 2년>0) 통과 0종 → 엔진 목록에 넣지 않음(§90)"


def test_lookback_covers_signal_window():
    from strategies._swing_base import signal_lookback
    by = {s.name: s for s in C.candidates()}
    assert signal_lookback(by["trend_tmpl"]) >= 252
    assert signal_lookback(by["ichimoku_bk"]) >= 78
    assert signal_lookback(by["rsi2_pb"]) >= 200 and signal_lookback(by["lowvol_trend"]) >= 200
    assert signal_lookback(by["uplimit_next"]) == 2


def test_fast_roll_equals_groupby_rolling():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"code": np.repeat(["a", "b", "c"], [30, 5, 40]),
                       "date": np.concatenate([np.arange(30), np.arange(5), np.arange(40)]).astype(str)})
    df["date"] = df["date"].str.zfill(8)
    df["x"] = rng.normal(size=len(df))
    df = C._prep(df)
    for fn in ("mean", "max", "min", "std", "sum"):
        for sh in (0, 1):
            a = C._roll(df, "x", 7, fn, shift=sh)
            s = df.groupby("code")["x"].shift(sh) if sh else df["x"]
            b = getattr(s.groupby(df["code"]).rolling(7, min_periods=7), fn)().reset_index(level=0, drop=True).reindex(df.index)
            assert np.allclose(a.fillna(-999), b.fillna(-999)), (fn, sh)


def test_strategies_run_on_small_frame():
    rng = np.random.default_rng(1)
    rows = []
    dates = pd.bdate_range("2020-01-01", periods=320).strftime("%Y%m%d")
    for k in range(12):
        p = 10000 * np.cumprod(1 + rng.normal(0.001, 0.03, len(dates)))
        for i, d in enumerate(dates):
            rows.append({"code": f"{k:06d}", "date": d, "open": p[i] * 0.995, "high": p[i] * 1.02, "low": p[i] * 0.98,
                         "close": p[i], "volume": 1e6 * (1 + rng.random()), "trading_value": 5e9,
                         "change_pct": 0.0 if i == 0 else (p[i] / p[i - 1] - 1) * 100, "market_cap": 1e11,
                         "inst_net": rng.normal(0, 1e8), "foreign_net": rng.normal(0, 1e8), "name": f"s{k}"})
    df = pd.DataFrame(rows)
    import strategy_engine as E
    for s in C.candidates():
        tr = s.backtest(df.copy(), E.DEFAULT_COSTS)
        assert isinstance(tr, list)
        for t in tr[:3]:
            assert t.entry_date > "20200101" and t.holding_days >= 1
