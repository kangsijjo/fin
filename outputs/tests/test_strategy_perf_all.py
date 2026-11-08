# -*- coding: utf-8 -*-
"""§98 — 천억이 탭 '전략별 성과(모든 매매)' + 추천 칸 '쓰는 곳'(본계좌·강도 방식 포함)."""
import os
from datetime import datetime

import pytest

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_usage_includes_real_account_and_strength_version(tmp_path, monkeypatch):
    import live_config as LC
    import real_account as RA
    import virtual_config as VC
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    monkeypatch.setenv("VIRTUAL_CONFIG_PATH", str(tmp_path / "vc.json"))
    LC.save(LC.default_config())
    rows = [{"engine": "star_high_500d_20", "slots": 10, "exempt": False, "stop": None, "take": None, "holding": 20}]
    VC.save({p: {"min_score": None, "strength_version": "v2", "strategies": rows} for p in VC.PORTFOLIOS},
            now=datetime(2026, 10, 27, 10, 0))
    monkeypatch.setattr(RA, "get_mode", lambda: "dryrun")
    u = VC.strategy_usage("20261027")["star_high_500d_20"]
    assert [x["place"] for x in u] == ["kiwoom", "kiwoom_real", "ai", "strength"]
    assert VC.usage_text(u) == "키움 6칸 · 본계좌(드라이런) 6칸 · AI 가상매매 10칸 · 강도 가상매매(v2) 10칸"
    assert u[1]["mirror"], "본계좌를 따로 저장 안 했으면 키움 모의 설정을 따른다"
    monkeypatch.setattr(RA, "get_mode", lambda: "live")
    assert VC.strategy_usage("20261027")["star_high_500d_20"][1]["label"] == "본계좌"


@pytest.fixture
def dash(tmp_path, monkeypatch):
    import integrated_dashboard_server as d
    import real_account as RA
    import virtual_config as VC
    monkeypatch.setattr(d, "REAL_DIR", tmp_path / "real")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "dryrun_20261105.csv").write_text(
        "time,side,code,name,strategy,qty,price,msg\n09:00:01,buy,000001,a,gc_for3d,1,100,x\n"
        "09:00:02,tp_sell,000001,a,gc_for3d,1,110,x\n", encoding="utf-8-sig")
    monkeypatch.setattr(RA, "get_mode", lambda: "dryrun")
    tr = lambda s, b, sp, bd, open_=False: {"strategy": s, "qty": 10, "buy_price": b, "sell_price": sp,   # noqa: E731
                                            "buy_date": bd, "sell_date": "20261110", "open": open_}
    kiwoom = {"trades": [tr("high_500d_filt", 1000, 1100, "20261001"), tr("high_500d_filt", 1000, 900, "20261102"),
                         tr("high_500d_filt", 1000, "", "20261103", True)],
              "positions": [{"strategy": "high_500d_filt", "entry_date": "20261103", "pnl_pct": -2.0}]}
    real = {"trades": [], "positions": [{"strategy": "gc_for3d", "entry_date": "20261105", "pnl_pct": 1.0, "bot": False}]}
    monkeypatch.setattr(d, "get_kiwoom_mock", lambda *a, **k: real if k.get("acct") == "kiwoom_real" else kiwoom)
    monkeypatch.setattr(d, "get_kis_mock", lambda *a, **k: {"trades": [], "positions": []})
    monkeypatch.setattr(d, "get_ai_paper", lambda: {"start": "20260630", "updated": "x", "portfolios": {
        "ai": {"trades": [{"strategy": "gc_for3d", "entry_date": "20261020", "net_pct": 5.0, "pnl": 50000}],
               "positions": [{"strategy": "gc_for3d", "entry_date": "20261104", "pnl_pct": 3.0}]},
        "strength": {"trades": [], "positions": []}}})
    monkeypatch.setattr(VC, "strategy_usage", lambda today=None: {"gc_for3d": [{"place": "ai", "label": "AI 가상매매", "slots": 1}]})
    return d


def test_perf_all_merges_places_net_of_costs_and_aliases(dash):
    r = dash.get_strategy_perf_all()
    rows = {x["engine"]: x for x in r["rows"]}
    k = rows["star_high_500d_20"]["cells"]["kiwoom"]            # 라이브 별칭 high_500d_filt → 엔진 이름
    assert k["n"] == 2 and k["win_pct"] == 50.0 and k["open_n"] == 1 and k["open_avg_pct"] == -2.0
    assert k["avg_pct"] < 0 and k["pnl"] < 0, "+10% / -10% 에 매매비용을 빼면 평균·합계가 음수"
    g = rows["gc_for3d"]
    assert g["cells"]["ai"] == {"n": 1, "win_pct": 100.0, "avg_pct": 5.0, "pnl": 50000, "open_n": 1, "open_avg_pct": 3.0}
    assert g["cells"]["kiwoom_real"].get("dry") == 1 and g["cells"]["kiwoom_real"]["open_n"] == 0, \
        "본계좌: 드라이런 매수 건수만, 사용자 보유(bot 아님)는 안 셈"
    assert g["uses"][0]["label"] == "AI 가상매매" and g["total"]["n"] == 1
    assert "드라이런" in {p["key"]: p["note"] for p in r["places"]}["kiwoom_real"]


def test_perf_all_period_filter_by_entry_date(dash):
    r = dash.get_strategy_perf_all("2026-11-01", "20261130")
    rows = {x["engine"]: x for x in r["rows"]}
    k = rows["star_high_500d_20"]["cells"]["kiwoom"]
    assert k["n"] == 1 and k["win_pct"] == 0.0 and k["open_n"] == 1, "10-01 진입 매매는 빠짐"
    assert "ai" not in rows["gc_for3d"]["cells"] or rows["gc_for3d"]["cells"]["ai"]["n"] == 0
    with dash.app.test_client() as c:
        j = c.get("/api/strategy_perf_all?from=20261101", environ_base=LOCAL).get_json()
    assert j["ok"] and j["from"] == "20261101" and [p["key"] for p in j["places"]] == \
        ["kiwoom", "kis", "kiwoom_real", "ai", "strength"]


def test_wiring_ui():
    src = open(os.path.join(HERE, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert 'id="ch-perf"' in src and "function chPerfLoad" in src and "'ch-perf'];" in src
    assert "function _useBadge" in src and "${_useBadge(r.used_elsewhere)}" in src and "${_useBadge(x.used_elsewhere)}" in src
    sr = open(os.path.join(HERE, "strategy_reco.py"), encoding="utf-8").read()
    assert '"slots": u.get("slots")' in sr
