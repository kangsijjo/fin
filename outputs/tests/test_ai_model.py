# -*- coding: utf-8 -*-
"""개선 AI(§85) — 학습·0~10 점수·모르는 전략·워크포워드 캐시, AI 가상매매 전환일, 연결(로거·파이프라인·감사·대시보드)."""
import os

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def aim(tmp_path, monkeypatch):
    import ai_model as A
    monkeypatch.setenv("AI_MODEL_DIR", str(tmp_path / "ai_model"))
    monkeypatch.setattr(A, "SEEDS", (1, 2))                       # 빠르게
    monkeypatch.setattr(A, "PARAMS", dict(A.PARAMS, n_estimators=30))
    A._CACHE.clear()
    return A


def _table(n=2600, seed=0, per_day=1):
    """두 전략 가짜 학습표 — 'rsi14 낮을수록 좋음' 이 숨어 있는 표(같은 달 안 순위로 배울 수 있어야).
    per_day = 하루 후보 수(검증의 '같은 날 상위 2개'는 후보 3개 이상인 날만 셈)."""
    import ai_model as A
    rng = np.random.default_rng(seed)
    rows = []
    for e in ("eng_a", "eng_b"):
        d = pd.bdate_range("2019-01-01", periods=-(-n // 2 // per_day), freq="B").repeat(per_day)[:n // 2]
        x = rng.normal(50, 10, n // 2)
        net = -(x - 50) * 0.3 + rng.normal(0, 3, n // 2)
        for i in range(n // 2):
            r = {f: rng.normal() for f in A.feature_list()}
            r.update(rsi14=x[i], net_pct=net[i], code=f"{i:06d}", date=d[i].strftime("%Y%m%d"),
                     entry_date=d[i].strftime("%Y%m%d"), exit_date=(d[i] + pd.Timedelta(days=28)).strftime("%Y%m%d"),
                     engine=e)
            rows.append(r)
    T = pd.DataFrame(rows)
    T["ym"] = T["entry_date"].str[:6]
    T["year"] = T["entry_date"].str[:4]
    T["y_rank"] = T.groupby(["engine", "ym"])["net_pct"].rank(pct=True) - 0.5
    return T


def test_fit_score_scale_and_direction(aim):
    T = _table()
    m, meta = aim.fit(T)
    assert m is not None and set(meta["engines"]) == {"eng_a", "eng_b"}
    assert "news_sent_7d" not in meta["feats"], "라이브에서 안 만드는 뉴스 감성은 빼야 한다"
    s = aim.score_frame(T, m, meta)
    assert np.nanmin(s) >= 0 and np.nanmax(s) <= 10
    assert abs(np.nanmedian(s[T["engine"].to_numpy() == "eng_a"]) - 5) < 0.6, "그 전략 과거 기준 5 = 보통"
    lo = T[T["rsi14"] < 40]
    hi = T[T["rsi14"] > 60]
    assert aim.score_frame(lo, m, meta).mean() > aim.score_frame(hi, m, meta).mean() + 2, "숨은 방향을 배워야 한다"


def test_live_score_unknown_engine_and_missing_values(aim):
    T = _table()
    m, meta = aim.fit(T)
    aim._save(m, meta)
    f = {k: None for k in meta["feats"]}
    f["rsi14"] = 30.0
    v = aim.score("eng_a", f)                        # 나머지 결측 → 결측 그대로(0 으로 채우지 않음)
    assert v is not None and 0 <= v <= 10
    assert aim.score("no_such_engine", f) is None, "모델에 없는 전략은 점수 없음(v2/v1 로 대체)"
    assert aim.score("eng_a", {}) is not None


def test_walk_forward_uses_only_past_and_caches(aim):
    T = _table(n=6000)
    X = T[T["year"] == "2024"].reset_index(drop=True)   # 2019~2023 청산분 ≥ MIN_TOTAL_N
    sc, info = aim.walk_forward(T, X[X["engine"] == "eng_a"].reset_index(drop=True),
                                X[X["engine"] == "eng_a"]["year"].tolist(), "eng_a")
    assert np.isfinite(sc).all() and not info["2024"]["skipped"]
    meta = aim._load("wf_2024.json", "wf_2024.meta.json")[1]
    assert meta["n_total"] == int((T["exit_date"] < "20240101").sum()), "2024 채점 모델 = 2024 전에 청산 끝난 매매만"
    mt = os.path.getmtime(os.path.join(aim.model_dir(), "wf_2024.meta.json"))
    aim.walk_forward(T, X.head(5), ["2024"] * 5, "eng_a")
    assert os.path.getmtime(os.path.join(aim.model_dir(), "wf_2024.meta.json")) == mt, "같은 표본이면 캐시 재사용"


def test_validate_reports_ai_and_v2(aim, monkeypatch):
    import strength_model as SM
    monkeypatch.setattr(SM, "fit", lambda T: None)           # v2 없음 → v2 칸 비어도 동작
    T = _table(n=12000, per_day=3)
    monkeypatch.setattr(aim, "VALIDATE_FROM", 2021)
    v = aim.validate(T, log=lambda *a: None)
    assert v and v["overall"]["ai"] is not None and v["overall"]["ai"] > 0
    assert {e["engine"] for e in v["engines"]} == {"eng_a", "eng_b"}


def test_paper_trader_switches_to_score_ai_from_since(monkeypatch):
    import ai_paper_trader as ap
    import virtual_config as VC
    S = ap.AI_SINCE
    prev = (pd.Timestamp(S) - pd.Timedelta(days=3)).strftime("%Y%m%d")
    hist = [{"effective": VC.LEGACY_DATE, "legacy": True, "legacy_min_strength": 5.0, "legacy_exempt": []},
            {"effective": prev, "portfolios": {
                "ai": {"min_score": 0.3, "strength_version": "v2", "strategies": [     # 옛 확률 척도 기준(0.3)
                    {"engine": "gc_for3d", "slots": 10, "exempt": False, "stop": None, "take": None, "holding": 2}]},
                "strength": {"min_score": 5.0, "strength_version": "v2", "strategies": [
                    {"engine": "gc_for3d", "slots": 10, "exempt": False, "stop": None, "take": None, "holding": 2}]}}}]
    cal = sorted({prev, S} | {(pd.Timestamp(S) + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(1, 6)})
    base = dict(name="x", strategy="gc_for3d", holding_days=2, entry_price_close=100.0, score_ic=7.0, score_v2=None)
    sig = pd.DataFrame([
        dict(base, signal_date=prev, code="000001", ai_prob=0.9, score_ai=None),    # 전환 전: 확률 0.9 ≥ 0.3 → 삼
        dict(base, signal_date=prev, code="000002", ai_prob=0.1, score_ai=9.0),     # 전환 전: 확률 0.1 < 0.3 → 안 삼
        dict(base, signal_date=S, code="000003", ai_prob=0.0, score_ai=0.2),        # 전환 후: 옛 기준 무시 → 삼(점수 순)
        dict(base, signal_date=S, code="000004", ai_prob=0.99, score_ai=None)])     # 전환 후 점수 없음 → 안 삼
    sig["ai_score"] = sig["score_ai"].where(sig["signal_date"] >= S, sig["ai_prob"])
    closes = {d: {c: 100.0 for c in ("000001", "000002", "000003", "000004")} for d in cal}
    r = ap.simulate(sig, cal, cal, closes, "ai_score", history=hist, port="ai")
    got = {t["code"] for t in r["trades"]} | {p["code"] for p in r["positions"]}
    assert got == {"000001", "000003"}, got


def test_wiring():
    import strength_logger as SL
    assert "score_ai" in SL._FIELDS and SL._FIELDS.index("score_ai") > SL._FIELDS.index("score_v2")
    bat = open(os.path.join(HERE, "run_ai_pipeline.bat"), "rb").read()
    bat.decode("ascii")
    txt = bat.decode("ascii")
    assert '-u ai_model.py --train' in txt
    assert '"%PYEXE%" -u ai_trainer_v4.py' not in txt, "옛 메타모델 학습은 중단"
    assert txt.index("ai_model.py --train") > txt.index("strength_model.py --train"), "강도 v2 학습표 갱신 뒤에 학습"
    rp = open(os.path.join(HERE, "run_paper.bat"), "rb").read().decode("ascii")
    assert "ai_trainer_v4.py >>" not in rp
    da = open(os.path.join(HERE, "daily_audit.py"), encoding="utf-8").read()
    assert os.path.join("db", "ai_model", "meta.json").replace(os.sep, "/") in da.replace('", "', "/") or '"ai_model", "meta.json"' in da
    import period_backtest  # noqa: F401
    pb = open(os.path.join(HERE, "period_backtest.py"), encoding="utf-8").read()
    assert 'choices=["v1", "v2", "ai"]' in pb
    ds = open(os.path.join(HERE, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert 'if sv not in ("v1", "v2", "ai")' in ds and '<option value="ai">' in ds
    assert '"ai_train":      [_VENV_PYTHON, str(BASE / "ai_model.py"), "--train"]' in ds
