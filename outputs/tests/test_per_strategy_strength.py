# -*- coding: utf-8 -*-
"""전략별 강도 기준(§71) — 모의계좌(live_config·트레이더·모니터) + 가상매매(virtual_config·ai_paper_trader)."""
import copy

import pandas as pd

import live_config as LC
import virtual_config as VC


def test_live_config_per_strategy_threshold_and_fallback():
    cfg = LC.default_config()
    cfg["accounts"]["kis"]["strategies"][0]["min_strength"] = 4.5
    assert LC.validate(cfg) == []
    A = LC.account("kis", cfg)
    first = LC.live_name(cfg["accounts"]["kis"]["strategies"][0]["engine"])
    second = LC.live_name(cfg["accounts"]["kis"]["strategies"][1]["engine"])
    assert A["min_strength_by"][first] == 4.5
    assert A["min_strength_by"][second] == A["min_strength"], "비우면 계좌 기본값"
    bad = copy.deepcopy(cfg)
    bad["accounts"]["kis"]["strategies"][0]["min_strength"] = 11
    assert any("전략별 강도 기준" in e for e in LC.validate(bad))
    ch = LC.diff(LC.default_config(), cfg)
    assert any("강도기준 없음→4.5" in c for c in ch), ch


def test_live_config_save_keeps_per_strategy_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    cfg = LC.default_config()
    cfg["accounts"]["kiwoom"]["strategies"][1]["min_strength"] = 6.2
    saved, _ = LC.save(cfg)
    assert saved["accounts"]["kiwoom"]["strategies"][1]["min_strength"] == 6.2
    assert LC.load()["accounts"]["kiwoom"]["strategies"][0].get("min_strength") is None


def test_trader_gate_uses_per_strategy_threshold(monkeypatch):
    import kis_trader as kx
    import kiwoom_trader as kt
    for m in (kx, kt):
        monkeypatch.setattr(m, "STRENGTH_TH", {"x_strat": 4.0})
        monkeypatch.setattr(m, "STRENGTH_EXEMPT", set())
        monkeypatch.setattr(m, "MIN_STRENGTH_SCORE", 6.0)
        assert m._strength_gate("x_strat", 4.5)[0] is True, "전략별 4.0 적용"
        assert m._strength_gate("y_strat", 4.5)[0] is False, "그 외는 계좌 기본 6.0"
        assert m._strength_gate("y_strat", 6.1)[0] is True


def test_virtual_per_strategy_threshold():
    import ai_paper_trader as ap
    hist = [{"effective": "20260105", "portfolios": {
        p: {"min_score": 6.0 if p == "strength" else None, "strength_version": "v1", "strategies": [
            {"engine": "gc_for3d", "slots": 5, "exempt": False, "stop": None, "take": None, "holding": 2,
             "min_score": 3.0 if p == "strength" else 0.3},
            {"engine": "bo_60d_mkt", "slots": 5, "exempt": False, "stop": None, "take": None, "holding": 2, "min_score": None}]}
        for p in VC.PORTFOLIOS}}]
    assert VC.validate(hist[0]["portfolios"]) == []
    cal = ["20260105", "20260106", "20260107", "20260108"]
    base = dict(signal_date="20260105", name="x", holding_days=2, entry_price_close=100.0, ai_prob=0.5, score_v2=None)
    sig = pd.DataFrame([dict(base, code="000001", strategy="gc_for3d", score_ic=4.0),      # 전략별 3.0 → 통과
                        dict(base, code="000002", strategy="bo_60d_mkt", score_ic=4.0),    # 기본 6.0 → 탈락
                        dict(base, code="000003", strategy="bo_60d_mkt", score_ic=6.5)])   # 통과
    closes = {d: {c: 100.0 for c in ("000001", "000002", "000003")} for d in cal}
    r = ap.simulate(sig, cal, cal, closes, "score_ic", min_score=6.0, history=hist, port="strength")
    assert {t["code"] for t in r["trades"]} == {"000001", "000003"}


def test_legacy_by_follows_timeline():
    tl = [("20261022162213", 5.7, [], {"a": 5.7}), (None, 5.0, [], {"a": 4.0})]
    assert VC.legacy_by(tl, "20261021") == {"a": 5.7}
    assert VC.legacy_by(tl, "20261023") == {"a": 4.0}
    assert VC.legacy_rules(tl, "20261023") == (5.0, [])


def test_kis_trader_handles_strategy_without_stop(monkeypatch):
    """[2026-10-26 실사고] KIS 에 손절 없는 전략(stoch_os20_mkt)을 넣자 STRATEGY_STOP[strat] KeyError 로
    09:00 매수 루프와 15:21 잔고 출력이 죽었다(rc=1 ×3, 워치독 경보). 손절 없음은 정상 설정이다."""
    import os
    import kis_trader as kx
    monkeypatch.setattr(kx, "STRATEGY_STOP", {"a": -0.15})
    assert kx._stop_txt("a") == "stop -15%"
    assert kx._stop_txt("no_stop_strategy") == "stop 없음"
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for f in ("kis_trader.py", "kiwoom_trader.py"):
        src = open(os.path.join(here, f), encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""')) and "종전 STRATEGY_STOP" not in l)
        assert "STRATEGY_STOP[strat]" not in code and "STRATEGY_TAKE[strat]" not in code, f
