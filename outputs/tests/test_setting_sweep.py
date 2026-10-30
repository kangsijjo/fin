# -*- coding: utf-8 -*-
"""§87 — 강도·손절·익절 탐색(setting_sweep): 시뮬 규칙·권장/유지 판정·현재 설정·API·연결."""
import json
import os
from types import SimpleNamespace

import numpy as np
import pandas as pd

import setting_sweep as S

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


def _rows(n=3000, seed=0, days=230):
    rng = np.random.default_rng(seed)
    d = pd.bdate_range("2024-01-01", periods=days + 25).strftime("%Y%m%d")
    out = []
    for _ in range(n):
        k = int(rng.integers(0, days))
        out.append({"entry_date": d[k], "exit_date": d[k + int(rng.integers(1, 20))], "code": f"{int(rng.integers(0, 250)):06d}",
                    "net": float(rng.normal(0.3, 8)), "score": float(rng.normal())})
    return pd.DataFrame(out)


def test_year_sim_matches_capital_simulator():
    import capital_simulator as CS
    R = _rows()
    ret, mdd, n = S.year_sim(R)
    trades = [SimpleNamespace(entry_date=r.entry_date, exit_date=r.exit_date, code=r.code, entry_price=1.0,
                              net_pct=r.net, gross_pct=r.net, score=r.score) for r in R.itertuples()]
    ref = CS.simulate_capital(trades, initial_capital=1.0, max_concurrent=10)
    assert abs(ret - ref["total_ret_pct"]) < 0.05 and n == ref["taken_n"], (ret, ref)


def test_choose_recommends_only_consistent_improvement():
    yrs = ["2021", "2022", "2023", "2024", "2025"]
    base = {y: (5.0, -20.0, 40) for y in yrs}
    good = {y: (12.0, -15.0, 30) for y in yrs}                       # 해마다 꾸준히 더 좋음
    lucky = {"2021": (80.0, -10, 30), "2022": (-30.0, -40, 30), "2023": (-20.0, -40, 30),
             "2024": (-10.0, -30, 30), "2025": (-5.0, -30, 30)}       # 첫해만 대박
    thin = {y: (50.0, -5.0, 2) for y in yrs}                         # 매매가 너무 적음 → 제외
    T = {S._key(None, None, None): base, S._key(5.0, -20.0, None): good,
         S._key(5.5, -20.0, None): good, S._key(5.0, -15.0, None): good, S._key(6.0, None, None): thin}
    r = S.choose(T, yrs)
    assert r["status"] == "권장" and r["best"] == {"th": 5.0, "stop": -20.0, "take": None} or r["best"]["th"] in (5.0, 5.5)
    assert r["wins"].split("/")[0] == r["wins"].split("/")[1]
    T2 = {S._key(None, None, None): base, S._key(4.0, None, 50.0): lucky}
    r2 = S.choose(T2, yrs)
    assert r2["status"] == "유지", "한 해만 좋았던 조합은 권장하지 않는다"


def test_current_settings_reads_both_configs():
    cur = S.current_settings()
    assert cur, "기본 설정(테스트 임시 경로)에서도 전략이 있어야"
    rows = [r for v in cur.values() for r in v]
    assert {r["place"] for r in rows} >= {"kiwoom", "kis"}
    import live_config as LC
    A = LC.load()["accounts"]["kis"]
    for s in A["strategies"]:
        r = next(x for x in cur[s["engine"]] if x["place"] == "kis")
        assert r["th"] == (None if s["exempt"] else (s.get("min_strength") if s.get("min_strength") is not None else A["min_strength"]))
        assert r["stop"] == s.get("stop")


def test_api_attaches_advice(monkeypatch):
    import integrated_dashboard_server as d
    import strategy_reco as SR
    sw = {"generated": "2026-11-01 05:00", "from_year": 2021, "rule": {},
          "engines": {"good": {"families": {"v2": {"status": "권장", "best": {"th": 5.0, "stop": -20.0, "take": None},
                                                   "best_stats": {"cagr": 9.0}, "base_stats": {"cagr": 4.0}, "wins": "4/5"}},
                               "current": [{"place": "kiwoom", "family": "v2", "th": 5.0, "stop": None, "take": None,
                                            "stats": {"cagr": 6.0}}]}}}
    monkeypatch.setattr(d, "_sweep_data", lambda: sw)
    monkeypatch.setattr(d, "_place_family", lambda: {"kiwoom": "v2", "kis": "v1", "ai": "ai", "strength": "v2"})
    monkeypatch.setattr(d, "_reco_eval", lambda: ({"good": {"score": 0.9, "cagr": 5.0}}, {"n_jobs": 1, "full": None}))
    monkeypatch.setattr(SR, "recommend", lambda pl, sel, engines=None, ev=None: ([{"engine": "good", "score": 90}], {}))
    with d.app.test_client() as c:
        r = c.get("/api/strategy_reco?places=" + json.dumps({"kiwoom": ["good"], "kis": ["good"]}), environ_base=LOCAL).get_json()
    a = r["places"]["kiwoom"]["selected"][0]["advice"]
    assert a["status"] == "권장" and a["best"]["stop"] == -20.0 and a["current"]["stats"]["cagr"] == 6.0
    assert r["places"]["kiwoom"]["recos"][0]["advice"]["status"] == "권장"
    assert "v1" in r["places"]["kis"]["selected"][0]["advice"]["note"], "v1 계좌는 탐색 안 함 안내"
    assert r["sweep"]["generated"] == "2026-11-01 05:00"


def test_wiring():
    bat = open(os.path.join(HERE, "run_ai_pipeline.bat"), "rb").read().decode("ascii")
    assert '-u setting_sweep.py' not in bat, "[§88] 일요일 파이프라인에서 빠지고 토요일 따로"
    sb = open(os.path.join(HERE, "run_setting_sweep.bat"), "rb").read().decode("ascii")
    assert '-u setting_sweep.py --fine' in sb and "%p" not in sb
    ps = open(os.path.join(HERE, "register_tasks.ps1"), encoding="utf-8").read()
    assert 'StockAI\\SettingSweep' in ps and "-DaysOfWeek Saturday" in ps and "run_setting_sweep.bat" in ps
    src = open(os.path.join(HERE, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert '"setting_sweep": [_VENV_PYTHON, str(BASE / "setting_sweep.py"), "--fine"]' in src
    assert "runTask('setting_sweep'" in src and '"setting_sweep"}' in src
    assert "function lcApplyAdv" in src and "function vcApplyAdv" in src and "function _advHtml" in src


def test_fine_refines_around_best_and_smoothing_prefers_plateau():
    yrs = ["2021", "2022", "2023", "2024"]
    base = {y: (2.0, -20.0, 40) for y in yrs}
    T = {S._key(None, None, None): base}
    for th in [None] + S.TH:
        for st in S.STOPS:
            for tk in S.TAKES:
                k = S._key(th, st, tk)
                if k in T:
                    continue
                g = 6.0 if (th == 5.0 and st == -20.0 and tk is None) else 2.5
                T[k] = {y: (g, -20.0, 40) for y in yrs}
    more, seeds = S.fine_combos(T, yrs)
    assert S._key(5.0, -20.0, None) in seeds
    assert S._key(4.8, -18.0, None) in more and S._key(5.3, -22.0, None) in more, "강도 0.1·손절 1%p 둘레"
    # 한 점만 튀는 곳(4.6) vs 넓게 좋은 구간(5.0 둘레) — 주변 평균으로 넓은 쪽을 고른다
    for k in more:
        th, st, tk = k
        near = th is not None and st is not None and tk is None and abs(th - 5.0) <= 0.2 and abs(st + 20) <= 2
        T[k] = {y: ((5.5 if near else 2.5), -20.0, 40) for y in yrs}       # 기본(2.0)보다 조금 나은 바탕 + 넓게 좋은 구간
    T[S._key(4.6, -20.0, None)] = {y: (9.0, -20.0, 40) for y in yrs}
    r = S.choose(T, yrs)
    assert abs(r["best"]["th"] - 5.0) <= 0.2 and r["status"] == "권장", r["best"]


def test_reco_uses_sweep_out_of_sample(tmp_path, monkeypatch):
    import strategy_reco as SR
    sw = {"generated": "2026-10-31 05:00", "engines": {
        "aa": {"families": {"v2": {"oos_years": {"2022": [20, -10, 1], "2023": [15, -8, 1], "2024": [10, -9, 1]}}}},
        "bb": {"families": {"v2": {"oos_years": {"2022": [-20, -40, 1], "2023": [-5, -30, 1], "2024": [-10, -35, 1]}}}}}}
    p = tmp_path / "sw.json"
    p.write_text(json.dumps(sw), encoding="utf-8")
    monkeypatch.setenv("SETTING_SWEEP_PATH", str(p))
    monkeypatch.setenv("PBT_DIR", str(tmp_path / "none"))
    monkeypatch.setattr(SR, "HERE", str(tmp_path))
    obs, info = SR.load_observations(now=pd.Timestamp("2026-11-01").to_pydatetime())
    assert info["sweep"]["n"] == 2
    sc = {o["engine"]: o["score"] for o in obs}
    assert sc["aa"] > sc["bb"]
