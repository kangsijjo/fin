# -*- coding: utf-8 -*-
"""AI·강도 가상매매 설정(§68) — 적용일·이력·검증 + ai_paper_trader 설정 반영 시뮬."""
import json
import os
from datetime import datetime

import pandas as pd
import pytest

import virtual_config as VC


@pytest.fixture
def vpath(tmp_path, monkeypatch):
    p = tmp_path / "virtual_config.json"
    monkeypatch.setenv("VIRTUAL_CONFIG_PATH", str(p))
    return p


def _ports(ai_rows=None, st_rows=None, ai_min=None, st_min=5.0):
    r = ai_rows or [{"engine": "gc_for3d", "slots": 10, "exempt": False, "stop": None, "take": None, "holding": 15}]
    return {"ai": {"min_score": ai_min, "strength_version": "v1", "strategies": r},
            "strength": {"min_score": st_min, "strength_version": "v1", "strategies": st_rows or r}}


def test_effective_date_rules():
    assert VC.effective_date(datetime(2026, 10, 22, 10, 0)) == "20261022"   # 목 18:25 전 → 오늘 신호부터
    assert VC.effective_date(datetime(2026, 10, 22, 19, 0)) == "20261023"   # 18:25 뒤 → 다음 거래일
    assert VC.effective_date(datetime(2026, 10, 24, 10, 0)) == "20261026"   # 토 → 월


def test_validate():
    assert VC.validate(_ports()) == []
    bad = _ports(ai_min=1.5)
    assert any("0~1" in e for e in VC.validate(bad))
    rows = [{"engine": "gc_for3d", "slots": 9, "exempt": False, "stop": None, "take": None, "holding": 15}]
    assert any("슬롯 합" in e for e in VC.validate(_ports(ai_rows=rows)))
    assert any("알 수 없는" in e for e in VC.validate(_ports(), engine_names={"x"}))


def test_default_portfolios_sum_to_ten():
    d = VC.default_portfolios()
    for p in VC.PORTFOLIOS:
        assert sum(s["slots"] for s in d[p]["strategies"]) == VC.SLOTS_TOTAL
        assert 1 <= len(d[p]["strategies"]) <= VC.MAX_STRATEGIES
    assert VC.validate(d) == []


def test_first_save_freezes_legacy_and_same_day_replaces(vpath):
    cfg, ch, eff = VC.save(_ports(), now=datetime(2026, 10, 22, 19, 0))
    assert eff == "20261023"
    assert cfg["history"][0]["legacy"] and cfg["history"][0]["effective"] == VC.LEGACY_DATE
    assert "legacy_min_strength" in cfg["history"][0]
    assert any("종전 규칙" in c for c in ch)
    cfg2, ch2, eff2 = VC.save(_ports(st_min=6.0), now=datetime(2026, 10, 22, 20, 0))
    assert eff2 == eff and len(cfg2["history"]) == 2, "같은 적용일 예약은 교체"
    assert any("기준 5 → 6" in c for c in ch2)
    act, pend = VC.state("20261022")
    assert act["legacy"] and pend["effective"] == "20261023"
    act, pend = VC.state("20261023")
    assert not act.get("legacy") and pend is None
    assert VC.needed_engines("20261023") == ["gc_for3d"]


def _sig(rows):
    return pd.DataFrame(rows)


def test_simulate_uses_config_from_effective_date():
    import ai_paper_trader as ap
    hist = [{"effective": VC.LEGACY_DATE, "legacy": True, "legacy_min_strength": 5.0, "legacy_exempt": []},
            {"effective": "20260107", "portfolios": {
                "ai": {"min_score": 0.3, "strength_version": "v1", "strategies": [
                    {"engine": "gc_for3d", "slots": 1, "exempt": False, "stop": -5, "take": 8, "holding": 3},
                    {"engine": "bo_60d_mkt", "slots": 9, "exempt": True, "stop": None, "take": None, "holding": 3}]},
                "strength": {"min_score": 6.0, "strength_version": "v1", "strategies": [
                    {"engine": "gc_for3d", "slots": 10, "exempt": False, "stop": None, "take": None, "holding": 3}]}}}]
    cal = [f"202601{d:02d}" for d in range(5, 20)]
    base = dict(name="x", holding_days=3, entry_price_close=100.0, score_ic=7.0, score_v2=None)
    sig = _sig([
        # 종전 규칙 구간(1/5): 아무 전략이나 담김
        dict(base, signal_date="20260105", code="000001", strategy="rsi_reversal", ai_prob=0.9),
        # 설정 구간(1/7~): gc_for3d 1칸 상한 → 두 번째는 못 삼 / 기준 0.3 미만 제외 / 면제 bo_60d_mkt 는 확률 낮아도 삼
        dict(base, signal_date="20260107", code="000002", strategy="gc_for3d", ai_prob=0.8),
        dict(base, signal_date="20260107", code="000003", strategy="gc_for3d", ai_prob=0.7),
        dict(base, signal_date="20260107", code="000004", strategy="rsi_reversal", ai_prob=0.95),
        dict(base, signal_date="20260107", code="000005", strategy="bo_60d_mkt", ai_prob=0.1),
        dict(base, signal_date="20260107", code="000006", strategy="gc_for3d", ai_prob=0.2)])
    closes = {d: {c: 100.0 for c in ("000001", "000002", "000003", "000004", "000005", "000006")} for d in cal}
    closes["20260108"]["000002"] = 109.0          # 익절 +8% → 1/8 종가 청산
    r = ap.simulate(sig, cal, cal, closes, "ai_prob", history=hist, port="ai")
    got = {t["code"]: t for t in r["trades"]}
    assert set(got) == {"000001", "000002", "000005"}, got
    assert got["000002"]["reason"] == "익절" and got["000002"]["exit_date"] == "20260108"
    assert got["000005"]["reason"] == "만기"
    # 강도 포트: 종전 구간은 고정된 legacy 기준 5.0(점수 7 통과), 설정 구간은 gc_for3d 만
    r2 = ap.simulate(sig, cal, cal, closes, "score_ic", min_score=9.9, history=hist, port="strength")
    assert {t["code"] for t in r2["trades"]} == {"000001", "000002", "000003", "000006"}


def test_simulate_without_history_is_unchanged():
    import ai_paper_trader as ap
    sig = _sig([{"signal_date": "20260105", "code": "000001", "name": "a", "strategy": "gc_for3d",
                 "holding_days": 2, "entry_price_close": 100.0, "score_ic": 3.0, "ai_prob": 0.1, "score_v2": None}])
    cal = ["20260105", "20260106", "20260107", "20260108"]
    closes = {d: {"000001": 110.0} for d in cal}
    assert [t["code"] for t in ap.simulate(sig, cal, cal, closes, "ai_prob")["trades"]] == ["000001"]


def test_virtual_signal_targets_skip_live_strategies(vpath):
    import live_config as LC
    import virtual_signal as VS
    live_eng = LC.load()["accounts"]["kis"]["strategies"][0]["engine"]
    rows = [{"engine": live_eng, "slots": 5, "exempt": False, "stop": None, "take": None, "holding": 20},
            {"engine": "bo_60d_mkt", "slots": 5, "exempt": False, "stop": None, "take": None, "holding": 20}]
    VC.save(_ports(ai_rows=rows), now=datetime(2026, 10, 22, 10, 0))
    assert [t[0] for t in VS.targets("20261022")] == ["bo_60d_mkt"]


def test_bat_runs_virtual_signal_before_paper_trader():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    t = open(os.path.join(here, "run_kis_signal.bat"), "rb").read().decode("ascii")
    assert t.index("virtual_signal.py") < t.index("ai_paper_trader.py")


def test_legacy_rules_follow_live_config_backups(tmp_path, monkeypatch):
    """종전 규칙 구간의 강도 기준은 '그날 모의계좌가 쓰던 값' — 키움 기준을 바꿔도 과거가 안 바뀐다."""
    import live_config as LC
    lp = tmp_path / "live_config.json"
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(lp))
    LC.save(LC.default_config())                                       # 5.7 (백업 없음: 첫 저장)
    new = LC.default_config()
    new["accounts"]["kiwoom"]["min_strength"] = 5.0
    import time
    time.sleep(1.1)
    LC.save(new)                                                       # 이 순간 5.7 백업이 남음
    tl = VC.live_rules_timeline()
    assert tl[-1][0] is None and tl[-1][1] == 5.0
    assert VC.legacy_rules(tl, "20200101")[0] == 5.7, "과거 신호일은 그때 값"
    assert VC.legacy_rules(tl, "29991231")[0] == 5.0
