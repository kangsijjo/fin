# -*- coding: utf-8 -*-
"""전략 사용처 표시(§79) — 모의계좌(키움·KIS)·AI·강도 가상매매, 백테스트 ★ 는 '지금' 설정 기준."""
import os
from datetime import datetime

import live_config as LC
import virtual_config as VC


def test_usage_lists_every_place_and_live_map_follows_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    monkeypatch.setenv("VIRTUAL_CONFIG_PATH", str(tmp_path / "vc.json"))
    cfg = LC.default_config()
    cfg["accounts"]["kis"]["strategies"][2] = {"engine": "star_high_500d_20", "slots": 2, "exempt": True,
                                               "stop": None, "take": None, "holding": 20}   # 두 계좌 모두
    LC.save(cfg)
    rows = [{"engine": "bo_60d_mkt", "slots": 10, "exempt": False, "stop": None, "take": None, "holding": 20}]
    VC.save({p: {"min_score": None, "strength_version": "v1", "strategies": rows} for p in VC.PORTFOLIOS},
            now=datetime(2026, 10, 27, 10, 0))
    u = VC.strategy_usage("20261027")
    assert [x["place"] for x in u["star_high_500d_20"]] == ["kiwoom", "kis"]
    assert [x["place"] for x in u["bo_60d_mkt"]] == ["ai", "strength"] and u["bo_60d_mkt"][0]["kind"] == "virtual"
    assert VC.usage_text(u["star_high_500d_20"]) == "키움 6칸 · KIS 2칸"
    import strategy_engine as E
    lm = E.live_map()
    assert lm["star_high_500d_20"] == "키움 high_500d_filt · KIS high_500d_filt"
    assert "gc_for3d" not in lm, "설정에서 뺀 전략은 ★ 없음(종전 고정 표는 남아 있었음)"


def test_dashboard_marks_usage_everywhere():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert "E.live_map()" in src and "VC.strategy_usage()" in src
    assert "function _pbtBadge" in src and "🧪" in src and "두 계좌에서 모두 운용" in src
    import period_backtest as P
    src_pb = open(os.path.join(here, "period_backtest.py"), encoding="utf-8").read()
    assert "E.live_map()" in src_pb
