# -*- coding: utf-8 -*-
"""§93 — 파이프라인을 돌려도 v2 학습이 안 되던 것.

실사고(2026-11-02): 데이터 캐시가 개인 순매수 없이 만들어져(분석 환경이 stock.db 를 못 읽은 채 다시 만듦) supply 계열이
매매 0건 → strength_model.train 이 supply_abc 를 '기록 없이' 건너뛰고 학습표를 빈 표로 덮어씀 → 개선 AI 가 58개로 학습.
또 주간 v2 학습이 모의계좌 전략만이라 강도매매 탭 전략은 처음 저장 때 한 번만 학습됐다.
"""
import json
import os
import time
from types import SimpleNamespace as NS

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
ACTION = {"X-Dashboard-Action": "1"}


def _daily(d):
    d.mkdir()
    for day, px in (("20260101", 100), ("20260102", 110)):
        pd.DataFrame({"code": ["000010", "000020"], "open": [px] * 2, "high": [px] * 2, "low": [px] * 2,
                      "close": [px, px + 1], "volume": [1000, 2000], "trading_value": [3e9, 4e9],
                      "change_pct": [1.0, -1.0], "시가총액": [1e10, 2e10]}).to_csv(d / f"{day}.csv", index=False,
                                                                                encoding="utf-8-sig")


def test_loader_does_not_cache_when_individual_join_fails(tmp_path, monkeypatch):
    import fin_paths
    from strategies import daily_loader as dl
    d = tmp_path / "daily"
    _daily(d)
    monkeypatch.setattr(dl, "DATA_DIR", str(d))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(fin_paths, "STOCK_DB", str(tmp_path / "nope" / "stock.db"))      # 열 수 없는 DB
    full = dl.load_macro_daily()
    assert full["individual_net"].isna().all()
    assert not (d / "_daily_cache.pkl").exists() and not (d / "_daily_cache.sig.json").exists(), \
        "조인 실패한 표를 캐시하면 다음 일봉까지 모든 프로세스가 supply 계열 0건(10-31 실사고)"
    import sqlite3
    db = tmp_path / "stock.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE supply_demand (date TEXT, ticker TEXT, individual_net_value REAL)")
    con.execute("INSERT INTO supply_demand VALUES ('2026-01-02', '000010', -5e8)")
    con.commit()
    con.close()
    monkeypatch.setattr(fin_paths, "STOCK_DB", str(db))
    full = dl.load_macro_daily()
    assert full["individual_net"].notna().sum() == 1
    assert (d / "_daily_cache.pkl").exists(), "조인이 되면 종전처럼 캐시"


def test_v2_train_keeps_usable_table_and_logs_failure(tmp_path, monkeypatch):
    import strength_model as SM
    monkeypatch.setattr(SM, "MODEL_DIR", str(tmp_path))
    good = pd.DataFrame({"net_pct": [1.0] * (SM.MIN_TRAIN + 5), "year": ["2024"] * (SM.MIN_TRAIN + 5)})
    good.to_pickle(SM.train_path("keep_me"))
    tiny = pd.DataFrame({"net_pct": [1.0, 2.0], "year": ["2024", "2024"]})
    monkeypatch.setattr(SM, "build_train_table", lambda e, df, log=print: tiny.copy())
    logs = []
    out = SM.train(["keep_me", "new_one"], df=pd.DataFrame({"date": ["20260102"]}), log=logs.append)
    assert len(pd.read_pickle(SM.train_path("keep_me"))) == SM.MIN_TRAIN + 5, "쓸 수 있던 학습표를 빈 표로 덮지 않음"
    assert "기존 학습표·모델 유지" in out["keep_me"]["why"] and "v1 로 대체" in out["new_one"]["why"]
    assert len(pd.read_pickle(SM.train_path("new_one"))) == 2, "기존 표가 없으면 종전처럼 저장"
    assert sum("[strength_v2] keep_me" in x for x in logs) == 1 and sum("[strength_v2] new_one" in x for x in logs) == 1, \
        "학습 못 한 전략도 로그에 남김(종전엔 continue 로 건너뜀)"


def test_v2_targets_include_strength_tab(tmp_path, monkeypatch):
    import copy
    import strength_model as SM
    import virtual_config as VC
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    monkeypatch.setenv("VIRTUAL_CONFIG_PATH", str(tmp_path / "vc.json"))
    ports = VC.default_portfolios()
    ports["strength"]["strength_version"] = "v2"
    ports["strength"]["strategies"][0]["engine"] = "bb_break"
    VC.save(copy.deepcopy(ports))
    eng = SM.configured_engines()
    assert "bb_break" in eng, "강도매매 탭(강도 v2 포트) 전략도 주간 v2 학습 대상"
    import live_config as LC
    assert set(s["engine"] for a in LC.ACCOUNTS for s in LC.load()["accounts"][a]["strategies"]) <= set(eng)
    ports["strength"]["strength_version"] = "v1"
    ports["strength"]["strategies"][0]["engine"] = "momentum_5d"
    monkeypatch.setenv("VIRTUAL_CONFIG_PATH", str(tmp_path / "vc2.json"))
    VC.save(copy.deepcopy(ports))
    assert "momentum_5d" not in SM.configured_engines(), "강도 포트가 v1 이면 v2 학습 대상 아님"


@pytest.fixture(scope="module")
def dash():
    import integrated_dashboard_server as d
    d.app.config["TESTING"] = True
    return d


def test_dashboard_v2_button_group_and_unbuffered_logs(dash, monkeypatch, tmp_path):
    assert dash._RUN_TASKS["strength_v2"][-2:] == [str(dash.BASE / "strength_model.py"), "--train"]
    assert "strength_v2" in dash._AI_GROUP, "학습표를 개선 AI·설정 탐색과 같이 쓰므로 동시 실행 막음"
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert "runTask('strength_v2','강도 v2 학습')" in src
    calls = []
    monkeypatch.setattr(dash._sp, "Popen", lambda cmd, **kw: calls.append(kw) or NS(poll=lambda: None, pid=7))
    monkeypatch.setattr(dash, "_RUN_PROCS", {})
    monkeypatch.setattr(dash, "BASE", tmp_path)
    with dash.app.test_client() as c:
        r = c.post("/api/run/strength_v2", environ_base=LOCAL, headers=ACTION).get_json()
    assert r["ok"] and calls[0]["env"].get("PYTHONUNBUFFERED") == "1", "도는 동안 로그가 바로 보이게"
    bat = open(os.path.join(OUTPUTS, "run_ai_pipeline.bat"), "rb").read()
    assert b"strength tab v2 portfolio" in bat and all(c < 128 for c in bat), "bat 는 ASCII 유지"
