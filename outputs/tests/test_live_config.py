# -*- coding: utf-8 -*-
"""
모의매매 전략 설정(live_config) + 엔진 신호(engine_signals) + 대시보드 설정 API + 백테스트 손절·익절 — 2026-10-21

고정하는 것
  · 설정 파일이 없으면 기본값 = 2026-10-18 현행 구성(트레이더 상수와 1:1)
  · 검증(슬롯 합 10·전략 1~4·손절/익절 범위·중복), 저장 시 백업·이력·옛 전략 청산 규칙 누적
  · 거래일 08:30~15:40 잠금, 18:25 이후/휴장일이면 신호 재생성
  · 트레이더가 설정을 읽는다(슬롯·우선순위·면제·손절·익절·강도) — KIS 익절은 종가 판정
  · 엔진 신호 = 백테스트 신호표의 마지막 날(가로채기), 보조데이터 없으면 건너뜀
  · 대시보드: GET/미리보기/저장(CSRF·로컬·confirm=2·잠금), 저장 후 재생성
  · 백테스트 손절·익절 재청산(종가 판정/장중 도달)
"""
import importlib
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
ACTION = {"X-Dashboard-Action": "1"}


@pytest.fixture
def cfgpath(tmp_path, monkeypatch):
    p = tmp_path / "live_config.json"
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(p))
    return p


def _cfg_with(kiwoom_rows, kis_rows=None, ms=5.7):
    import live_config as LC
    c = LC.default_config()
    c["accounts"]["kiwoom"] = {"min_strength": ms, "strategies": kiwoom_rows}
    if kis_rows is not None:
        c["accounts"]["kis"]["strategies"] = kis_rows
    return c


def test_default_equals_current_trader_constants(cfgpath):
    import live_config as LC
    assert not cfgpath.exists()
    kw, ki = LC.account("kiwoom"), LC.account("kis")
    assert kw["slots"] == {"high_500d_filt": 6, "high_52w_filt": 4} and kw["priority"][0] == "high_500d_filt"
    assert kw["exempt"] == {"high_500d_filt", "high_52w_filt"} and kw["take"] == {"rsi_vol": 0.2, "high_52w_filt": 0.5}
    assert ki["stop"] == {"h52w_for3d_mkt": -0.15, "for_high20_mkt": -0.10, "gc_for3d": -0.26}
    assert ki["exempt"] == {"gc_for3d"} and kw["min_strength"] == ki["min_strength"] == 5.7
    assert kw["holding"]["rsi_reversal"] == 5 and ki["holding"]["gc_for3d"] == 15


def test_validate_rules():
    import live_config as LC
    row = lambda e, n, **k: dict({"engine": e, "slots": n, "exempt": False, "stop": None, "take": None, "holding": 20}, **k)
    assert LC.validate(_cfg_with([row("rsi_reversal", 10)])) == []
    errs = " | ".join(LC.validate(_cfg_with([row("rsi_reversal", 6), row("rsi_reversal", 3)])))
    assert "두 번" in errs and "슬롯 합이 9" in errs
    errs = " | ".join(LC.validate(_cfg_with([row("a", 10, stop=-60, take=0.5)], ms=11)))
    assert "손절" in errs and "익절" in errs and "강도 기준" in errs
    assert "1~4개" in " ".join(LC.validate(_cfg_with([row(f"s{i}", 2) for i in range(5)])))
    assert "알 수 없는" in " ".join(LC.validate(_cfg_with([row("nope", 10)]), engine_names={"rsi_reversal"}))


def test_save_backup_history_and_exit_rules(cfgpath):
    import live_config as LC
    new = _cfg_with([{"engine": "gc_20_60", "slots": 10, "exempt": True, "stop": -8, "take": 25, "holding": 15}])
    cfg, ch = LC.save(new, note="t")
    assert cfgpath.exists() and any("gc_20_60" in c for c in ch) and any("star_high_500d_20" in c and "−" in c for c in ch)
    kw = LC.account("kiwoom")
    assert kw["slots"] == {"gc_20_60": 10} and kw["stop"]["gc_20_60"] == -0.08 and kw["take"]["gc_20_60"] == 0.25
    assert kw["take"]["high_52w_filt"] == 0.5, "빠진 전략의 보유분 익절 규칙은 남는다"
    assert kw["holding"]["high_500d_filt"] == 20
    LC.save(_cfg_with([{"engine": "rsi_reversal", "slots": 10, "exempt": False, "stop": None, "take": None, "holding": 5}]))
    backups = list((cfgpath.parent / "live_config_backups").glob("*.json"))
    assert backups, "이전 설정 백업"
    hist = (cfgpath.parent / "live_config_history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(hist) == 2
    assert LC.account("kiwoom")["stop"]["gc_20_60"] == -0.08, "두 번 바꿔도 옛 규칙 누적"


def test_broken_config_falls_back_to_default(cfgpath):
    import live_config as LC
    cfgpath.write_text("{", encoding="utf-8")
    c = LC.load()
    assert c.get("_fallback") and LC.account("kiwoom", c)["slots"] == {"high_500d_filt": 6, "high_52w_filt": 4}
    cfgpath.write_text(json.dumps({"accounts": {"kiwoom": {"min_strength": 5, "strategies": []}}}), encoding="utf-8")
    assert "검증 실패" in LC.load()["_fallback"]


def test_lock_and_regen_windows(monkeypatch):
    import live_config as LC
    import market_calendar as MC
    monkeypatch.setattr(MC, "is_trading_day", lambda d=None, holidays=None: True)
    assert LC.is_locked(datetime(2026, 10, 21, 9, 0))[0]
    assert LC.is_locked(datetime(2026, 10, 21, 15, 39))[0]
    assert not LC.is_locked(datetime(2026, 10, 21, 15, 40))[0]
    assert not LC.is_locked(datetime(2026, 10, 21, 8, 29))[0]
    assert not LC.needs_regen(datetime(2026, 10, 21, 16, 0)) and LC.needs_regen(datetime(2026, 10, 21, 19, 0))
    monkeypatch.setattr(MC, "is_trading_day", lambda d=None, holidays=None: False)
    assert not LC.is_locked(datetime(2026, 10, 24, 10, 0))[0] and LC.needs_regen(datetime(2026, 10, 24, 10, 0))


def test_traders_read_config(cfgpath):
    import live_config as LC
    LC.save(_cfg_with([{"engine": "gc_20_60", "slots": 7, "exempt": False, "stop": -9, "take": None, "holding": 15},
                       {"engine": "star_high_52w_20_filt", "slots": 3, "exempt": True, "stop": None, "take": 40, "holding": 20}],
                      [{"engine": "for_high20_mkt", "slots": 10, "exempt": False, "stop": -12, "take": 30, "holding": 20}],
                      ms=6.0))
    import kiwoom_trader as kt
    import kis_trader as kx
    try:
        kt = importlib.reload(kt)
        kx = importlib.reload(kx)
        assert kt.STRATEGY_MAX_SLOTS == {"gc_20_60": 7, "high_52w_filt": 3} and kt.STRATEGY_PRIORITY[0] == "gc_20_60"
        assert kt.MIN_STRENGTH_SCORE == 6.0 and kt.STRENGTH_EXEMPT == {"high_52w_filt"}
        assert kt.STRATEGY_STOP["gc_20_60"] == -0.09 and kt.PROFIT_TARGET["high_52w_filt"] == 0.40
        assert kt._strength_gate("gc_20_60", 5.9)[0] is False and kt._strength_gate("high_52w_filt", 1)[0] is True
        assert kx.STRATEGY_MAX_SLOTS == {"for_high20_mkt": 10} and kx.STRATEGY_TAKE["for_high20_mkt"] == 0.30
        assert kx.STRATEGY_STOP["h52w_for3d_mkt"] == -0.15, "빠진 KIS 전략 보유분 손절 유지"
    finally:
        os.environ["LIVE_CONFIG_PATH"] = str(cfgpath.parent / "none.json")
        importlib.reload(kt)
        importlib.reload(kx)


def test_kis_take_profit_is_close_based(monkeypatch):
    import kis_trader as kx
    monkeypatch.setattr(kx, "STRATEGY_TAKE", {"for_high20_mkt": 0.20})
    monkeypatch.setattr(kx, "load_kis_positions", lambda: {
        "000001": {"strategy": "for_high20_mkt", "entry_px": 100.0, "signal_date": "20260101", "holding_days": 20},
        "000002": {"strategy": "for_high20_mkt", "entry_px": 100.0, "signal_date": "20260101", "holding_days": 20},
        "000003": {"strategy": "for_high20_mkt", "entry_px": 100.0, "signal_date": "20260101", "holding_days": 20}})
    import strategies.daily_loader as DL
    monkeypatch.setattr(DL, "load_macro_daily", lambda *a, **k: pd.DataFrame({"code": ["000001"], "date": ["20260101"]}))
    monkeypatch.setattr(kx, "SIGNALS_CSV", "/nonexistent.csv")
    due = kx.codes_due_for_exit({"000001": 121.0, "000002": 115.0, "000003": 89.0}, reasons=("stop",))
    assert due == {"000001": "take", "000003": "stop"}


def test_kiwoom_strategy_stop_codes(monkeypatch):
    import kiwoom_trader as kt
    monkeypatch.setattr(kt, "STRATEGY_STOP", {"gc_20_60": -0.10})
    pos = {"000001": {"price": 89, "avg_price": 100}, "000002": {"price": 95, "avg_price": 100},
           "000003": {"price": 50, "avg_price": 100}}
    out = kt._strategy_stop_codes(pos, {"000001": "gc_20_60", "000002": "gc_20_60", "000003": "rsi_reversal"})
    assert out == {"000001": -11.0}


def test_kis_buy_skips_unknown_strategy_instead_of_remapping():
    import inspect
    import kis_trader as kx
    src = inspect.getsource(kx.cmd_buy)
    assert 'strat = "h52w_for3d_mkt"\n' not in src and "운용 중이 아닌 전략" in src


# ── 엔진 신호 ──
def _toy_df(n=80):
    dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("20250101", periods=n)]
    rows = []
    for c, slope in (("000010", 1.0), ("000020", -1.0)):
        for i, d in enumerate(dates):
            px = 1000 + slope * i * 5
            rows.append({"code": c, "date": d, "open": px, "high": px, "low": px * 0.99, "close": px,
                         "volume": 1e6, "trading_value": 5e9, "change_pct": 0.1, "foreign_net": 1.0, "inst_net": 1.0})
    return pd.DataFrame(rows), dates


def test_signal_frame_capture_matches_backtest_signals():
    import engine_signals as ES
    import strategy_engine as E
    from strategies.high_52w import FiftyTwoWeekHighStrategy
    df, dates = _toy_df()
    s = FiftyTwoWeekHighStrategy(lookback_days=20, holding_days=5, name="t")
    sdf, col = ES.signal_frame(s, df)
    trades = s.backtest(df.copy(), E.DEFAULT_COSTS)
    sig_days = set(sdf[sdf[col]]["date"][sdf[sdf[col]]["code"] == "000010"])
    assert {t.entry_date for t in trades} <= {dates[dates.index(d) + 1] for d in sig_days if dates.index(d) + 1 < len(dates)}
    import strategies._swing_base as SB
    import strategies.high_52w as H
    assert H._make_trades_for_signals is SB._make_trades_for_signals, "가로채기 후 원래 함수로 복구"


def test_today_signals_rows_and_aux_skip(monkeypatch):
    import engine_signals as ES
    from strategies.high_52w import FiftyTwoWeekHighStrategy
    import period_backtest as P
    df, dates = _toy_df()
    s = FiftyTwoWeekHighStrategy(lookback_days=20, holding_days=5, name="t")
    rows, why = ES.today_signals(s, df, dates[-1], dates, {"000010": "가나다"}, "t_live", 5,
                                 lambda nm, c: not nm, lambda ad, ld, n: "X")
    assert why == "" and [r["code"] for r in rows] == ["000010"]
    assert rows[0]["strategy"] == "t_live" and rows[0]["holding_days"] == 5 and rows[0]["target_exit_date"] == "X"
    monkeypatch.setattr(P, "aux_needs", lambda st: ["individual"])
    monkeypatch.setattr(ES, "_fill_individual_today", lambda d, ld: 0)
    rows, why = ES.today_signals(s, df, dates[-1], dates, {}, "t", 5, lambda nm, c: False, lambda *a: "")
    assert rows == [] and "개인 순매수" in why


def test_run_configured_uses_detector_or_engine(cfgpath, monkeypatch):
    import live_config as LC
    import engine_signals as ES
    LC.save(_cfg_with([{"engine": "star_high_52w_20_filt", "slots": 5, "exempt": True, "stop": None, "take": None, "holding": 20},
                       {"engine": "rsi_reversal", "slots": 5, "exempt": False, "stop": None, "take": None, "holding": 5}]))
    calls = []
    monkeypatch.setattr(ES, "today_signals", lambda s, *a, **k: (calls.append(s.name) or ([{"x": 1}], "")))
    out, skipped = ES.run_configured("kiwoom", pd.DataFrame(), "20260101", ["20260101"], {},
                                     {"high_52w_filt": lambda *a: [{"d": 1}]}, None, None, log=lambda *a: None)
    assert [n for n, _ in out] == ["high_52w_filt", "rsi_reversal"] and calls == ["rsi_reversal"] and not skipped


# ── 대시보드 API ──
@pytest.fixture(scope="module")
def dash():
    import integrated_dashboard_server as d
    d.app.config["TESTING"] = True
    return d


def _body(accounts=None, **k):
    import live_config as LC
    acc = accounts or {a: LC.default_config()["accounts"][a] for a in LC.ACCOUNTS}
    return dict({"accounts": acc}, **k)


def test_live_config_get_and_preview(dash, cfgpath, monkeypatch):
    import live_config as LC
    with dash.app.test_client() as c:
        d = c.get("/api/live_config", environ_base=LOCAL).get_json()
        assert d["ok"] and len(d["engines"]) >= 55 and d["slots_total"] == 10
        assert any(e["engine"] == "h52w_cred_dec" and "신용" in e["note"] for e in d["engines"])
        acc = json.loads(json.dumps(d["config"]["accounts"]))
        acc["kiwoom"]["strategies"][0]["slots"] = 5
        r = c.post("/api/live_config/preview", json={"accounts": acc}, environ_base=LOCAL, headers=ACTION).get_json()
        assert not r["ok"] and any("슬롯 합" in e for e in r["errors"])
        acc["kiwoom"]["strategies"][1]["slots"] = 5
        r = c.post("/api/live_config/preview", json={"accounts": acc}, environ_base=LOCAL, headers=ACTION).get_json()
        assert r["ok"] and any("slots 6→5" in x for x in r["changes"])
        assert c.post("/api/live_config/preview", json={"accounts": acc}, environ_base=LOCAL).status_code == 403


def test_live_config_save_guards(dash, cfgpath, monkeypatch):
    import live_config as LC
    spawned = []
    monkeypatch.setattr(dash._sp, "Popen", lambda cmd, **kw: spawned.append(cmd) or NS(poll=lambda: None, pid=1))
    monkeypatch.setattr(dash, "_LC_REGEN", {"proc": None})
    acc = {a: LC.default_config()["accounts"][a] for a in LC.ACCOUNTS}
    acc["kiwoom"] = {"min_strength": 6.0, "strategies": [
        {"engine": "gc_20_60", "slots": 10, "exempt": False, "stop": -8, "take": None, "holding": 999}]}
    with dash.app.test_client() as c:
        assert c.post("/api/live_config", json={"accounts": acc, "confirm": 2}, environ_base=LOCAL).status_code == 403
        assert c.post("/api/live_config", json={"accounts": acc, "confirm": 2},
                      environ_base={"REMOTE_ADDR": "10.0.0.5"}, headers=ACTION).status_code in (302, 403)
        assert c.post("/api/live_config", json={"accounts": acc}, environ_base=LOCAL, headers=ACTION).status_code == 400
        monkeypatch.setattr(LC, "is_locked", lambda now=None: (True, "장중"))
        assert c.post("/api/live_config", json={"accounts": acc, "confirm": 2}, environ_base=LOCAL,
                      headers=ACTION).status_code == 423
        monkeypatch.setattr(LC, "is_locked", lambda now=None: (False, ""))
        monkeypatch.setattr(LC, "needs_regen", lambda now=None: True)
        r = c.post("/api/live_config", json={"accounts": acc, "confirm": 2}, environ_base=LOCAL, headers=ACTION).get_json()
    assert r["ok"] and r["regen"] and spawned and spawned[0][-1].endswith("regen_signals.py")
    saved = json.loads(cfgpath.read_text(encoding="utf-8"))
    assert saved["accounts"]["kiwoom"]["strategies"][0]["holding"] == 15, "보유일은 화면 값이 아니라 엔진 값"
    assert dash._live_tables("kiwoom")[0] == {"gc_20_60": 10}
    assert "강도(score_ic) ≥ 6" in dash._live_tables("kiwoom")[3]["gc_20_60"]


def test_dashboard_has_config_panels_and_two_confirms():
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert 'id="lc-mock"' in src and 'id="lc-strength"' in src
    assert "confirm('[1/2]" in src and "confirm('[2/2]" in src and "{confirm:2}" in src
    assert "lcLoad(); }catch(e){}" in src


# ── 백테스트 손절·익절 ──
def test_apply_exits_close_and_intraday():
    import period_backtest as P
    ds = np.array(["d0", "d1", "d2", "d3", "d4"])
    op = np.array([100, 100, 96, 85, 90.0]); hi = np.array([101, 101, 97, 88, 95.0])
    lo = np.array([99, 89, 84, 80, 85.0]); cl = np.array([100, 95, 88, 86, 92.0])
    panel = {"A": (ds, op, hi, lo, cl)}
    t = NS(entry_date="d0", exit_date="d4", code="A", entry_price=100.0, net_pct=-8.245, gross_pct=-8.0, score=1)
    # 종가 기준: d2 종가 88 ≤ 90 → d3 시가 85 에 청산
    r = P.apply_exits([t], panel, stop=-10, mode="close")[0]
    assert r.exit_date == "d3" and abs(r.gross_pct - (-15.0)) < 1e-9 and abs(r.net_pct - (-15.245)) < 1e-9
    # 장중: d1 저가 89 ≤ 90 → 그 가격 90
    r = P.apply_exits([t], panel, stop=-10, mode="intraday")[0]
    assert r.exit_date == "d1" and abs(r.gross_pct - (-10.0)) < 1e-9
    # 익절 장중: 고가가 닿는 날 — 없으면 원래 매매 그대로
    assert P.apply_exits([t], panel, take=20, mode="intraday")[0] is t
    with pytest.raises(ValueError):
        P.parse_exits(5, None)
    assert P.parse_exits("", "20") == (None, 20.0)


def test_pbt_api_passes_exit_rules(dash, monkeypatch):
    calls = []
    monkeypatch.setattr(dash._sp, "Popen", lambda cmd, **kw: calls.append(cmd) or NS(poll=lambda: None, pid=1))
    monkeypatch.setattr(dash, "_PBT_PROC", {"proc": None, "job": None})
    body = {"start": "20240101", "end": "20241231", "names": ["rsi_reversal"], "stop": "-10", "take": "20",
            "exit_mode": "intraday"}
    with dash.app.test_client() as c:
        assert c.post("/api/pbt/run", json={**body, "stop": "10"}, environ_base=LOCAL, headers=ACTION).status_code == 400
        assert c.post("/api/pbt/run", json={**body, "exit_mode": "x"}, environ_base=LOCAL, headers=ACTION).status_code == 400
        assert c.post("/api/pbt/run", json=body, environ_base=LOCAL, headers=ACTION).get_json()["ok"]
    cmd = calls[0]
    assert cmd[cmd.index("--stop") + 1] == "-10" and cmd[cmd.index("--take") + 1] == "20"
    assert cmd[cmd.index("--exit-mode") + 1] == "intraday"


# ── [2026-10-22] 강도 피처 일일 수집 ──
def test_scheduler_has_daily_indicator_and_kiwoom_jobs():
    src = open(os.path.join(os.path.dirname(OUTPUTS), "Stock_AI_Project", "scheduler.py"), encoding="utf-8").read()
    assert 'schedule.every().day.at("16:15").do(daily_indicators_job)' in src
    assert 'schedule.every().day.at("17:30").do(kiwoom_daily_job)' in src
    i = src.index("def kiwoom_daily_job")
    body = src[i:src.index("def weekend_audit_job")]
    assert body.count('"--force"') == 2 and "_is_krx_holiday()" in body and "_run_in_background" in body
    j = src.index("def daily_indicators_job")
    assert "_korea_stocks_rows(today) >= 1000" in src[j:i], "오늘 가격이 들어온 뒤에 계산"


def test_watchdog_thresholds_follow_daily_collection():
    src = open(os.path.join(OUTPUTS, "watchdog.py"), encoding="utf-8").read()
    assert '("korea_indicators", "date", 3, True)' in src and '("program_trading", "date", 3, True)' in src
    assert '("credit_balance", "date", 4, True)' in src
