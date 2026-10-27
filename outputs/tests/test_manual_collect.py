# -*- coding: utf-8 -*-
"""데이터+로그 탭 수동 수집 버튼(§77) — 실행 대상 존재·매매시간 차단·날짜 자리표시자·버튼/깜빡임·스케줄러 지표 잡."""
import os
from datetime import datetime

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(HERE)
DATA_TASKS = ["backfill", "supply_demand", "macro", "credit", "program", "indicators", "foreign_ratio", "news",
              "after_market", "recheck"]


@pytest.fixture(scope="module")
def dash():
    import integrated_dashboard_server as d
    return d


def _target_exists(cmd, cwd):
    """명령에서 실행 대상(.py/.ps1/.bat 파일 또는 -m 모듈)이 실제로 있는가."""
    for i, x in enumerate(cmd):
        if x == "-m":
            mod = cmd[i + 1].replace(".", os.sep)
            return os.path.exists(os.path.join(cwd, mod + ".py")) or os.path.isdir(os.path.join(cwd, mod))
        if x.endswith((".py", ".ps1", ".bat")) and i > 0:
            p = x if os.path.isabs(x) or ":" in x else os.path.join(cwd, x)
            p = p.replace("\\\\", os.sep).replace("C:" + os.sep + "fin", ROOT).replace("C:\\fin", ROOT)
            return os.path.exists(p) or os.path.exists(os.path.join(cwd, os.path.basename(x)))
    return False


def test_every_data_button_targets_an_existing_script(dash):
    for t in DATA_TASKS:
        cmd = dash._resolve_cmd(dash._RUN_TASKS[t])
        cwd = dash._RUN_CWD.get(t, str(dash.BASE))
        cwd = cwd.replace("C:\\fin", ROOT).replace("\\", os.sep)
        if not os.path.isdir(cwd):
            cwd = os.path.join(ROOT, "Stock_AI_Project") if "Stock_AI" in cwd else HERE
        cmd = [c.replace("C:\\fin", ROOT).replace("\\", os.sep) for c in cmd]
        assert _target_exists(cmd, cwd), (t, cmd, cwd)


def test_trade_window_blocks_broker_tasks_only(dash):
    mon_0905 = datetime(2026, 10, 26, 9, 5)
    assert dash._trade_window_block("credit", mon_0905), "매수 시간엔 키움 키 쓰는 수집 차단"
    assert dash._trade_window_block("program", datetime(2026, 10, 26, 15, 21))
    assert dash._trade_window_block("credit", datetime(2026, 10, 26, 10, 0)) == ""
    assert dash._trade_window_block("macro", mon_0905) == "", "증권사 키 안 쓰는 수집은 언제나"
    assert dash._trade_window_block("credit", datetime(2026, 10, 25, 9, 5)) == "", "휴일은 막지 않음"


def test_date_placeholders_resolved_at_click_time(dash):
    cmd = dash._resolve_cmd(dash._RUN_TASKS["credit"], now=datetime(2026, 10, 26))
    assert "2026-09-11" in cmd and not any("{" in c for c in cmd)
    cmd = dash._resolve_cmd(dash._RUN_TASKS["program"], now=datetime(2026, 10, 26))
    assert "2026-09-26" in cmd


def test_buttons_and_freshness_wired():
    src = open(os.path.join(HERE, "integrated_dashboard_server.py"), encoding="utf-8").read()
    for t, fresh in (("program", "program_trading"), ("indicators", "korea_indicators"),
                     ("foreign_ratio", "foreign_ratio"), ("news", "news")):
        assert f"runTask('{t}'" in src and f'data-fresh="{fresh}"' in src, t
    for tbl in ("korea_indicators", "program_trading", "foreign_ratio"):
        assert f'("{tbl}",' in src, tbl
    assert "pbtCollect('all')" in src


def test_scheduler_indicator_job_collects_today_prices_first():
    src = open(os.path.join(ROOT, "Stock_AI_Project", "scheduler.py"), encoding="utf-8").read()
    body = src[src.index("def daily_indicators_job"):src.index("def kiwoom_daily_job")]
    assert body.index("run_pipeline('collect')") < body.index("run_pipeline('indicators')")
