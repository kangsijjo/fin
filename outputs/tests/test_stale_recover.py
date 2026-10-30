# -*- coding: utf-8 -*-
"""유니버스 지연 자동 복구(§81) — 건너뜀 기록·복구 판정·작업 등록."""
import os
from datetime import datetime

import market_calendar as mc

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_mark_stale_skip_accumulates_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("STALE_SKIP_PATH", str(tmp_path / "s.json"))
    now = datetime(2026, 10, 30, 9, 0)
    mc.mark_stale_skip("키움 안C", "20261027", now=now)
    mc.mark_stale_skip("KIS 안D", "20261027", now=now)
    mc.mark_stale_skip("KIS 안D", "20261027", now=now)
    d = mc.load_stale_skip()
    assert d["date"] == "20261030" and d["accounts"] == ["키움 안C", "KIS 안D"]
    mc.mark_stale_skip("KIS 안D", "20261030", now=datetime(2026, 11, 2, 9, 0))
    assert mc.load_stale_skip()["accounts"] == ["KIS 안D"], "다른 날이면 새로"


def test_trader_gate_writes_skip_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("STALE_SKIP_PATH", str(tmp_path / "s.json"))
    import kis_trader as kx
    monkeypatch.setattr(mc, "check_signal_freshness", lambda d, l: (False, None))
    assert kx._signals_fresh("20261027", "KIS 안D") is False
    assert "KIS 안D" in mc.load_stale_skip()["accounts"]


def test_plan_only_acts_after_morning_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("STALE_SKIP_PATH", str(tmp_path / "s.json"))
    import recover_stale as R
    now = datetime(2026, 10, 30, 13, 0)
    monkeypatch.setattr(mc, "is_trading_day", lambda d=None, holidays=None: True)
    monkeypatch.setattr(R, "_gap", lambda: (0, []))
    assert R.plan(now)[0] == "skip", "기록 없으면 아무것도 안 함"
    mc.mark_stale_skip("KIS 안D", "20261027", now=datetime(2026, 10, 30, 9, 0))
    assert R.plan(now)[0] == "signals"
    monkeypatch.setattr(R, "_gap", lambda: (2, ["20261028", "20261029"]))
    assert R.plan(now)[0] == "collect"
    d = mc.load_stale_skip(); d["done"] = "13:50 복구 매수"; mc.save_stale_skip(d)
    assert R.plan(now)[0] == "skip", "같은 날 두 번 안 함"
    monkeypatch.setattr(mc, "is_trading_day", lambda d=None, holidays=None: False)
    assert R.plan(now)[0] == "skip"


def test_task_registered_and_bat_runs_script():
    ps = open(os.path.join(HERE, "register_tasks.ps1"), encoding="utf-8").read()
    assert "StockAI\\StaleRecover" in ps and "run_stale_recover.bat" in ps and '"13:00"' in ps
    bat = open(os.path.join(HERE, "run_stale_recover.bat"), "rb").read().decode("ascii")
    assert "recover_stale.py" in bat and "is_krx_holiday.ps1" in bat
    import recover_stale as R
    assert R.BUY_DEADLINE == "1500" and R.LAST_TRY == "1400"
