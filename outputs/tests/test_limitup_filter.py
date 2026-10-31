# -*- coding: utf-8 -*-
"""§91 상한가 마감 신호 제외(skip_limitup) — 거르개(signal_filters) · 설정(live/virtual_config) · 트레이더 cmd_buy ·
가상매매 · 강도 컷 모니터 · 기간 백테스트 옵션 · 추천(lu 조합) · 설정 탐색 현재값 · 대시보드 연결."""
import copy
import json
import os
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

import signal_filters as SF

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
ACTION = {"X-Dashboard-Action": "1"}


def _daily(base, d, rows, col="change_pct"):
    p = os.path.join(base, "macro_data", "daily")
    os.makedirs(p, exist_ok=True)
    pd.DataFrame([{"code": c, col: v} for c, v in rows]).to_csv(os.path.join(p, f"{d}.csv"), index=False, encoding="utf-8-sig")


# ── 거르개 ──
def test_limitup_codes_reads_daily_file(tmp_path):
    _daily(str(tmp_path), "20261030", [("000001", 29.97), ("000002", 29.4), ("3", 30.0)])
    assert SF.limitup_codes("2026-10-30", base=str(tmp_path)) == {"000001", "000003"}, "≥29.5 · 코드 6자리"
    assert SF.limitup_codes("20261029", base=str(tmp_path)) is None, "파일 없으면 None(판정 불가)"
    _daily(str(tmp_path), "20261028", [("000009", 30.0)], col="등락률")
    assert SF.limitup_codes("20261028", base=str(tmp_path)) == {"000009"}


def test_drop_limitup_only_configured_strategies_and_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(SF, "HERE", str(tmp_path))
    _daily(str(tmp_path), "20261030", [("000001", 30.0), ("000002", 1.0)])
    logs = []
    sigs = [{"code": "000001", "strategy": "a", "signal_date": "20261030"},     # 켠 전략 · 상한가 → 뺌
            {"code": "000002", "strategy": "a", "signal_date": "20261030"},     # 켠 전략 · 보통 → 남김
            {"code": "000001", "strategy": "b", "signal_date": "20261030"},     # 안 켠 전략 → 남김
            {"code": "000001", "strategy": "a", "signal_date": "20261027"}]     # 그날 일봉 없음 → 남김(fail-open)
    kept, dropped = SF.drop_limitup(sigs, {"a"}, log=logs.append, label="t")
    assert dropped == [sigs[0]] and kept == sigs[1:]
    assert any("상한가 마감 신호 제외 1건" in x for x in logs) and any("20261027" in x and "건너뜀" in x for x in logs)
    assert SF.drop_limitup(sigs, set()) == (sigs, []), "켠 전략이 없으면 그대로"


def test_limitup_entry_keys_mark_next_trading_day_of_each_code():
    df = pd.DataFrame({"code": ["1"] * 3 + ["2"] * 3, "date": ["20240102", "20240103", "20240105"] * 2,
                       "change_pct": [30.0, 1.0, 2.0, 1.0, 29.6, 3.0]})
    keys = SF.limitup_entry_keys(df)
    assert keys == {("000001", "20240103"), ("000002", "20240105")}, "진입일 = 그 종목의 다음 거래일"
    assert SF.limitup_flags(df, ["1", "2", "2"], ["20240103", "20240103", "20240105"], keys=keys) == [True, False, True]


# ── 설정 ──
def test_live_config_skip_limitup_roundtrip(tmp_path, monkeypatch):
    import live_config as LC
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    cfg = LC.default_config()
    bad = copy.deepcopy(cfg)
    bad["accounts"]["kiwoom"]["strategies"][0]["skip_limitup"] = "yes"
    assert any("상한가 마감 신호 제외" in e for e in LC.validate(bad)), "참/거짓만"
    s0 = cfg["accounts"]["kiwoom"]["strategies"][0]
    s0["skip_limitup"] = True
    saved, ch = LC.save(cfg)
    assert saved["accounts"]["kiwoom"]["strategies"][0]["skip_limitup"] is True
    assert saved["accounts"]["kiwoom"]["strategies"][1]["skip_limitup"] is False, "없으면 끔으로 저장"
    assert any("상한가 제외 아니오→예" in c for c in ch), ch
    assert LC.account("kiwoom")["skip_limitup"] == {LC.live_name(s0["engine"])}
    assert LC.account("kis")["skip_limitup"] == set()


def test_virtual_config_skip_limitup_clean_validate_diff():
    import virtual_config as VC
    ports = VC.default_portfolios()
    new = copy.deepcopy(ports)
    new["strength"]["strategies"][0]["skip_limitup"] = True
    c = VC.clean(new)
    assert c["strength"]["strategies"][0]["skip_limitup"] is True and c["ai"]["strategies"][0]["skip_limitup"] is False
    bad = copy.deepcopy(new)
    bad["ai"]["strategies"][0]["skip_limitup"] = 1
    assert any("상한가 마감 신호 제외" in e for e in VC.validate(bad))
    ch = VC.diff(VC.clean(ports), c)
    assert any("상한가 제외 아니오→예" in x for x in ch), ch


# ── 트레이더(모의) — 신호 읽은 직후 거르고, 다 빠지면 주문 단계로 가지 않음 ──
@pytest.mark.parametrize("modname,nxt", [("kiwoom_trader", "get_api"), ("kis_trader", "KISMockClient")])
def test_trader_cmd_buy_drops_limitup_signals(monkeypatch, modname, nxt):
    import importlib
    m = importlib.import_module(modname)
    if hasattr(m, "guard_mock_only"):
        monkeypatch.setattr(m, "guard_mock_only", lambda *a, **k: None)
    monkeypatch.setattr(m.kill_switch, "guard_buy", lambda *a, **k: None)
    monkeypatch.setattr(SF, "limitup_codes", lambda d, base=None: {"000001"})
    monkeypatch.setattr(m, "SKIP_LIMITUP", {"s_on"})
    reached = []

    def stop(*a, **k):
        reached.append(1)
        raise RuntimeError("주문 단계 도달")
    monkeypatch.setattr(m, nxt, stop)
    monkeypatch.setattr(m, "todays_signals", lambda: [{"code": "000001", "strategy": "s_on", "signal_date": "20261030"}])
    m.cmd_buy()
    assert not reached, "남은 신호가 없으면 주문 단계로 가지 않음"
    monkeypatch.setattr(m, "todays_signals", lambda: [{"code": "000001", "strategy": "s_on", "signal_date": "20261030"},
                                                      {"code": "000001", "strategy": "s_off", "signal_date": "20261030"}])
    with pytest.raises(RuntimeError):
        m.cmd_buy()
    assert reached, "안 켠 전략 신호는 그대로 진행"


# ── 가상매매(AI·강도 포트) ──
def test_virtual_trader_skips_limitup_only_for_enabled_strategy():
    import ai_paper_trader as ap
    import virtual_config as VC
    rows = [{"engine": "gc_for3d", "slots": 5, "exempt": False, "stop": None, "take": None, "holding": 2, "min_score": None,
             "skip_limitup": True},
            {"engine": "bo_60d_mkt", "slots": 5, "exempt": False, "stop": None, "take": None, "holding": 2, "min_score": None}]
    hist = [{"effective": "20260105", "portfolios": {p: {"min_score": None, "strength_version": "v1",
                                                         "strategies": copy.deepcopy(rows)} for p in VC.PORTFOLIOS}}]
    cal = ["20260105", "20260106", "20260107", "20260108"]
    base = dict(signal_date="20260105", name="x", holding_days=2, entry_price_close=100.0, ai_prob=0.5, score_v2=None, score_ic=5.0)
    sig = pd.DataFrame([dict(base, code="000001", strategy="gc_for3d", limitup=True),      # 켠 전략 · 상한가 → 안 삼
                        dict(base, code="000002", strategy="gc_for3d", limitup=False),
                        dict(base, code="000003", strategy="bo_60d_mkt", limitup=True)])   # 안 켠 전략 → 삼
    closes = {d: {c: 100.0 for c in ("000001", "000002", "000003")} for d in cal}
    r = ap.simulate(sig, cal, cal, closes, "score_ic", history=hist, port="strength")
    assert {t["code"] for t in r["trades"]} == {"000002", "000003"}


def test_cut_monitor_skip_lu(monkeypatch):
    import strength_cut_monitor as CM
    monkeypatch.setattr(CM, "SKIP_LU", {"kiwoom": {"high_52w_filt"}, "kis": set()})
    assert CM._skip_lu("kiwoom", "high_52w_filt", NS(limitup=True))
    assert not CM._skip_lu("kiwoom", "high_52w_filt", NS(limitup=False))
    assert not CM._skip_lu("kis", "high_52w_filt", NS(limitup=True))
    assert not CM._skip_lu("kiwoom", "high_52w_filt", NS()), "기록 없으면 빼지 않음"


# ── 기간 백테스트 옵션 ──
def test_run_job_skip_limitup_filters_every_row(monkeypatch, tmp_path):
    import period_backtest as P
    cal = [d.strftime("%Y%m%d") for d in pd.bdate_range("20240101", periods=140)]
    rows = [{"code": c, "date": d, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "trading_value": 5e9,
             "volume": 1e5, "change_pct": 30.0 if (c == "B" and d == cal[99]) else 0.1}
            for d in cal for c in ("A", "B", "C")]
    df = pd.DataFrame(rows)
    trs = [NS(entry_date=cal[100], exit_date=cal[110], code=c, entry_price=100.0, net_pct=n, gross_pct=n, score=1)
           for c, n in (("A", 5.0), ("B", -3.0), ("C", 1.0))]

    class S:
        name = "fake"
        holding_days = 10

        def backtest(self, d, costs):
            return list(trs)

    monkeypatch.setattr(P, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(P, "trading_calendar", lambda: cal)
    monkeypatch.setattr(P, "strategies_by_name", lambda: ({"fake": S()}, {}))
    monkeypatch.setattr(P, "aux_coverage", lambda c, d=None, force=False: {})
    monkeypatch.setattr(P, "_notify", lambda t: None)
    import strategies.daily_loader as DL
    monkeypatch.setattr(DL, "load_macro_daily", lambda *a, **k: df.copy())
    for job, lu, n in (("pbt_20991231_000002", False, 3), ("pbt_20991231_000003", True, 2)):
        P.run_job(job, cal[90], cal[-1], ["fake"], False, skip_limitup=lu)
        st = json.load(open(P._job_path(job), encoding="utf-8"))
        assert st["params"]["skip_limitup"] is lu and st["results"][0]["n_trades"] == n
    assert any("상한가 마감 신호 제외" in w["msg"] and "1건" in w["msg"] for w in st["warnings"])


@pytest.fixture(scope="module")
def dash():
    import integrated_dashboard_server as d
    d.app.config["TESTING"] = True
    return d


def test_pbt_api_passes_skip_limitup_and_jobs_list(dash, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(dash._sp, "Popen", lambda cmd, **kw: calls.append(cmd) or NS(poll=lambda: None, pid=1))
    monkeypatch.setattr(dash, "_PBT_PROC", {"proc": None, "job": None})
    body = {"start": "20240101", "end": "20241231", "names": ["rsi_reversal"]}
    with dash.app.test_client() as c:
        assert c.post("/api/pbt/run", json=body, environ_base=LOCAL, headers=ACTION).get_json()["ok"]
        monkeypatch.setattr(dash, "_PBT_PROC", {"proc": None, "job": None})
        assert c.post("/api/pbt/run", json={**body, "skip_limitup": True}, environ_base=LOCAL, headers=ACTION).get_json()["ok"]
    assert "--skip-limitup" not in calls[0] and "--skip-limitup" in calls[1]
    monkeypatch.setattr(dash, "_PBT_DIR", tmp_path)
    (tmp_path / "pbt_20261031_090000.json").write_text(json.dumps(
        {"status": "done", "params": {"start": "20240101", "end": "20241231", "names": ["a"], "skip_limitup": True},
         "results": [{"strategy": "a", "cagr": 1.0}]}), encoding="utf-8")
    with dash.app.test_client() as c:
        js = c.get("/api/pbt/jobs", environ_base=LOCAL).get_json()["jobs"]
    assert js[0]["skip_limitup"] is True


# ── 추천: '상한가 제외' 실험은 별도 조합 ──
def test_reco_counts_limitup_jobs_as_own_variant(tmp_path, monkeypatch):
    import strategy_reco as SR
    pbt = tmp_path / "pbt"
    pbt.mkdir()
    monkeypatch.setenv("PBT_DIR", str(pbt))
    monkeypatch.setattr(SR, "HERE", str(tmp_path))
    yy = {"2022": 3, "2023": 5}
    for i, (lu, cg) in enumerate(((False, 2.0), (True, 9.0), (True, 8.0))):
        rows = [{"strategy": e, "cagr": (cg if e == "e" else k), "mdd": -20, "sharpe": 0.5, "yearly": yy}
                for k, e in enumerate(["e", "x1", "x2", "x3", "x4", "x5", "x6", "x7"])]
        json.dump({"status": "done", "started": "2026-10-30 10:00:00", "finished": "2026-10-30 10:00:00",
                   "params": {"start": "20220101", "end": "20251231", "stop": None, "take": None, "skip_limitup": lu},
                   "results": rows}, open(pbt / f"pbt_2026103{i}_000000.json", "w", encoding="utf-8"))
    obs, _ = SR.load_observations(now=pd.Timestamp("2026-10-31").to_pydatetime())
    keys = {o["variant"] for o in obs if o["engine"] == "e"}
    base = json.dumps({"sv": None, "th": None, "stop": None, "take": None}, sort_keys=True)
    assert base in keys, "끈 실험의 열쇠는 예전 그대로"
    assert any(json.loads(k).get("lu") is True for k in keys)
    assert SR.variant_text({"th": None, "stop": -10, "lu": True}) == "손절 -10% · 상한가 마감 신호 제외"


# ── 설정 탐색: 지금 설정의 lu · 대시보드 권장 요약 ──
def test_sweep_current_settings_and_advice_carry_lu(tmp_path, monkeypatch, dash):
    import live_config as LC
    import setting_sweep as SS
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    cfg = LC.default_config()
    e0 = cfg["accounts"]["kis"]["strategies"][0]["engine"]
    cfg["accounts"]["kis"]["strategies"][0]["skip_limitup"] = True
    LC.save(cfg)
    cur = SS.current_settings()
    assert any(r["place"] == "kis" and r["lu"] is True for r in cur[e0])
    sw = {"engines": {"e": {"families": {"v2": {"status": "권장", "best": {"th": 5.0, "stop": None, "take": None, "lu": True}}},
                            "current": [{"place": "kis", "family": "v2", "th": 5.7, "stop": None, "take": None, "stats": None}]}}}
    a = dash._sweep_advice(sw, "e", "kis", "v2")
    assert a["current"]["lu"] is False and a["best"]["lu"] is True, "옛 탐색 결과(lu 없음)는 끔으로"
    sw["engines"]["e"]["current"][0]["lu"] = True
    assert dash._sweep_advice(sw, "e", "kis", "v2")["current"]["lu"] is True


def test_dashboard_wiring_for_limitup():
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    for s in ('id="pbt-lu"', "skip_limitup:!!(document.getElementById('pbt-lu')", "lu.checked=!!j.skip_limitup",
              "lcSet('${a}',${i},'skip_limitup',this.checked)", "vcSet('${p}',${i},'skip_limitup',this.checked)",
              "else if(k==='skip_limitup') s.skip_limitup=!!v;", "if(P.lu!=null) S.skip_limitup=!!P.lu;",
              "min_strength:th,holding:e.holding,skip_limitup:!!P.lu", "min_score:th,holding:e.holding,skip_limitup:!!P.lu",
              "${b.lu?' · 상한가 마감 신호 제외':''}", 'ex += ["--skip-limitup"]'):
        assert s in src, s
    src_p = open(os.path.join(OUTPUTS, "period_backtest.py"), encoding="utf-8").read()
    assert '"--skip-limitup"' in src_p and "skip_limitup=False" in src_p
