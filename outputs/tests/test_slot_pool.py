# -*- coding: utf-8 -*-
"""칸 없음(pool) 모드(§75) — 설정·트레이더 순서·가상매매·백분위."""
import copy

import pandas as pd

import live_config as LC
import virtual_config as VC


def test_live_config_pool_mode_skips_slot_sum_and_reports_change():
    cfg = LC.default_config()
    cfg["accounts"]["kis"]["slot_mode"] = "pool"
    cfg["accounts"]["kis"]["strategies"][0]["slots"] = 1          # 합 10 아님 — pool 이면 허용
    assert LC.validate(cfg) == []
    assert LC.account("kis", cfg)["slot_mode"] == "pool"
    assert LC.account("kiwoom", cfg)["slot_mode"] == "slots", "기본은 전략별 칸"
    assert any("칸 방식" in c for c in LC.diff(LC.default_config(), cfg))
    bad = copy.deepcopy(cfg)
    bad["accounts"]["kis"]["slot_mode"] = "x"
    assert any("칸 방식" in e for e in LC.validate(bad))
    slots_bad = copy.deepcopy(cfg)
    slots_bad["accounts"]["kis"]["slot_mode"] = "slots"
    assert any("슬롯 합" in e for e in LC.validate(slots_bad)), "전략별 칸이면 합 10 검사 유지"


def test_live_config_save_keeps_slot_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    cfg = LC.default_config()
    cfg["accounts"]["kiwoom"]["slot_mode"] = "pool"
    saved, _ = LC.save(cfg)
    assert saved["accounts"]["kiwoom"]["slot_mode"] == "pool" and LC.load()["accounts"]["kiwoom"]["slot_mode"] == "pool"


def test_trader_pool_order_uses_within_strategy_rank(monkeypatch):
    import kis_trader as kx
    import kiwoom_trader as kt
    import strength_model as SM
    # 전략 A 는 점수가 후하게(8) 나와도 그 전략 안에선 중간(50%), 전략 B 는 6 이 상위 90%
    fake = {("A", 8.0): 50.0, ("B", 6.0): 90.0, ("A", 9.5): 95.0}
    monkeypatch.setattr(SM, "rank_pct", lambda eng, ln, sc, ver="v1": None if sc is None else fake[(ln, sc)])
    sigs = [{"code": "000001", "strategy": "A"}, {"code": "000002", "strategy": "B"},
            {"code": "000003", "strategy": "A"}, {"code": "000004", "strategy": "B"}]
    strength = {("000001", "A"): 8.0, ("000002", "B"): 6.0, ("000003", "A"): 9.5}   # 000004 무기록 → 맨 뒤
    for m in (kx, kt):
        assert [s["code"] for s in m._pool_order(sigs, strength)] == ["000003", "000002", "000001", "000004"]


def test_traders_have_pool_branch():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for f in ("kis_trader.py", "kiwoom_trader.py"):
        src = open(os.path.join(here, f), encoding="utf-8").read()
        assert 'SLOT_MODE == "pool"' in src and "_pool_order(" in src and "MAX_CONCURRENT - len(" in src, f


def test_rank_pct_fallback_without_reference(monkeypatch):
    import strength_model as SM
    monkeypatch.setattr(SM, "_v2_ref", lambda e: None)
    monkeypatch.setattr(SM, "_v1_ref", lambda ln: None)
    assert SM.rank_pct("x", "x", 6.0, "v2") == 60.0 and SM.rank_pct("x", "x", None) is None


def test_virtual_pool_ignores_strategy_caps():
    import ai_paper_trader as ap
    rows = [{"engine": "gc_for3d", "slots": 1, "exempt": True, "stop": None, "take": None, "holding": 2},
            {"engine": "bo_60d_mkt", "slots": 1, "exempt": True, "stop": None, "take": None, "holding": 2}]
    hist = [{"effective": "20260105", "portfolios": {
        p: {"min_score": None, "strength_version": "v1", "slot_mode": "pool", "strategies": copy.deepcopy(rows)}
        for p in VC.PORTFOLIOS}}]
    assert VC.validate(hist[0]["portfolios"]) == [], "pool 이면 칸 합 10 아니어도 됨"
    cal = ["20260105", "20260106", "20260107", "20260108"]
    base = dict(signal_date="20260105", name="x", holding_days=2, entry_price_close=100.0, score_v2=None)
    sig = pd.DataFrame([dict(base, code=f"00000{i}", strategy="gc_for3d", score_ic=5.0 + i, ai_prob=0.5 + i / 100)
                        for i in range(1, 5)])
    closes = {d: {f"00000{i}": 100.0 for i in range(1, 5)} for d in cal}
    for port, col in (("ai", "ai_prob"), ("strength", "score_ic")):
        r = ap.simulate(sig, cal, cal, closes, col, min_score=0.0, history=hist, port=port)
        assert len(r["trades"]) == 4, (port, "gc_for3d 칸 1 이지만 pool 이라 4종목 모두")
    hist[0]["portfolios"]["ai"]["slot_mode"] = "slots"
    hist[0]["portfolios"]["ai"]["strategies"][0]["slots"] = 9
    r = ap.simulate(sig, cal, cal, closes, "ai_prob", history=hist, port="ai")
    assert len(r["trades"]) == 4
    hist[0]["portfolios"]["ai"]["strategies"][0]["slots"] = 2
    hist[0]["portfolios"]["ai"]["strategies"][1]["slots"] = 8
    r = ap.simulate(sig, cal, cal, closes, "ai_prob", history=hist, port="ai")
    assert len(r["trades"]) == 2, "전략별 칸이면 gc_for3d 2칸까지"
