# -*- coding: utf-8 -*-
"""§92 — 데이터 정체 경고에 '누를 버튼' 안내 + 주가 수집기 확정일 컷오프·마지막 날 덮어쓰기 + 지난 미완성 일봉 복구.

실사고(2026-11-02): 대시보드가 'korea_indicators 6일 정체'라고만 해서 어느 버튼인지 몰랐다. 원인은
  ① 평일 16:15 기술지표 갱신 앞의 주가 수집이 '마지막+1일 >= 오늘'이면 건너뛰어 오늘 가격을 끝내 못 받음(10-22 이후 매일 생략),
  ② 06:30 수집이 09:00 를 넘기면 오늘 '장중 미완성 일봉'을 저장하고 다시 안 고침(10-08·10-29·11-02 실측).
"""
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime
from types import SimpleNamespace as NS

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
FIN = os.path.dirname(OUTPUTS)
SAI = os.path.join(FIN, "Stock_AI_Project")
if SAI not in sys.path:
    sys.path.insert(0, SAI)
# 테스트가 Stock_AI_Project/logs 의 실제 로그 파일에 쓰지 않게(get_logger 는 핸들러가 이미 있으면 그대로 돌려줌)
for _n in ("collector",):
    if not logging.getLogger(_n).handlers:
        logging.getLogger(_n).addHandler(logging.NullHandler())

import fresh_fix as FF


# ── A. 경고 → 버튼 안내 ──
def _html():
    import ast
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    t = ast.parse(src)
    return [ast.literal_eval(n.value) for n in t.body
            if isinstance(n, ast.Assign) and any(getattr(x, "id", None) == "HTML" for x in n.targets)][0]


def test_fix_map_matches_dashboard_buttons():
    """안내하는 버튼 이름 = 데이터+로그 탭에서 그 표를 가장 콕 집는 data-fresh 버튼 이름(빨갛게 깜빡이는 그 버튼)."""
    btns = re.findall(r'<button class="runbtn" data-fresh="([^"]+)"[^>]*>([^<]+)</button>', _html())
    assert btns, "data-fresh 버튼을 못 찾음"
    tables = {t for f, _ in btns for t in f.split(",")}
    assert tables <= set(FF.FIX), f"안내 표에 없는 표: {tables - set(FF.FIX)}"
    for t in FF.FIX:
        cand = sorted(((len(f.split(",")), lab) for f, lab in btns if t in f.split(",")))
        assert cand and cand[0][1].strip() == FF.FIX[t][1], (t, cand)
    assert FF.hint("korea_indicators") == "데이터+로그 탭 [기술지표] 버튼(45~90분)"
    assert FF.label("korea_indicators") == "기술지표(korea_indicators)" and FF.hint("nope") == ""


def test_health_messages_carry_fix(monkeypatch):
    import integrated_dashboard_server as d
    monkeypatch.setattr(d, "get_freshness", lambda: [{"table": "korea_indicators", "latest": "2026-10-27", "age_days": 6}])
    monkeypatch.setattr(d, "PAPER_CSV", d.Path(os.path.join(HERE, "__nope__.csv")))
    hs = d.get_health()
    assert hs[0]["msg"] == "기술지표(korea_indicators) 6일 정체(최신 2026-10-27)"
    assert hs[0]["fix"] == {"table": "korea_indicators", "button": "기술지표", "note": "45~90분"}
    assert "신호" not in hs[0]["msg"], "신호 버튼 깜빡임 판정(fillRunButtons)과 섞이지 않게"
    html = _html()
    for s in ('id="fix-list"', "function goFix(i)", "function _fixBtns(f)", ".runbtn.fix-focus", "onclick=\"goFix(${i});return false\""):
        assert s in html, s


def test_watchdog_and_audit_messages_name_the_button(monkeypatch):
    import watchdog as w
    con = sqlite3.connect(":memory:")
    for t, c in (("korea_stocks", "date"), ("supply_demand", "date"), ("korea_indicators", "date"), ("foreign_ratio", "date"),
                 ("usa_stocks", "date"), ("macro_indicators", "date"), ("credit_balance", "date"),
                 ("program_trading", "date"), ("news", "pubDate")):
        con.execute(f"CREATE TABLE {t} ({c} TEXT)")
        con.execute(f"INSERT INTO {t} VALUES ('2020-01-02')")
    monkeypatch.setattr(w, "_open_db", lambda: con)
    out = {k: (lbl, det) for k, lbl, ok, det in w.check_data()}
    lbl, det = out["data_korea_indicators"]
    assert lbl == "데이터 기술지표(korea_indicators)" and "→ 데이터+로그 탭 [기술지표] 버튼(45~90분)" in det
    assert "[수급] 버튼(거래일 08:40~09:25·15:00~15:45 엔 막힘)" in out["data_supply_demand"][1]
    src = open(os.path.join(OUTPUTS, "watchdog.py"), encoding="utf-8").read()
    assert "python -m src.processor.indicators all" not in src.split("def check_indicator_coverage")[1].split("def check_holiday")[0]
    assert "FF.hint(tbl)" in open(os.path.join(OUTPUTS, "daily_audit.py"), encoding="utf-8").read()


# ── B. 수집기: 확정일 컷오프 + 마지막 날 덮어쓰기 ──
@pytest.fixture
def mc(tmp_path, monkeypatch):
    import src.config_db as CDB
    monkeypatch.setattr(CDB, "DB_PATH", str(tmp_path / "stock.db"))
    from src.collector import db as DB
    from src.collector import main_collector as MC
    con = CDB.get_connection()
    DB._ensure_table(con, "korea_stocks")
    DB._ensure_table(con, "usa_stocks")
    rows = [("2026-10-30", "000001", "A", "s", 100, 110, 95, 105, 1000, None, 0.01),        # 확정 일봉
            ("2026-11-02", "000002", "B", "s", 100, 100, 100, 100, 0, None, 0.0),           # 오늘 장중(09:0x)에 들어간 미완성
            ("2026-10-30", "000002", "B", "s", 90, 99, 89, 98, 3000, None, 0.0)]
    con.executemany('INSERT INTO korea_stocks (date,ticker,name,sector,Open,High,Low,Close,Volume,"Adj Close","Change") '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?)', rows)
    con.execute('INSERT INTO usa_stocks (date,ticker,name,sector,Open,High,Low,Close,Volume) VALUES '
                "('2026-11-01','UUU','U','x',1,1,1,1,1)")
    con.commit()
    con.close()
    truth = {"000001": {"2026-10-30": (100, 110, 95, 105, 1000), "2026-11-02": (106, 121, 104, 120, 2000),
                        "2026-11-03": (120, 121, 119, 120, 7)},                    # 11-03 = 장중 값(받으면 안 됨)
             "000002": {"2026-10-30": (90, 99, 89, 98, 3000), "2026-11-02": (101, 135, 99, 130, 5000)},
             "UUU": {"2026-11-02": (2, 2, 2, 2, 2)}}
    calls = []

    def reader(sym, start, end):
        calls.append((sym, start, end))
        d = truth.get(sym, {})
        ks = [k for k in sorted(d) if k >= start]            # 소스가 end 를 무시하고 장중 오늘까지 주는 경우도 흉내
        df = pd.DataFrame([dict(zip(("Open", "High", "Low", "Close", "Volume"), d[k]), Change=0.0) for k in ks],
                          index=pd.DatetimeIndex(pd.to_datetime(ks), name="Date"))
        return df
    monkeypatch.setattr(MC, "fdr", NS(DataReader=reader))
    monkeypatch.setattr(MC, "get_all_tickers", lambda: pd.DataFrame(
        [{"ticker": "000001", "name": "A", "sector": "s", "market": "korea"},
         {"ticker": "000002", "name": "B", "sector": "s", "market": "korea"},
         {"ticker": "UUU", "name": "U", "sector": "x", "market": "usa"}]))
    return MC, CDB, calls


def _rows(CDB, t):
    con = CDB.get_connection()
    try:
        return {r[0]: r[1:] for r in con.execute(
            "SELECT date, Close, Volume FROM korea_stocks WHERE ticker=? ORDER BY date", [t])}
    finally:
        con.close()


def test_korea_cutoff():
    from src.collector.main_collector import korea_cutoff
    assert korea_cutoff(datetime(2026, 11, 2, 6, 30)) == "2026-11-01"
    assert korea_cutoff(datetime(2026, 11, 2, 15, 39)) == "2026-11-01", "장중·마감 직후는 어제까지"
    assert korea_cutoff(datetime(2026, 11, 2, 15, 40)) == "2026-11-02"
    assert korea_cutoff(datetime(2026, 11, 2, 16, 15)) == "2026-11-02", "16:15 기술지표 갱신은 오늘 가격까지"


def test_collect_during_market_never_stores_today(mc):
    MC, CDB, calls = mc
    MC.collect_all(now=datetime(2026, 11, 3, 10, 0))       # 11-03 장중
    a = _rows(CDB, "000001")
    assert "2026-11-03" not in a and a["2026-11-02"] == (120, 2000), "어제(확정)까지만, 장중 오늘은 버림"
    assert ("000001", "2026-10-30", "2026-11-02") in calls, "마지막 저장일부터 하루 겹쳐 받음"


def test_collect_after_close_gets_today_and_heals_partial(mc):
    MC, CDB, calls = mc
    MC.collect_all(now=datetime(2026, 11, 2, 10, 0))
    assert "2026-11-02" not in _rows(CDB, "000001"), "장중엔 오늘 안 받음(종전 버그: 장중 값 저장)"
    assert _rows(CDB, "000002")["2026-11-02"] == (100, 0), "장중엔 아직 못 고침(확정 전)"
    MC.collect_all(now=datetime(2026, 11, 2, 16, 15))
    assert _rows(CDB, "000001")["2026-11-02"] == (120, 2000), "16:15 엔 오늘 확정 일봉을 받음(기술지표 갱신 가능)"
    assert _rows(CDB, "000002")["2026-11-02"] == (130, 5000), "오전에 들어간 미완성 일봉을 덮어써 치유"
    assert not [c for c in calls if c[0] == "UUU"], "미국은 종전대로(오늘 날짜는 안 받음)"
    con = CDB.get_connection()
    assert con.execute("SELECT name FROM korea_stocks WHERE ticker='000002' AND date='2026-11-02'").fetchone()[0] == "B"
    con.close()


def test_save_stock_refresh_only_listed_dates(mc):
    MC, CDB, _ = mc
    from src.collector import db as DB
    df = pd.DataFrame({"date": ["2026-10-30", "2026-11-02"], "ticker": ["000002"] * 2, "name": ["B"] * 2, "sector": ["s"] * 2,
                       "Open": [1, 1], "High": [1, 1], "Low": [1, 1], "Close": [77, 88], "Volume": [9, 9], "Change": [0, 0]})
    assert DB.save_stock(df, "korea") == (0, 0), "덮어쓸 날짜를 안 주면 종전처럼 있는 행은 그대로"
    assert DB.save_stock(df, "korea", refresh_dates=("2026-11-02",)) == (0, 1)
    r = _rows(CDB, "000002")
    assert r["2026-11-02"] == (88, 9) and r["2026-10-30"] == (98, 3000), "지정한 날짜만 덮어씀(과거 행은 수정주가 섞임 방지)"
    assert DB.save_stock(df, "korea", refresh_dates=("2026-11-02",)) == (0, 0), "값이 같으면 안 씀"


# ── C. 지난 미완성 일봉 복구 ──
def test_repair_detects_and_backs_up_before_overwrite(tmp_path, monkeypatch, mc):
    MC, CDB, _ = mc
    from src.collector import repair_partial_bars as R
    md = tmp_path / "macro"
    md.mkdir()
    pd.DataFrame({"code": ["000001", "000002"], "volume": [1000, 3000]}).to_csv(md / "20261030.csv", index=False)
    pd.DataFrame({"code": ["000002"], "volume": [5000]}).to_csv(md / "20261102.csv", index=False)
    con = CDB.get_connection()
    sd = R.suspect_dates(con, "2026-10-01", macro_dir=str(md), min_bad=1, excess=0)
    assert sd == [("2026-11-02", 1, 1)], "거래량이 KRX 확정치의 90% 미만인 종목이 있는 날만"
    monkeypatch.setitem(sys.modules, "FinanceDataReader", NS(DataReader=MC.fdr.DataReader))
    monkeypatch.setattr(R, "FIN", str(tmp_path))
    monkeypatch.setattr(R, "MACRO_DAILY", str(md))
    con.close()
    assert R.main(["--dates", "2026-11-02"]) == 0
    assert _rows(CDB, "000002")["2026-11-02"] == (100, 0), "기본은 점검만"
    monkeypatch.setattr(R, "suspect_dates", lambda *a, **k: [("2026-11-02", 1, 1)])
    assert R.main(["--apply"]) == 0
    assert _rows(CDB, "000002")["2026-11-02"] == (130, 5000)
    day = os.listdir(tmp_path / "_archive")[0]
    man = open(tmp_path / "_archive" / day / "MANIFEST.csv", encoding="utf-8-sig").read()
    assert man.startswith("set,original_relpath,bytes,moved_at") and "korea_stocks_partial_bars" in man
    bk = os.listdir(tmp_path / "_archive" / day / "korea_stocks_partial_bars")
    old = pd.read_csv(tmp_path / "_archive" / day / "korea_stocks_partial_bars" / bk[0], dtype={"ticker": str})
    assert old.iloc[0]["Close"] == 100 and old.iloc[0]["Volume"] == 0, "원래 값(미완성)을 지우지 않고 백업"


def test_repair_skips_adjusted_price_jumps():
    from src.collector import repair_partial_bars as R
    db = pd.DataFrame({"date": ["2026-10-08"] * 3, "ticker": ["a", "b", "c"], "Open": [10] * 3, "High": [10] * 3,
                       "Low": [10] * 3, "Close": [100.0] * 3, "Volume": [5, 5, 500], "Change": [0] * 3})
    fx = pd.DataFrame({"date": ["2026-10-08"] * 3, "ticker": ["a", "b", "c"], "Open": [10] * 3, "High": [12, 10, 10],
                       "Low": [9, 10, 10], "Close": [110.0, 50.0, 101.0], "Volume": [900, 10, 500], "Change": [0] * 3})
    upd, weird = R.plan_updates(db, fx)
    assert list(upd["ticker"]) == ["a"] and weird == 1, "종가 50% 차이(액면분할 등)는 건너뜀"
    assert "c" not in list(upd["ticker"]), "거래량이 같으면(미완성 아님) 값이 조금 달라도 안 건드림"


def test_repair_flags_only_spikes_over_baseline():
    """평소에도 출처 차이로 몇십 종목은 거래량이 다르다(2024년 ~100/일) — 그 무렵 수준보다 튄 날만 의심."""
    from src.collector import repair_partial_bars as R
    rows = [(f"d{i:02d}", 1800, 100) for i in range(30)]
    rows[15] = ("spike", 1800, 1700)
    rows[20] = ("small", 1800, 130)
    assert [r[0] for r in R.spikes(rows)] == ["spike"]
    late = [(f"e{i:02d}", 1800, 3) for i in range(30)]
    late[10] = ("may08", 1800, 90)
    assert [r[0] for r in R.spikes(late)] == ["may08"], "평소 거의 0 인 시기엔 90종목도 튄 날"
