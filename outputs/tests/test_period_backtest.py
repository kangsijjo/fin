# -*- coding: utf-8 -*-
"""
기간 백테스트 테스트기(period_backtest.py + 대시보드 /api/pbt/*) — 2026-10-18

고정하는 것
  · 데이터 충분성 점검: 가격 범위 밖·워밍업 부족·보조데이터 구간 밖·수집 중단·짧은 기간
  · 일별 자본곡선 재현기 = capital_simulator.simulate_capital(MTM) 과 같은 결과
  · 대시보드 POST 는 /api/run 과 같은 규칙(CSRF 커스텀 헤더 + 로컬), 입력 화이트리스트, 동시 1건
"""
import json
import os
import sys
from types import SimpleNamespace as NS

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
ACTION = {"X-Dashboard-Action": "1"}


def _cal(n=400, start=20200101):
    """주말 없는 가짜 거래일 n개(YYYYMMDD 정렬만 맞으면 됨)."""
    import pandas as pd
    return [d.strftime("%Y%m%d") for d in pd.bdate_range(str(start), periods=n)]


def test_warmup_days_known_strategies():
    import period_backtest as P
    import strategy_engine as E
    m = {s.name: s for s in E.ALL_STRATEGIES}
    assert P.warmup_days(m["star_high_500d_40"]) == 500 + 60 + 5     # 500일 신고가 + 시장게이트
    assert P.warmup_days(m["rsi_reversal"]) == 14 + 5
    assert P.warmup_days(m["inst_for_3d"]) == 3 + 5                  # n_days 만 있는 전략도 과대추정 안 함
    assert P.aux_needs(m["h52w_cred_dec"]) == ["credit"]


def test_check_data_flags_every_shortage():
    import period_backtest as P
    import strategy_engine as E
    smap = {s.name: s for s in E.ALL_STRATEGIES}
    cal = _cal(400)
    cov = {"foreign": [cal[0], cal[-1]], "inst": [cal[0], cal[-1]],
           "individual": [cal[100], cal[300]], "short": None, "credit": [cal[50], cal[-1]]}
    # 시작 이전 → 오류
    w, _, ok = P.check_data("19990101", cal[-1], ["rsi_reversal"], cal, cov, False, smap)
    assert not ok and w[0]["level"] == "error"
    # 정상 + 여러 경고
    w, eff, ok = P.check_data(cal[10], "29991231", ["star_high_500d_40", "supply_abc", "h52w_short_dec"],
                              cal, cov, True, smap)
    msgs = " | ".join(x["msg"] for x in w)
    assert ok and eff == cal[-1]
    assert "까지입니다" in msgs                         # 종료일 잘림
    assert "565거래일" in msgs                          # 워밍업 부족
    assert "개인 순매수 데이터는" in msgs and "멈춰 있습니다" in msgs   # 시작 이전 + 수집 중단
    assert "공매도 거래비중 데이터가 없습니다" in msgs
    assert "운 검정은 전략 5개 이상" in msgs
    # 기간 너무 짧음 → 오류
    w, _, ok = P.check_data(cal[10], cal[15], ["rsi_reversal"], cal, cov, False, smap)
    assert not ok
    # 알 수 없는 전략 → 오류
    w, _, ok = P.check_data(cal[10], cal[300], ["nope"], cal, cov, False, smap)
    assert not ok


def test_check_data_uses_first_trading_day_and_emits_collect_hints():
    """2026-10-18 오경보: 시작일 1/1(휴장) vs 데이터 첫날 1/4 → '부터입니다' 경고가 나면 안 된다."""
    import period_backtest as P
    import strategy_engine as E
    smap = {s.name: s for s in E.ALL_STRATEGIES}
    cal = _cal(400)
    holiday = cal.pop(200)                       # 휴장일 하나를 달력에서 뺀다
    cov = {"foreign": [cal[0], cal[-1]], "inst": [cal[0], cal[-1]],
           "individual": [cal[200], cal[300]], "short": [cal[200], cal[-1]], "credit": None}
    w, _, ok = P.check_data(holiday, cal[-1], ["supply_abc", "h52w_short_dec", "h52w_cred_dec"],
                            cal, cov, False, smap)
    msgs = " | ".join(x["msg"] for x in w)
    assert ok and "부터입니다" not in msgs, msgs
    hints = [x["collect"] for x in w if x.get("collect")]
    assert {"aux": "individual", "start": cal[301], "end": cal[-1]} in hints     # 멈춤 구간
    assert {"aux": "credit", "start": cal[200], "end": cal[-1]} in hints         # 아예 없음
    # 진짜로 앞이 비면 그 구간을 정확히 제안
    w, _, _ = P.check_data(cal[150], cal[-1], ["h52w_short_dec"], cal, cov, False, smap)
    h = [x["collect"] for x in w if x.get("collect")]
    assert h == [{"aux": "short", "start": cal[150], "end": cal[199]}]


def test_collect_kinds_cover_every_aux_key_and_build_safe_commands():
    import collect_aux as C
    import period_backtest as P
    assert set(C.AUX_TO_KIND) == set(P._AUX_LABEL), "점검이 요구하는 데이터마다 수집기가 있어야 한다"
    cmd, _ = C.KINDS["individual"]["cmd"]("20180614", "20201230")
    assert cmd[-3:] == ["--backfill", "20180614", "20201230"]
    cmd, _ = C.KINDS["short"]["cmd"]("20180614", "20201230")
    assert cmd[cmd.index("--start") + 1] == "20180614" and cmd[cmd.index("--end") + 1] == "20201230"
    cmd, cwd = C.KINDS["credit"]["cmd"]("20210104", "20211230")
    assert cmd[cmd.index("--since") + 1] == "2021-01-04" and cwd.endswith("Stock_AI_Project")


def test_collect_invalidates_caches(tmp_path, monkeypatch):
    import collect_aux as C
    import period_backtest as P
    from strategies import daily_loader as L
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps({"data_max": "20261016", "items": {}}), encoding="utf-8")
    sig = tmp_path / "_daily_cache.sig.json"
    sig.write_text(json.dumps([3, 10, "x.csv", 1]), encoding="utf-8")
    monkeypatch.setattr(P, "COVERAGE_JSON", str(cov))
    monkeypatch.setattr(C, "LOADER_DIR", str(tmp_path))
    C.invalidate_caches("individual")
    assert json.loads(cov.read_text(encoding="utf-8"))["data_max"] == ""
    assert L._read_sig(str(sig)) != (3, 10, "x.csv", 1)


def test_archive_cleanup_keeps_latest_and_restores(tmp_path, monkeypatch):
    import archive_cleanup as A
    root = tmp_path / "fin"
    res = root / "outputs" / "results"
    res.mkdir(parents=True)
    names = ["trades_AB_20260504-20261015.csv", "trades_AB_20260504-20261016.csv",
             "trades_A_20260504-20261016.csv", "strategy_lab_trades_20261015.csv",
             "strategy_lab_trades_20261016.csv", "exit_compare_20261016.csv"]
    for n in names:
        (res / n).write_text("x", encoding="utf-8")
    picked = sorted(os.path.basename(p) for p in A.pick_snapshots(str(res)))
    assert picked == ["strategy_lab_trades_20261015.csv", "trades_AB_20260504-20261015.csv"]
    monkeypatch.setattr(A, "ROOT", str(root))
    monkeypatch.setattr(A, "ARCHIVE_ROOT", str(root / "_archive"))
    monkeypatch.setattr(A, "PICKERS", {"snapshots": lambda: A.pick_snapshots(str(res))})
    moved, skipped, _, dest = A.archive(["snapshots"], stamp="20261018", now=9e12)
    assert len(moved) == 2 and not skipped
    assert not (res / "trades_AB_20260504-20261015.csv").exists()
    assert (res / "trades_AB_20260504-20261016.csv").exists(), "최신은 남긴다"
    A.restore(dest)
    assert (res / "trades_AB_20260504-20261015.csv").exists()


def test_run_recheck_collects_individual_daily():
    raw = open(os.path.join(OUTPUTS, "run_recheck.bat"), "rb").read()
    assert b"backfill_individual.py --backfill !IND_LO! !TODAY!" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "bat 은 CRLF"
    raw.decode("ascii")


def test_equity_curve_matches_simulate_capital():
    import period_backtest as P
    from capital_simulator import simulate_capital
    cal = _cal(30)
    pm = {}
    for i, d in enumerate(cal):
        pm[("A", d)] = 100 + i
        pm[("B", d)] = 100 - i * 0.5
        pm[("C", d)] = 100 + (i % 3)
    tr = [NS(entry_date=cal[1], exit_date=cal[8], code="A", entry_price=101, net_pct=6.0, gross_pct=6.0, score=2),
          NS(entry_date=cal[1], exit_date=cal[12], code="B", entry_price=99.5, net_pct=-5.0, gross_pct=-5.0, score=1),
          NS(entry_date=cal[9], exit_date=cal[20], code="C", entry_price=100, net_pct=2.0, gross_pct=2.0, score=1)]
    eq, taken = P.equity_curve(tr, pm, cal, slots=2)
    ref = simulate_capital(tr, max_concurrent=2, price_map=pm, trading_dates=cal)
    assert abs((eq.iloc[-1] / 1e7 - 1) * 100 - ref["total_ret_pct"]) < 0.01
    assert taken == [6.0, -5.0, 2.0]
    st = P.curve_stats(eq)
    assert set(st) >= {"total_ret", "cagr", "mdd", "sharpe", "yearly"}


def test_luck_test_runs():
    pytest.importorskip("arch")
    import numpy as np
    import pandas as pd
    import period_backtest as P
    rng = np.random.default_rng(0)
    R = pd.DataFrame(rng.normal(0, 1, (200, 6)), columns=list("abcdef"))
    out = P.luck_test(R, pd.Series(np.zeros(200)))
    assert 0 <= out["pbo"] <= 1 and 0 <= out["dsr"] <= 1 and "spa_cash" in out


# ── 대시보드 ──
@pytest.fixture(scope="module")
def dash():
    import integrated_dashboard_server as d
    d.app.config["TESTING"] = True
    return d


def test_pbt_meta_lists_strategies_and_presets(dash):
    with dash.app.test_client() as c:
        d = c.get("/api/pbt/meta", environ_base=LOCAL).get_json()
    assert len(d["strategies"]) == len(d["presets"]["all"]) >= 50
    assert len(d["presets"]["live"]) == 6


def test_pbt_post_requires_action_header_and_valid_input(dash):
    body = {"start": "20240101", "end": "20241231", "names": ["rsi_reversal"]}
    with dash.app.test_client() as c:
        for url in ("/api/pbt/check", "/api/pbt/run"):
            assert c.post(url, json=body, environ_base=LOCAL).status_code == 403          # CSRF
            assert c.post(url, json={**body, "start": "2024-01-01"}, environ_base=LOCAL,
                          headers=ACTION).status_code == 400                                # 날짜 형식
            assert c.post(url, json={**body, "names": ["x; rm -rf /"]}, environ_base=LOCAL,
                          headers=ACTION).status_code == 400                                # 화이트리스트
            assert c.post(url, json=body, environ_base={"REMOTE_ADDR": "10.0.0.5"},
                          headers=ACTION).status_code in (302, 403)                         # 원격 차단
        assert c.get("/api/pbt/status?job=../../x", environ_base=LOCAL).status_code == 400


def test_pbt_run_spawns_once_and_check_parses(dash, monkeypatch):
    calls = []

    class FakeProc:
        pid = 1

        def poll(self):
            return None                      # 계속 실행 중

    monkeypatch.setattr(dash._sp, "Popen", lambda cmd, **kw: calls.append(cmd) or FakeProc())
    monkeypatch.setattr(dash, "_PBT_PROC", {"proc": None, "job": None})
    body = {"start": "20240101", "end": "20241231", "names": ["rsi_reversal", "gc_for3d"], "luck": True}
    with dash.app.test_client() as c:
        r = c.post("/api/pbt/run", json=body, environ_base=LOCAL, headers=ACTION).get_json()
        assert r["ok"] and dash._PBT_JOB_RE.fullmatch(r["job"])
        cmd = calls[0]
        assert cmd[cmd.index("--names") + 1] == "rsi_reversal,gc_for3d"
        assert cmd[cmd.index("--job") + 1] == r["job"]
        assert cmd[cmd.index("--start") + 1] == "20240101" and "--luck" in cmd
        r2 = c.post("/api/pbt/run", json=body, environ_base=LOCAL, headers=ACTION)
        assert r2.status_code == 409, "동시에 두 번 돌면 안 된다"

    out = "noise\nPBT_CHECK " + json.dumps({"ok": True, "warnings": [], "eff_end": "20241231", "n_days": 244})
    monkeypatch.setattr(dash._sp, "run",
                        lambda cmd, **kw: NS(stdout=out.encode("utf-8"), stderr=b"", returncode=0))
    with dash.app.test_client() as c:
        d = c.post("/api/pbt/check", json=body, environ_base=LOCAL, headers=ACTION).get_json()
    assert d["ok"] and d["n_days"] == 244


def test_collect_endpoint_guards_and_single_job(dash, monkeypatch, tmp_path):
    import collect_aux as C
    calls = []

    class FakeProc:
        pid = 1

        def poll(self):
            return None

    monkeypatch.setattr(dash._sp, "Popen", lambda cmd, **kw: calls.append(cmd) or FakeProc())
    monkeypatch.setattr(dash, "_COLLECT_PROC", {"proc": None, "kind": None})
    body = {"kind": "individual", "start": "20180614", "end": "20201230"}
    with dash.app.test_client() as c:
        assert c.post("/api/pbt/collect", json=body, environ_base=LOCAL).status_code == 403          # CSRF
        assert c.post("/api/pbt/collect", json=body, environ_base={"REMOTE_ADDR": "10.0.0.5"},
                      headers=ACTION).status_code in (302, 403)                                     # 원격
        assert c.post("/api/pbt/collect", json={**body, "kind": "rm -rf"}, environ_base=LOCAL,
                      headers=ACTION).status_code == 400                                            # 화이트리스트
        assert c.post("/api/pbt/collect", json={**body, "start": "20210101", "end": "20200101"},
                      environ_base=LOCAL, headers=ACTION).status_code == 400                        # 역순
        r = c.post("/api/pbt/collect", json=body, environ_base=LOCAL, headers=ACTION).get_json()
        assert r["ok"] and calls[0][-6:] == ["--kind", "individual", "--start", "20180614", "--end", "20201230"]
        assert c.post("/api/pbt/collect", json={**body, "kind": "short"}, environ_base=LOCAL,
                      headers=ACTION).status_code == 409, "한 번에 한 종류만"


def test_collect_status_marks_dead_runner_stale(dash, monkeypatch, tmp_path):
    import collect_aux as C
    monkeypatch.setattr(C, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dash, "_COLLECT_PROC", {"proc": None, "kind": None})
    p = tmp_path / "collect_short.json"
    p.write_text(json.dumps({"kind": "short", "status": "running"}), encoding="utf-8")
    old = __import__("time").time() - 600
    os.utime(p, (old, old))
    with dash.app.test_client() as c:
        d = c.get("/api/pbt/collect_status", environ_base=LOCAL).get_json()
    st = {k["kind"]: k["state"] for k in d["kinds"]}
    assert st["short"]["status"] == "stale" and st["individual"] is None
    assert d["aux_to_kind"]["individual"] == "individual"
