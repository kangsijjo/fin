# -*- coding: utf-8 -*-
"""강도 v2(전략별 학습) — 2026-10-22, 설명서 §62

고정하는 것: 방향이 해마다 뒤집히는 지표·시장 공통 지표는 버린다 / 표본 적으면 0 쪽으로 축소 / 학습 부족이면 모델 없음 /
워크포워드는 그 해 이전만 / 라이브 채점은 저장 모델로 / 설정 strength_version / 트레이더는 v2 우선·없으면 v1 / 연계(로그·API·주간 학습)."""
import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
ACTION = {"X-Dashboard-Action": "1"}


def _toy(n_per_year=200, years=("2019", "2020", "2021", "2022"), seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for k, y in enumerate(years):
        for i in range(n_per_year):
            good = rng.normal()                       # 해마다 같은 방향(+)
            flip = rng.normal()                       # 해마다 방향이 바뀜
            mkt = rng.normal()                        # 시장 공통 지표
            ret = 2 * good + (1 if k % 2 == 0 else -1) * 2 * flip + 2 * mkt + rng.normal()
            rows.append({"year": y, "entry_date": f"{y}0601", "net_pct": ret, "rsi14": good, "atr_pct": flip,
                         "kospi_ret_20d": mkt})
    return pd.DataFrame(rows)


def test_fit_keeps_stable_drops_flipping_and_market():
    import strength_model as M
    m = M.fit(_toy())
    assert set(m["ic"]) == {"rsi14"}, m["ic"]
    raw = 0.5
    assert 0 < m["ic"]["rsi14"] < raw and m["n"] == 800
    assert len(m["ref"]["rsi14"]) == 1001
    assert M.fit(_toy(n_per_year=50, years=("2019", "2020"))) is None, "학습 300건 미만이면 모델 없음"


def test_score_frame_direction_and_live_score(tmp_path, monkeypatch):
    import strength_model as M
    monkeypatch.setattr(M, "MODEL_DIR", str(tmp_path))
    m = M.fit(_toy())
    json.dump(m, open(M.model_path("t"), "w"))
    hi = M.score("t", {"rsi14": 2.0})
    lo = M.score("t", {"rsi14": -2.0})
    assert hi > 5 > lo
    assert M.score("nomodel", {"rsi14": 1.0}) is None


def test_walk_forward_uses_prior_years_only(monkeypatch):
    import strength_model as M
    seen = []
    real = M.fit
    monkeypatch.setattr(M, "fit", lambda T: (seen.append(sorted(T["year"].unique())) or real(T)))
    T = _toy(years=("2019", "2020", "2021", "2022", "2023"))
    X = pd.DataFrame({"rsi14": [0.0, 1.0]})
    sc, info = M.walk_forward(T, X, ["2022", "2023"])
    assert seen == [["2019", "2020", "2021"], ["2019", "2020", "2021", "2022"]]
    assert not np.isnan(sc).any() and info["2023"]["n_ic"] >= 1


def test_live_config_strength_version(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    import live_config as LC
    assert LC.account("kiwoom")["strength_version"] == "v1", "기본은 종전(v1)"
    c = LC.default_config()
    c["accounts"]["kis"]["strength_version"] = "v3"
    assert any("강도 방식" in e for e in LC.validate(c))
    c["accounts"]["kis"]["strength_version"] = "v2"
    cfg, ch = LC.save(c)
    assert LC.account("kis")["strength_version"] == "v2" and any("강도 방식 v1 → v2" in x for x in ch)


def test_trader_uses_v2_with_v1_fallback(tmp_path, monkeypatch):
    import kis_trader as kx
    monkeypatch.chdir(tmp_path)
    os.makedirs("db")
    pd.DataFrame([
        {"signal_date": "20261022", "account": "KIS_안D", "strategy": "gc_for3d", "code": "000001", "score_ic": 3.0, "score_v2": 6.1},
        {"signal_date": "20261022", "account": "KIS_안D", "strategy": "for_high20_mkt", "code": "000002", "score_ic": 4.0, "score_v2": ""},
    ]).to_csv("db/signal_strength_log.csv", index=False, encoding="utf-8-sig")
    sigs = [{"signal_date": "20261022", "code": "000001"}, {"signal_date": "20261022", "code": "000002"}]
    monkeypatch.setattr(kx, "STRENGTH_VERSION", "v1")
    assert kx._strength_map(sigs) == {("000001", "gc_for3d"): 3.0, ("000002", "for_high20_mkt"): 4.0}
    monkeypatch.setattr(kx, "STRENGTH_VERSION", "v2")
    assert kx._strength_map(sigs) == {("000001", "gc_for3d"): 6.1, ("000002", "for_high20_mkt"): 4.0}


def test_wiring_logger_pipeline_and_api():
    import strength_logger as SL
    assert "score_v2" in SL._FIELDS
    bat = open(os.path.join(OUTPUTS, "run_ai_pipeline.bat"), "rb").read().decode("ascii")
    assert "strength_model.py --train" in bat
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert 'id="pbt-sver"' in src and "lcSetAcc('${a}','strength_version'" in src and '"--strength-version", "v2"' in src
    rg = open(os.path.join(OUTPUTS, "regen_signals.py"), encoding="utf-8").read()
    assert '"--missing-only"' in rg


def test_pbt_api_passes_strength_version(monkeypatch):
    from types import SimpleNamespace as NS
    import integrated_dashboard_server as d
    d.app.config["TESTING"] = True
    calls = []
    monkeypatch.setattr(d._sp, "Popen", lambda cmd, **kw: calls.append(cmd) or NS(poll=lambda: None, pid=1))
    monkeypatch.setattr(d, "_PBT_PROC", {"proc": None, "job": None})
    body = {"start": "20240101", "end": "20241231", "names": ["gc_for3d"], "strength": "5.5", "strength_version": "v2"}
    with d.app.test_client() as c:
        assert c.post("/api/pbt/run", json={**body, "strength_version": "x"}, environ_base=LOCAL, headers=ACTION).status_code == 400
        assert c.post("/api/pbt/run", json=body, environ_base=LOCAL, headers=ACTION).get_json()["ok"]
    assert calls[0][calls[0].index("--strength-version") + 1] == "v2"
