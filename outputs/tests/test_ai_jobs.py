# -*- coding: utf-8 -*-
"""§89 — 데이터+로그 탭 'AI·탐색 작업 상태'(get_ai_jobs_status): 완료·실패·진행 중·오래됨 판정."""
import json
import os
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    import integrated_dashboard_server as d
    base = tmp_path / "outputs"
    (base / "db" / "ai_model").mkdir(parents=True)
    (base / "db" / "period_bt").mkdir(parents=True)
    (base / "logs").mkdir()
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(d, "BASE", base)
    monkeypatch.setattr(d, "_PBT_DIR", base / "db" / "period_bt")
    return d, base, tmp_path / "logs"


def _row(rows, key):
    return next(r for r in rows if key in r["name"])


def test_pipeline_done_failed_running(env):
    d, base, fl = env
    now = datetime(2026, 11, 1, 9, 0)
    p = fl / "ai_pipeline_20261101_0300.log"
    p.write_text("[x] AI pipeline start\n[WARN] full strategy backtest failed\n[x] AI pipeline DONE\n", encoding="utf-8")
    os.utime(p, (now.timestamp() - 3600, now.timestamp() - 3600))
    r = _row(d.get_ai_jobs_status(now)["rows"], "파이프라인")
    assert r["status"] == "ok" and "경고 1건" in r["detail"] and r["when"] == "11-01 03:00"
    p.write_text('[x] AI pipeline start\n[x] AI pipeline FAILED at step "collect" - downstream steps skipped\n', encoding="utf-8")
    r = _row(d.get_ai_jobs_status(now)["rows"], "파이프라인")
    assert r["status"] == "fail" and "collect" in r["detail"]
    p.write_text("[x] AI pipeline start\n[1/5] ...\n", encoding="utf-8")
    os.utime(p, (now.timestamp() - 600, now.timestamp() - 600))
    assert _row(d.get_ai_jobs_status(now)["rows"], "파이프라인")["status"] == "run"


def test_ai_model_and_sweep(env):
    d, base, fl = env
    now = datetime(2026, 11, 20, 9, 0)
    (base / "db" / "ai_model" / "registry.csv").write_text(
        "trained,data_max,n_engines,n_train,top2_ai,top2_v2,dayic_ai,dayic_v2,engines_better,years_better\n"
        "2026-11-01 03:40,20261030,59,2266294,1.07,0.6,0.11,0.08,49/59,5/6\n", encoding="utf-8-sig")
    r = _row(d.get_ai_jobs_status(now)["rows"], "개선 AI 학습")
    assert r["status"] == "stale" and "59전략" in r["detail"], "8일 넘으면 오래됨"
    sw = base / "db" / "setting_sweep.json"
    sw.write_text(json.dumps({"generated": "2026-11-15 02:14", "fine": True, "sec": 868,
                              "engines": {"a": {"families": {"v2": {"status": "권장"}}}, "b": {"error": "x"}}}), encoding="utf-8")
    os.utime(sw, (now.timestamp() - 5 * 86400, now.timestamp() - 5 * 86400))
    r = _row(d.get_ai_jobs_status(now)["rows"], "촘촘")
    assert r["status"] == "ok" and "권장 1건" in r["detail"] and "실패 1전략" in r["detail"] and r["took"] == "14분"
    lg = fl / "setting_sweep_20261120_0200.log"
    lg.write_text("[x] setting sweep start\n", encoding="utf-8")
    os.utime(lg, (now.timestamp() - 1800, now.timestamp() - 1800))
    assert _row(d.get_ai_jobs_status(now)["rows"], "촘촘")["status"] == "run"
    lg.write_text("[x] setting sweep start\n[x] setting sweep end rc=1\n", encoding="utf-8")
    assert _row(d.get_ai_jobs_status(now)["rows"], "촘촘")["status"] == "fail"


def test_learn_after_backtest_shows_last_real_learning(env):
    d, base, fl = env
    pb = base / "db" / "period_bt"
    (pb / "pbt_20261030_220000.json").write_text(json.dumps({"finished": "2026-10-30 22:55:31",
                                                           "ai_learn": {"started": True, "n_new": 47}}), encoding="utf-8")
    os.utime(pb / "pbt_20261030_220000.json", (1, 1_000_000))
    (pb / "pbt_20261030_230000.json").write_text(json.dumps({"finished": "2026-10-30 23:46:27",
                                                           "ai_learn": {"started": False, "why": "모두 이미 학습됨"}}), encoding="utf-8")
    (base / "logs" / "run_ai_learn.log").write_text(
        'x\n{"ok": true, "engines": 59, "learned": ["a", "b"], "validation": {"ai": 1.07, "v2": 0.6}}\n', encoding="utf-8")
    r = _row(d.get_ai_jobs_status()["rows"], "백테스트 뒤")
    assert r["status"] == "ok" and "모두 이미 학습됨" in r["detail"] and "새 전략 2개" in r["detail"]


def test_wired_into_api_all_and_tab():
    src = open(Path(__file__).resolve().parents[1] / "integrated_dashboard_server.py", encoding="utf-8").read()
    assert '"ai_jobs":    _safe(get_ai_jobs_status)' in src and "_r(fillAIJobs, d.ai_jobs);" in src
    assert src.index('id="ai-jobs-rows"') > src.index('id="tab-datalogs"')
