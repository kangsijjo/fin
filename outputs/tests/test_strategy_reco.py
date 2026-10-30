# -*- coding: utf-8 -*-
"""§86 — 백테스트 종합 추천(strategy_reco)·백테스트 뒤 AI 학습(ai_model.learn, period_backtest._spawn_learn)."""
import json
import os

import pandas as pd
import pytest

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


def _job(d, name, fin, rows, start="20220101", end="20251231", sv=None, stop=None, strength=None):
    json.dump({"status": "done", "started": fin, "finished": fin,
               "params": {"start": start, "end": end, "eff_end": end, "strength_version": sv, "stop": stop, "take": None,
                          "strength": strength or []},
               "results": rows}, open(os.path.join(d, name), "w", encoding="utf-8"))


def _row(e, cagr, mdd, sh, yearly, th=None):
    r = {"strategy": e if th is None else f"{e} ≥{th}", "base": e, "cagr": cagr, "mdd": mdd, "sharpe": sh,
         "yearly": yearly, "strength_min": th}
    return r


@pytest.fixture
def reco(tmp_path, monkeypatch):
    import strategy_reco as SR
    pbt = tmp_path / "pbt"
    pbt.mkdir()
    (tmp_path / "results").mkdir()
    monkeypatch.setenv("PBT_DIR", str(pbt))
    monkeypatch.setattr(SR, "HERE", str(tmp_path))
    good = {"2022": 5, "2023": 12, "2024": 3, "2025": 8}
    bad = {"2022": -20, "2023": -5, "2024": 2, "2025": -9}
    mid = {"2022": -2, "2023": 4, "2024": -1, "2025": -1}      # 해 플러스 25% → 추천 조건(50%) 미달
    for i, fin in enumerate(("2026-10-25 10:00:00", "2026-10-27 10:00:00", "2026-10-29 10:00:00")):
        rows = [_row("good", 9, -25, 0.8, good), _row("bad", -12, -70, -0.4, bad), _row("mid", 0.5, -40, 0.1, mid)]
        rows += [_row(f"x{k}", -3 + k, -50, 0.0, mid) for k in range(6)]
        rows += [_row("mid", 7, -30, 0.6, good, th=5.0)]          # mid 은 v2 ≥5 에서 좋아짐
        _job(str(pbt), f"pbt_2026102{i}_000000.json", fin, rows, sv="v2", strength=[5.0])
    return SR


def test_evaluate_ranks_consistent_winner(reco):
    ev, info = reco.evaluate(now=pd.Timestamp("2026-10-30").to_pydatetime())
    assert info["n_jobs"] == 3
    assert ev["good"]["score"] > 0.8 and ev["bad"]["score"] < 0.3
    assert ev["mid"]["best_variant"] and json.loads(ev["mid"]["best_variant"])["th"] == 5.0, "강도 줄이 더 좋으면 권장 설정"
    assert ev["good"]["best_variant"] is None


def test_recommend_excludes_selected_and_weak(reco):
    rec, _ = reco.recommend("kiwoom", ["x0"], now=pd.Timestamp("2026-10-30").to_pydatetime())
    names = [r["engine"] for r in rec]
    assert names[0] == "good" and "bad" not in names and "mid" not in names
    rec2, _ = reco.recommend("kiwoom", ["good"], now=pd.Timestamp("2026-10-30").to_pydatetime())
    assert "good" not in [r["engine"] for r in rec2], "이미 고른 전략은 추천 안 함"


def test_old_rule_results_weigh_half(reco, tmp_path):
    obs, _ = reco.load_observations(now=pd.Timestamp("2026-10-30").to_pydatetime())
    w_new = max(o["w"] for o in obs if o["engine"] == "good")
    _job(str(tmp_path / "pbt"), "pbt_20261020_000000.json", "2026-10-20 10:00:00",
         [_row("good", 9, -25, 0.8, {"2023": 1})] + [_row(f"y{k}", 1, -30, 0.1, {"2023": 1}) for k in range(8)])
    obs, _ = reco.load_observations(now=pd.Timestamp("2026-10-30").to_pydatetime())
    w_old = [o["w"] for o in obs if o["engine"] == "good" and o["source"] == "기간 20220101~20251231"]
    assert min(w_old) < w_new * 0.6, "§64 계산 수정 전 결과는 절반 + 오래된 만큼 줄어듦"


def test_learn_skips_when_known_and_trains_otherwise(tmp_path, monkeypatch):
    import ai_model as A
    import strength_model as SM
    monkeypatch.setenv("AI_MODEL_DIR", str(tmp_path / "m"))
    monkeypatch.setattr(SM, "train_path", lambda e: str(tmp_path / f"{e}_train.pkl"))
    (tmp_path / "known_train.pkl").write_bytes(b"x")
    monkeypatch.setattr(A, "meta_info", lambda: {"engines": ["known"]})
    calls = []
    monkeypatch.setattr(A, "_train", lambda v, log, extra: calls.append(extra) or {"ok": True, "n": 1, "engines": 2})
    assert A.learn(["known"])["skipped"] and not calls
    r = A.learn(["known", "newbie"])
    assert r["ok"] and calls == [["known", "newbie"]]
    os.makedirs(A.model_dir(), exist_ok=True)
    open(os.path.join(A.model_dir(), "train.lock"), "w").write("1")
    assert A.train()["busy"], "다른 학습 중이면 건너뜀"


def test_spawn_learn_respects_env_and_needs(monkeypatch):
    import period_backtest as P
    import ai_model as A
    import subprocess
    assert P._spawn_learn(["a"]) is None, "테스트(conftest)에선 PBT_NO_LEARN=1"
    monkeypatch.delenv("PBT_NO_LEARN", raising=False)
    monkeypatch.setattr(A, "meta_info", lambda: {"engines": []})
    got = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: got.append(cmd))
    r = P._spawn_learn(["zzz_engine"])
    assert r["started"] and "--learn" in got[0] and got[0][-1] == "zzz_engine"


def test_lift_rows_fast_equals_slow():
    import numpy as np
    import ai_model as A
    rng = np.random.default_rng(3)
    D = pd.DataFrame({"engine": rng.choice(["a", "b"], 600), "date": rng.choice([f"2024010{i}" for i in range(1, 9)], 600),
                      "Y": 2024, "net_pct": rng.normal(0, 5, 600), "s1": rng.normal(size=600)})
    D.loc[D.sample(50, random_state=1).index, "s1"] = np.nan
    f, s = A._lift_rows(D, ["s1"]), A._lift_rows_slow(D, ["s1"])
    m = f.merge(s, on=["engine", "date"], suffixes=("", "_s"))
    assert len(f) == len(s) and np.allclose(m["s1"], m["s1_s"]) and np.allclose(m["s1_ic"], m["s1_ic_s"], equal_nan=True)


def test_api_and_ui_wiring(monkeypatch):
    import integrated_dashboard_server as d
    import strategy_reco as SR
    monkeypatch.setattr(d, "_reco_eval", lambda: ({"good": {"score": 0.9, "cagr": 5.0}}, {"n_jobs": 1, "full": None}))
    monkeypatch.setattr(SR, "recommend", lambda pl, sel, engines=None, ev=None: ([{"engine": "good", "score": 90}], {}))
    with d.app.test_client() as c:
        r = c.get("/api/strategy_reco?places=" + json.dumps({"kiwoom": ["good"], "bogus": ["x"]}), environ_base=LOCAL).get_json()
    assert r["ok"] and set(r["places"]) == {"kiwoom"} and r["places"]["kiwoom"]["selected"][0]["score"] == 90
    src = open(d.__file__, encoding="utf-8").read()
    assert src.count('class="reco-box"') == 2 and "function lcAddReco" in src and "function vcAddReco" in src
    pb = open(os.path.join(os.path.dirname(d.__file__), "period_backtest.py"), encoding="utf-8").read()
    assert 'state["ai_learn"] = _spawn_learn(names)' in pb and "train_tables[n].to_pickle(p)" in pb
