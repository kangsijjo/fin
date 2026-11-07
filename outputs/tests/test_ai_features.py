# -*- coding: utf-8 -*-
"""§97 — 개선 AI 가격 피처(ai_features)·회귀+분류 앙상블(ai_model)·연결(로거·기간 백테스트·설정 탐색·대시보드)."""
import os

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _daily(n=420, codes=("000001", "000002"), seed=0, halts=(50, 51, 52, 300)):
    """load_macro_daily 형식 가짜 일봉 — halts 날은 거래 없음(시가·고가·저가 0, 종가 이월)."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2023-01-02", periods=n).strftime("%Y%m%d")
    rows = []
    for k, c in enumerate(codes):
        px = 1000.0 * (k + 1)
        for i, d in enumerate(days):
            px *= 1 + rng.normal(0, 0.02)
            o = px * (1 + rng.normal(0, 0.005))
            h, lo = max(o, px) * 1.01, min(o, px) * 0.99
            r = dict(code=c, date=d, open=o, high=h, low=lo, close=px, volume=1000, trading_value=px * 1e5,
                     market_cap=px * 1e7)
            if i in halts:
                r.update(open=0, high=0, low=0, volume=0, trading_value=0.0)
            rows.append(r)
    return pd.DataFrame(rows)


def test_panel_definitions_and_no_trade_rows_skipped():
    import ai_features as AF
    D = _daily()
    P = AF.panel(D)
    assert list(P.columns) == ["code", "date"] + list(AF.PRICE_FEATS)
    assert len(P) == len(D) - 2 * 4, "거래 없던 날 행은 뺀다"
    g = D[(D["open"] > 0)].copy()
    g = g[g["code"] == "000001"].reset_index(drop=True)
    p = P[P["code"] == "000001"].reset_index(drop=True)
    i = 350
    c = g["close"]
    assert p.loc[i, "ret_5"] == pytest.approx((c[i] / c[i - 5] - 1) * 100, rel=1e-5)
    ret = c / c.shift(1) - 1
    assert p.loc[i, "vol_20"] == pytest.approx(ret[i - 19:i + 1].std() * 100, rel=1e-4)
    assert p.loc[i, "dist_high250"] == pytest.approx((c[i] / g["high"][i - 249:i + 1].max() - 1) * 100, rel=1e-4)
    assert p.loc[i, "dist_low250"] > 0, "0원 저가(거래 없던 날)가 250일 저가가 되면 안 된다"
    hi, lo = g["high"][i - 19:i + 1].max(), g["low"][i - 19:i + 1].min()
    assert p.loc[i, "rsv_20"] == pytest.approx((c[i] - lo) / (hi - lo), rel=1e-4)
    assert p.loc[i, "gap_1d"] == pytest.approx((g["open"][i] / c[i - 1] - 1) * 100, rel=1e-4)


def test_live_at_date_equals_full_panel():
    """라이브(종목마다 마지막 HISTORY 행만)와 학습(전체 이력) 값이 같아야 한다 — 학습·라이브 잣대 일치."""
    import ai_features as AF
    D = _daily(n=520)
    d = sorted(D["date"].unique())[-30]
    live = AF.at_date(D, ["000001", "2"], d)                   # 코드 0 채움도 처리
    P = AF.panel(D)
    for c in ("000001", "000002"):
        row = P[(P["code"] == c) & (P["date"] == d)].iloc[0]
        for f in AF.PRICE_FEATS:
            a, b = live[c][f], float(row[f])
            assert (a is None and b != b) or a == pytest.approx(b, rel=1e-5, abs=1e-6), (c, f, a, b)
    assert AF.at_date(D, ["000001"], "20990101") == {}, "그날 행이 없으면 빠진다(결측으로 채점)"


def test_attach_keeps_order_and_missing_is_nan():
    import ai_features as AF
    D = _daily()
    dates = sorted(D["date"].unique())
    X = pd.DataFrame({"code": ["000002", "000001", "999999", "000001"],
                      "date": [dates[310], dates[400], dates[400], dates[51]]})       # 51 = 거래 없던 날
    out = AF.attach(X, D)
    assert len(out) == 4 and list(out["code"]) == ["000002", "000001", "999999", "000001"]
    assert out.loc[0, "ret_5"] == out.loc[0, "ret_5"] and np.isnan(out.loc[2, "ret_5"]) and np.isnan(out.loc[3, "ret_5"])
    pan = AF.panel(D)
    out2 = AF.attach(X[["code", "date"]].copy(), pan=pan)
    assert np.allclose(out[list(AF.PRICE_FEATS)].to_numpy(float), out2[list(AF.PRICE_FEATS)].to_numpy(float), equal_nan=True)


@pytest.fixture
def aim(tmp_path, monkeypatch):
    import ai_model as A
    monkeypatch.setenv("AI_MODEL_DIR", str(tmp_path / "ai_model"))
    monkeypatch.setattr(A, "SEEDS", (1, 2))
    monkeypatch.setattr(A, "PARAMS", dict(A.PARAMS, n_estimators=30))
    A._CACHE.clear()
    return A


def _table(A, n=3000, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for e in ("eng_a", "eng_b"):
        d = pd.bdate_range("2019-01-01", periods=n // 2)
        x = rng.normal(0, 10, n // 2)
        net = -x * 0.3 + rng.normal(0, 3, n // 2)
        for i in range(n // 2):
            r = {f: rng.normal() for f in A.feature_list()}
            r.update(vol_20=x[i], net_pct=net[i], code=f"{i:06d}", date=d[i].strftime("%Y%m%d"),
                     entry_date=d[i].strftime("%Y%m%d"), exit_date=(d[i] + pd.Timedelta(days=28)).strftime("%Y%m%d"),
                     engine=e)
            rows.append(r)
    T = pd.DataFrame(rows)
    T["ym"] = T["entry_date"].str[:6]
    T["year"] = T["entry_date"].str[:4]
    T["y_rank"] = T.groupby(["engine", "ym"])["net_pct"].rank(pct=True) - 0.5
    return T


def test_ensemble_fit_save_load_and_scale(aim):
    import ai_features as AF
    T = _table(aim)
    assert set(AF.PRICE_FEATS) <= set(aim.feature_list())
    m, meta = aim.fit(T)
    assert meta["ensemble"] == "회귀+분류" and len(m.cls) == 2 and set(meta["ref_a"]) == {"eng_a", "eng_b"}
    s = aim.score_frame(T, m, meta)
    assert 0 <= np.nanmin(s) and np.nanmax(s) <= 10
    assert abs(np.nanmedian(s[T["engine"].to_numpy() == "eng_b"]) - 5) < 0.6, "평균한 뒤에도 5 = 그 전략 보통"
    assert aim.score_frame(T[T["vol_20"] < -8], m, meta).mean() > aim.score_frame(T[T["vol_20"] > 8], m, meta).mean() + 2
    aim._save(m, meta)
    m2, meta2 = aim._load()
    assert len(m2.cls) == 2 and np.allclose(aim.score_frame(T.head(200), m2, meta2), s[:200])
    assert os.path.exists(os.path.join(aim.model_dir(), "model.c0.json"))


def test_old_model_without_classifier_still_scores(aim):
    """배포 직후 재학습 전엔 옛 모델(회귀만·가격 피처 없음)이 그대로 채점돼야 한다."""
    T = _table(aim)
    base = [f for f in aim.feature_list() if f not in __import__("ai_features").PRICE_FEATS]
    m, meta = aim.fit(T, feats=base)
    m.cls = []
    for k in ("ref_c", "ref_a"):
        meta.pop(k)
    aim._save(m, meta)
    meta_saved = aim.meta_info()
    assert meta_saved["parts_cls"] == []
    v = aim.score("eng_a", {"vol_20": 1.0})
    assert v is not None and 0 <= v <= 10


def test_walk_forward_refuses_x_without_price_features(aim):
    T = _table(aim)
    X = T[["code", "date", "engine"]].head(3)
    with pytest.raises(ValueError):
        aim.walk_forward(T, X, ["2019"] * 3, "eng_a")


def test_wiring():
    src = lambda f: open(os.path.join(HERE, f), encoding="utf-8").read()   # noqa: E731
    sl = src("strength_logger.py")
    assert "_AF.at_date(df," in sl and "{**feats, **pf_map.get(code, {})}" in sl
    pb = src("period_backtest.py")
    assert "pf_pan = AF.panel(df)" in pb and pb.index("pf_pan = AF.panel(df)") < pb.index("df = df[(df[\"date\"] >= cal[max(0, i0 - need - 5)])")
    assert "AF.attach(SB.trade_features(df, allt, log=print), pan=pf_pan)" in pb and "pan=pf_pan)" in pb
    sw = src("setting_sweep.py")
    assert "T_all = AIM.load_tables(pan=pf_pan)" in sw and sw.index("pf_pan = AF.panel(df)") < sw.index('df = df[df["date"] >= f"{FROM_YEAR - 3}0101"]')
    ds = src("integrated_dashboard_server.py")
    assert "from ai_features import LABELS as _PF_LABELS" in ds
    import ai_model as A
    assert A.VERSION >= 3
