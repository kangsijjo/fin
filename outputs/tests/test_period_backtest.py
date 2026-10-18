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
    import strategy_engine as E
    assert set(d["presets"]["live"]) == set(E.LIVE_MAP)        # 2026-10-18 키움 교체 후 5개(키움 2 + KIS 3)


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


# ── 부족분 한 번에 수집 + 완료/미완료 보고 (2026-10-18) ──
def test_verdict_and_report_lines(monkeypatch):
    import collect_aux as C
    cal = _cal(30)
    monkeypatch.setattr(C, "_cal", lambda: cal)
    monkeypatch.setattr(C, "have_days", lambda k, s, e: set(cal) - {cal[3], cal[4], cal[5], cal[9]})
    v = C._verdict("individual", cal[0], cal[-1], 0)
    assert v["status"] == "partial" and v["missing_n"] == 4 and v["n_days"] == 30
    assert v["ranges"] == f"{cal[3]}~{cal[5]}(3일), {cal[9]}"
    line = C.step_line(v)
    assert line.startswith("⚠ 개인 순매수") and "4/30거래일 미완료" in line
    monkeypatch.setattr(C, "have_days", lambda k, s, e: set(cal))
    assert C.step_line(C._verdict("short", cal[0], cal[-1], 0)).startswith("✅")
    monkeypatch.setattr(C, "have_days", lambda k, s, e: set())
    v = C._verdict("credit", cal[0], cal[-1], 1)
    assert v["status"] == "failed" and "(rc=1)" in C.step_line(v)


def test_short_ban_days_not_counted_as_missing(monkeypatch):
    import collect_aux as C
    cal = _cal(40, start=20200309)                # 금지기간(2020-03-16~) 걸침
    monkeypatch.setattr(C, "_cal", lambda: cal)
    monkeypatch.setattr(C, "have_days", lambda k, s, e: {d for d in cal if d < "20200316"})
    v = C._verdict("short", cal[0], cal[-1], 0)
    assert v["status"] == "done" and "금지기간" in v["note"]


def test_plan_all_ranges():
    import collect_aux as C
    cal = ["20180614", "20180615"] + [f"2021010{i}" for i in (4, 5, 6, 7, 8)] + ["20261014", "20261015", "20261016"]
    plan = dict((k, (s, e)) for k, s, e in C.plan_all(cal))
    assert list(dict.fromkeys(k for k, _, _ in C.plan_all(cal))) == ["flow", "individual", "short", "credit"]
    assert plan["individual"] == ("20180614", "20261015")      # 오늘분은 다음 자동수집이 채움
    assert plan["credit"] == ("20210104", "20261014")          # 신용은 2일 늦게 나옴


def test_run_all_reports_done_and_missing(monkeypatch, tmp_path):
    import collect_aux as C
    cal = _cal(30)
    sent, calls = [], []
    monkeypatch.setattr(C, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(C, "_cal", lambda: cal)
    monkeypatch.setattr(C, "plan_all", lambda c, now=None: [("individual", cal[0], cal[-1]),
                                                            ("short", cal[0], cal[-1]),
                                                            ("credit", cal[0], cal[-1])])
    monkeypatch.setattr(C, "_exec", lambda kind, cmd, cwd, st, key, log_mode="w": calls.append((kind, cmd)) or 0)
    monkeypatch.setattr(C, "invalidate_caches", lambda k: None)
    monkeypatch.setattr(C, "_refresh_coverage", lambda st: None)
    monkeypatch.setattr(C, "_market_hours", lambda now=None: False)
    monkeypatch.setattr(C, "_credit_pre2022_rows", lambda: 0)            # 키움이 2022 이전을 안 줌
    monkeypatch.setattr(C, "_notify", sent.append)
    have = {"individual": set(cal), "short": set(cal[:20]), "credit": set()}
    monkeypatch.setattr(C, "have_days", lambda k, s, e: have[k])
    rc = C.run_all()
    rep = sent[-1]
    assert "완료 1 · 일부 미완료 1 · 실패 1" in rep, rep
    assert "✅ 개인 순매수" in rep and "⚠ 공매도 거래비중" in rep and "10/30거래일 미완료" in rep
    assert "❌ 신용잔고" in rep and "FHPST04760000" in rep
    assert [k for k, _ in calls] == ["individual", "short", "credit"]
    assert "--limit" in calls[-1][1], "신용은 시험 수집만 하고 본 수집은 생략"
    assert rc == 1
    st = json.loads((tmp_path / "collect_all.json").read_text(encoding="utf-8"))
    assert st["status"] == "done" and [s["status"] for s in st["steps"]] == ["done", "partial", "failed"]


def test_credit_skipped_during_market_hours(monkeypatch, tmp_path):
    import collect_aux as C
    cal = _cal(10)
    sent = []
    monkeypatch.setattr(C, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(C, "_cal", lambda: cal)
    monkeypatch.setattr(C, "plan_all", lambda c, now=None: [("credit", cal[0], cal[-1])])
    monkeypatch.setattr(C, "_exec", lambda *a, **k: (_ for _ in ()).throw(AssertionError("장중엔 실행 금지")))
    monkeypatch.setattr(C, "_refresh_coverage", lambda st: None)
    monkeypatch.setattr(C, "_market_hours", lambda now=None: True)
    monkeypatch.setattr(C, "_notify", sent.append)
    assert C.run_all() == 0
    assert "⏭ 신용잔고" in sent[-1] and "건너뜀 1" in sent[-1]


def test_collect_lock_blocks_second_runner(monkeypatch, tmp_path):
    import collect_aux as C
    monkeypatch.setattr(C, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(C, "LOCK_PATH", str(tmp_path / "collect.lock"))
    assert C._acquire_lock() and not C._acquire_lock()
    old = __import__("time").time() - 600
    os.utime(C.LOCK_PATH, (old, old))
    assert C._acquire_lock(), "죽은 실행기의 자물쇠는 넘겨받는다"
    C._release_lock()
    assert not os.path.exists(C.LOCK_PATH)


def test_collect_all_endpoint_and_bat(dash, monkeypatch, tmp_path):
    import collect_aux as C
    calls = []

    class FakeProc:
        pid = 1

        def poll(self):
            return None

    monkeypatch.setattr(dash._sp, "Popen", lambda cmd, **kw: calls.append(cmd) or FakeProc())
    monkeypatch.setattr(dash, "_COLLECT_PROC", {"proc": None, "kind": None})
    monkeypatch.setattr(C, "LOCK_PATH", str(tmp_path / "collect.lock"))
    with dash.app.test_client() as c:
        r = c.post("/api/pbt/collect", json={"kind": "all"}, environ_base=LOCAL, headers=ACTION).get_json()
        assert r["ok"] and calls[0][-2:] == ["--kind", "all"]
    # bat 이 돌고 있으면(자물쇠 신선) 대시보드 버튼은 409
    monkeypatch.setattr(dash, "_COLLECT_PROC", {"proc": None, "kind": None})
    (tmp_path / "collect.lock").write_text("1")
    with dash.app.test_client() as c:
        assert c.post("/api/pbt/collect", json={"kind": "all"}, environ_base=LOCAL,
                      headers=ACTION).status_code == 409
        assert c.get("/api/pbt/collect_status", environ_base=LOCAL).get_json()["running"] == "external"
    raw = open(os.path.join(OUTPUTS, "collect_all.bat"), "rb").read()
    raw.decode("ascii")
    assert b"collect_aux.py --kind all" in raw and raw.count(b"\n") == raw.count(b"\r\n")


def test_state_write_survives_windows_replace_lock(tmp_path, monkeypatch):
    """2026-10-18 실사고: 대시보드가 읽는 순간 os.replace 가 WinError 5 → 수집 실행기 사망."""
    import period_backtest as P
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(5, "액세스가 거부되었습니다")
        return real(a, b)

    monkeypatch.setattr(P.os, "replace", flaky)
    target = tmp_path / "s.json"
    assert P.write_json_atomic(str(target), {"a": 1}, wait=0) is True
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1} and calls["n"] == 4
    monkeypatch.setattr(P.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(5, "x")))
    assert P.write_json_atomic(str(target), {"a": 2}, tries=2, wait=0) is True     # 제자리 쓰기로 마무리
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


# ── 전략 설명 팝업 + 지표 툴팁 (2026-10-18) ──
def test_every_strategy_has_plain_explanation():
    import strategy_engine as E
    import strategy_explain as X
    for s in E.ALL_STRATEGIES:
        d = X.explain(s)
        assert d and d["title"] and d["idea"] and d["buy"] and d["sell"], s.name
    m = {s.name: s for s in E.ALL_STRATEGIES}
    d = X.explain(m["star_high_500d_20"])
    assert any("500거래일" in b for b in d["buy"]) and any("20거래일째" in x for x in d["sell"])
    assert any("미래" in c or "늦게 공개" in c for c in X.explain(m["h52w_cred_dec"])["caution"])
    assert X.explain(m["macd_zx"])["caution"], "0선 조건이 무효라는 사실을 숨기지 않는다"
    assert not X.explain(m["macd_all"])["caution"]


def test_macd_zero_cross_flag_is_currently_a_noop():
    """설명 팝업의 주의 문구가 사실인지 고정 — 코드를 고치면 이 테스트와 문구를 같이 바꿀 것."""
    import numpy as np
    import pandas as pd
    from strategies.macd_cross import MacdCrossStrategy
    import strategy_engine as E
    rng = np.random.default_rng(1)
    dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("20230102", periods=300)]
    rows = []
    for code in ("000001", "000002", "000003"):
        px = 10000 * np.cumprod(1 + rng.normal(0, 0.02, len(dates)))
        rows += [{"code": code, "date": d, "open": p, "high": p * 1.01, "low": p * 0.99, "close": p,
                  "volume": 1e6, "trading_value": 5e9, "change_pct": 0.0} for d, p in zip(dates, px)]
    df = pd.DataFrame(rows)
    a = MacdCrossStrategy(require_zero_cross=True, holding_days=5).backtest(df.copy(), E.DEFAULT_COSTS)
    b = MacdCrossStrategy(require_zero_cross=False, holding_days=5).backtest(df.copy(), E.DEFAULT_COSTS)
    assert [(t.code, t.entry_date) for t in a] == [(t.code, t.entry_date) for t in b]


def test_dashboard_has_strategy_popup_and_metric_tips(dash):
    with dash.app.test_client() as c:
        d = c.get("/api/pbt/meta", environ_base=LOCAL).get_json()
    assert all(s["desc"] and s["desc"]["buy"] for s in d["strategies"])
    assert d["common_rules"]
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    for k in ("cagr:", "mdd:", "sharpe:", "slot:", "vs:"):
        assert k in src[src.index("const _PBT_TIPS={"):src.index("function _pbtTipInit")], k
    assert "function pbtExplain(name)" in src and "onclick=\"pbtExplain(" in src
    assert "th('cagr','CAGR')" in src and "th('mdd','MDD')" in src and "th('sharpe','샤프')" in src


# ── 강도 비교 (2026-10-19) ──
def test_parse_strength_validates_and_normalizes():
    import period_backtest as P
    assert P.parse_strength("") == [] and P.parse_strength(None) == []
    assert P.parse_strength("5.7, 4,5 4") == [4.0, 5.0, 5.7]
    for bad in ("11", "-1", "5;rm", "1,2,3,4,5,6", "abc"):
        with pytest.raises(ValueError):
            P.parse_strength(bad)


def test_check_data_warns_years_without_strength_training():
    import period_backtest as P
    import strategy_engine as E
    smap = {s.name: s for s in E.ALL_STRATEGIES}
    cal = [d.strftime("%Y%m%d") for d in __import__("pandas").bdate_range("20180102", "20211230")]
    cov = {k: [cal[0], cal[-1]] for k in ("foreign", "inst", "individual", "short", "credit")}
    w, _, ok = P.check_data("20190102", "20211230", ["rsi_reversal"], cal, cov, False, smap,
                            [5.7], {"2018": 1500, "2019": 8000, "2020": 9000})
    msgs = " | ".join(x["msg"] for x in w)
    assert ok and "2019년은 강도 학습 표본이 1,500건" in msgs and "2020년은" not in msgs
    assert "≥5.7" in msgs
    w, _, ok = P.check_data("20190102", "20211230", ["rsi_reversal"], cal, cov, False, smap, [5.7], None)
    assert not ok, "학습 데이터가 없으면 실행 거부"


def test_score_rows_matches_factor_scorer_formula():
    """백테스트 채점식 = 라이브 factor_scorer.score_ic (available-only·커버리지 가드·0~10)."""
    import numpy as np
    import pandas as pd
    import strength_backtest as SB
    from factor_scorer import FactorScorer
    rng = np.random.default_rng(0)
    feats = ["rsi14", "vix", "tv_ratio", "crd_remn_rt"]
    train = pd.DataFrame({f: rng.normal(size=500) for f in feats})
    train["net_pct"] = train["rsi14"] * -0.5 + train["tv_ratio"] * 0.3 + rng.normal(size=500)
    ic, ref = SB.fit_scorer(train, feats)
    fs = FactorScorer.__new__(FactorScorer)
    fs.ic_weights, fs.feat_stats = dict(ic), {k: list(v) for k, v in ref.items()}
    X = pd.DataFrame({f: rng.normal(size=40) for f in feats})
    X.loc[:9, "rsi14"] = np.nan                       # 일부 결측 → available-only 분모
    X.loc[10:12, ["rsi14", "tv_ratio", "crd_remn_rt"]] = np.nan   # 커버리지 미달 → 전체 분모
    mine = SB.score_rows(X, ic, ref, 0.40)
    theirs = [fs.score_ic({f: (None if np.isnan(v) else float(v)) for f, v in r.items()})["total"]
              for r in X.to_dict("records")]
    assert np.allclose(mine, theirs, atol=0.006)


def test_asof_takes_latest_row_on_or_before_signal_date():
    import pandas as pd
    import strength_backtest as SB
    L = pd.DataFrame({"code": ["A", "A", "B"], "date": ["20240105", "20240103", "20240105"]})
    R = pd.DataFrame({"code": ["A", "A", "B"], "date": ["20240102", "20240104", "20240106"], "v": [1.0, 2.0, 9.0]})
    m = SB._asof(L, R, ["v"]).set_index(["code", "date"])["v"]
    assert m[("A", "20240105")] == 2.0 and m[("A", "20240103")] == 1.0
    assert m.isna()[("B", "20240105")], "미래 행(01-06)을 끌어오면 안 된다"


def test_walk_forward_uses_only_prior_years(monkeypatch):
    import numpy as np
    import pandas as pd
    import strength_backtest as SB
    seen = []
    monkeypatch.setattr(SB, "fit_scorer", lambda tr, feats: (seen.append(tr["entry_date"].max()) or ({}, {})))
    monkeypatch.setattr(SB, "_ic_constants", lambda: (["rsi14"], 0.4, {"rsi_reversal"}))
    hist = pd.DataFrame({"entry_date": ["20220601"] * 2500 + ["20230601"] * 2500, "strategy": "rsi_reversal",
                         "net_pct": 0.0, "rsi14": 1.0})
    X = pd.DataFrame({"rsi14": [1.0, 1.0, 1.0]})
    sc, info = SB.walk_forward_scores(X, ["2022", "2023", "2024"], hist)
    assert info["2022"]["skipped"] and np.isnan(sc[0]), "2022 이전 표본 0건 → 채점 안 함"
    assert seen == ["20220601", "20230601"], "각 해는 그 이전 매매로만 학습"


def test_run_job_strength_rows(monkeypatch, tmp_path):
    """강도 모드: 기본 줄 + 임계별 줄, 강도 미달·무채점 신호 제외, 강도순 배정."""
    import numpy as np
    import period_backtest as P
    import strength_backtest as SB
    cal = _cal(140, start=20240101)
    import pandas as pd
    rows = [{"code": c, "date": d, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0 + i * (0.5 if c == "A" else -0.2),
             "change_pct": 0.1, "trading_value": 5e9, "volume": 1e5}
            for i, d in enumerate(cal) for c in ("A", "B", "C")]
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
    monkeypatch.setattr(P, "history_years", lambda: {"2023": 9999})
    monkeypatch.setattr(P, "_notify", lambda t: None)
    import strategies.daily_loader as DL
    monkeypatch.setattr(DL, "load_macro_daily", lambda *a, **k: df.copy())
    monkeypatch.setattr(SB, "score_trades", lambda d, t, log=print: (np.array([6.0, 4.5, np.nan]), {"2024": {"n_train": 9999}}))
    P.run_job("pbt_20991231_000001", cal[90], cal[-1], ["fake"], False, [4.0, 5.7])
    st = json.load(open(P._job_path("pbt_20991231_000001"), encoding="utf-8"))
    r = {x["strategy"]: x for x in st["results"]}
    assert set(r) == {"fake", "fake ≥4", "fake ≥5.7"}
    assert r["fake"]["n_trades"] == 3 and r["fake ≥4"]["n_trades"] == 2 and r["fake ≥5.7"]["n_trades"] == 1
    assert r["fake ≥5.7"]["avg_net"] == 5.0 and r["fake ≥4"]["pass_pct"] == round(2 / 3 * 100, 1)
    assert st["params"]["strength"] == [4.0, 5.7] and st["strength_info"]["2024"]["n_train"] == 9999
    assert any("강도를 매기지 못한 신호 1건" in w["msg"] for w in st["warnings"])


def test_pbt_api_passes_strength(dash, monkeypatch):
    calls = []

    class FakeProc:
        pid = 1

        def poll(self):
            return None

    monkeypatch.setattr(dash._sp, "Popen", lambda cmd, **kw: calls.append(cmd) or FakeProc())
    monkeypatch.setattr(dash, "_PBT_PROC", {"proc": None, "job": None})
    body = {"start": "20240101", "end": "20241231", "names": ["rsi_reversal"], "strength": "5.7, 4"}
    with dash.app.test_client() as c:
        assert c.post("/api/pbt/run", json={**body, "strength": "4;x"}, environ_base=LOCAL,
                      headers=ACTION).status_code == 400
        r = c.post("/api/pbt/run", json=body, environ_base=LOCAL, headers=ACTION).get_json()
        assert r["ok"]
        cmd = calls[0]
        assert cmd[cmd.index("--strength") + 1] == "4,5.7"
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert 'id="pbt-strength"' in src and "strength:(document.getElementById('pbt-strength')" in src
