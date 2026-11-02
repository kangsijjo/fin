# -*- coding: utf-8 -*-
"""§96 키움 본계좌(실계좌) 자동매매 — 실제 돈이라 '하면 안 되는 것'을 테스트로 못박는다.

  · 기본은 드라이런: 주문·원장 변경 없이 '냈을 주문'만 기록. 실주문은 모드 live(사람이 켬)일 때만
  · 사용자가 직접 산 종목은 절대 안 건드림: 칸 계산·만기·손절·서킷브레이커·익절 모두 프로그램 몫(원장 수량)만
  · 매수는 현재가 +2% 지정가(호가 올림, 상한가 이하), 주문가능금액은 100% 현금 기준, 미수금 있으면 매수 중단
  · 전송 직전 기록(inflight) — 로그를 못 쓰고 죽어도 재시도가 같은 종목을 또 주문하지 않음
  · 일일 손실 한도 → 신규 매수 정지(매도는 계속), '그날만'은 다음 거래일 자동 해제
  · 모의 프로필은 그대로(회귀 없음)
전부 임시 폴더·가짜 API — 실주문·실파일·네트워크 무접촉.
"""
import importlib
import json
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)

TODAY = datetime.today().strftime("%Y%m%d")
SIG_DATE = "20200102"        # 오늘과 겹치지 않는 신호일(신선도 판정은 테스트에서 끔)


# ── 가짜 키움 API ─────────────────────────────────────────────────────────────
def _item(code, qty, cur, avg, pred=0, name=None):
    return {"stk_cd": "A" + code, "stk_nm": name or code, "rmnd_qty": str(qty), "cur_prc": str(cur),
            "pur_pric": str(avg), "pred_close_pric": str(pred), "evlt_amt": str(qty * cur),
            "evltv_prft": str((cur - avg) * qty), "prft_rt": f"{(cur / avg - 1) * 100:.2f}"}


class FakeApi:
    def __init__(self, pages=None, deposit=10_000_000, uncl=0, quotes=None):
        self.pages = pages or [[]]
        self.deposit, self.uncl, self.quotes = deposit, uncl, quotes or {}
        self.orders = []
        outer = self

        class Acct:
            def deposit_detail_status_request_kt00001(self, qry_tp="3", **kw):
                return {"100stk_ord_alow_amt": f"{outer.deposit:015d}", "ord_alow_amt": f"{outer.deposit * 2:015d}",
                        "entr": f"{outer.deposit:015d}", "ch_uncla_tot": f"{outer.uncl:015d}", "return_code": 0}

            def account_evaluation_balance_detail_request_kt00018(self, query_type, domestic_exchange_type,
                                                                   cont_yn="N", next_key=""):
                i = int(next_key or 0)
                r = {"acnt_evlt_remn_indv_tot": outer.pages[i], "return_code": 0}
                if i + 1 < len(outer.pages):
                    r["cont-yn"], r["next-key"] = "Y", str(i + 1)
                return r

            def unfilled_orders_request_ka10075(self, **kw):
                return {"oso": []}

        class Order:
            def stock_buy_order_request_kt10000(self, **kw):
                outer.orders.append(("buy", kw))
                return {"ord_no": f"B{len(outer.orders)}", "return_code": 0}

            def stock_sell_order_request_kt10001(self, **kw):
                outer.orders.append(("sell", kw))
                return {"ord_no": f"S{len(outer.orders)}", "return_code": 0}

            def stock_cancel_order_request_kt10003(self, **kw):
                outer.orders.append(("cancel", kw))
                return {"return_code": 0}

        class Info:
            def basic_stock_information_request_ka10001(self, stock_code, **kw):
                q = outer.quotes.get(stock_code)
                return ({"cur_prc": f"+{q[0]}", "upl_pric": str(q[1]), "return_code": 0} if q
                        else {"return_code": 1, "return_msg": "조회 실패"})

        self.acct, self.order, self.info = Acct(), Order(), Info()


# ── 본계좌 프로필로 다시 import 한 트레이더(끝나면 모의로 복원) ─────────────────
@pytest.fixture
def real_kt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                       # 트레이더의 ./db/... 상대경로가 임시 폴더로
    monkeypatch.setenv("KIWOOM_PROFILE", "real")
    import real_account as RA
    monkeypatch.setattr(RA, "REAL_DIR", str(tmp_path / "db" / "kiwoom_real"))
    import kill_switch as KS
    monkeypatch.setattr(KS, "status", lambda: (False, None, None))   # 사용자 PC 의 전체 정지 상태와 무관하게
    import kiwoom_trader as kt
    kt = importlib.reload(kt)
    monkeypatch.setattr(kt.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(kt, "_signals_fresh", lambda *_a, **_k: True)
    monkeypatch.setattr(kt, "verify_strength", lambda sigs, strength, account=None: (strength, set()))
    yield kt, RA, tmp_path
    monkeypatch.delenv("KIWOOM_PROFILE", raising=False)
    importlib.reload(kt)                              # 다른 테스트가 쓰는 같은 모듈 객체를 모의로 되돌림


def _signals(tmp, rows):
    d = tmp / "db" / "kiwoom_real"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([dict(signal_date=SIG_DATE, name=r[0], entry_price_close=r[2], holding_days=20, code=r[0],
                       strategy=r[1]) for r in rows]).to_csv(d / "signals.csv", index=False, encoding="utf-8-sig")
    m = tmp / "macro_data" / "daily"
    m.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"code": r[0], "close": r[2], "trading_value": 1e10} for r in rows]) \
        .to_csv(m / f"{SIG_DATE}.csv", index=False, encoding="utf-8-sig")


# ── 순수 함수 ─────────────────────────────────────────────────────────────────
def test_tick_ceil_and_limit_price():
    import kiwoom_trader as kt
    assert kt._tick_ceil(1999.2) == 2000 and kt._tick_ceil(4996) == 5000 and kt._tick_ceil(10001) == 10010
    assert kt._tick_ceil(52_340) == 52_400 and kt._tick_ceil(10_000) == 10_000
    assert kt._real_limit_price(10_000) == 10_200, "현재가 +2% 를 호가 단위로 올림"
    assert kt._real_limit_price(10_000, upper=10_100) == 10_100, "상한가를 넘지 않는다"


def test_day_pnl_and_loss_limit():
    import real_account as RA
    own = {"A": {"qty": 10, "price": 900, "avg_price": 1000, "pred_close": 950},     # 전일종가 대비 -500
           "B": {"qty": 5, "price": 2000, "avg_price": 1900, "pred_close": 0},      # 오늘 산 것 → 매입가 대비 +500
           "C": {"qty": 3, "price": 100, "avg_price": 0, "pred_close": 0}}          # 기준가 없음 → 뺀다
    pnl, n = RA.day_pnl(own, bought_today={"B"})
    assert (pnl, n) == (0, 2)
    pnl, n = RA.day_pnl({"A": {"qty": 10, "price": 900, "avg_price": 1000}}, (), prev_close={"A": 1100})
    assert pnl == -2000, "브로커 전일종가가 없으면 일봉 전일종가"
    assert RA.loss_limit_hit(-100_000, 100_000) and not RA.loss_limit_hit(-99_999, 100_000)
    assert not RA.loss_limit_hit(-10**9, None), "한도 없으면 절대 안 걸림"


def test_mode_defaults_to_dryrun_and_set_mode(tmp_path, monkeypatch, captured_notifications):
    import real_account as RA
    monkeypatch.setattr(RA, "REAL_DIR", str(tmp_path))
    assert RA.get_mode() == "dryrun", "모드 파일이 없으면 드라이런(실주문 안 나감)"
    (tmp_path / "mode.json").write_text("{깨짐", encoding="utf-8")
    assert RA.get_mode() == "dryrun", "깨져도 드라이런"
    with pytest.raises(ValueError):
        RA.set_mode("yolo")
    RA.set_mode("live", note="t")
    assert RA.get_mode() == "live" and any("실주문 켜짐" in m for m in captured_notifications)
    assert RA.mode_history()[0]["mode"] == "live"


def test_halt_day_until_auto_expires(tmp_path, monkeypatch):
    import real_account as RA
    monkeypatch.setattr(RA, "REAL_DIR", str(tmp_path))
    assert RA.halt_engage("loss_limit", "x", until=TODAY, notify=False)
    assert RA.halt_status(TODAY)[0] is True
    nxt = (datetime.today() + timedelta(days=1)).strftime("%Y%m%d")
    assert RA.halt_status(nxt)[0] is False, "'그날만' 정지는 다음 날 자동 해제"
    (tmp_path / "halt.json").write_text("{깨짐", encoding="utf-8")
    assert RA.halt_status()[0] is True, "상태 파일이 깨지면 실제 돈 — 모르면 사지 않는다(정지로 본다)"
    assert RA.halt_release("fix", notify=False) and RA.halt_status()[0] is False


# ── 프로필 ────────────────────────────────────────────────────────────────────
def test_mock_profile_is_unchanged():
    import kiwoom_trader as kt
    assert kt.REAL is False and kt.DRY is False and kt.ORDERS_DIR == "./db/kiwoom"
    assert kt.SIGNALS_CSV == "./paper_signals.csv" and kt.LABEL == "키움 안C"
    pos = {"005930": {"qty": 1}}
    assert kt._own(pos) is pos, "모의는 계좌 전체가 프로그램 몫"


def test_real_profile_constants(real_kt):
    kt, RA, tmp = real_kt
    assert kt.REAL and kt.MODE == "dryrun" and kt.DRY, "모드 파일이 없으면 드라이런"
    assert kt.ORDERS_DIR == "./db/kiwoom_real" and kt.SIGNALS_CSV == "./db/kiwoom_real/signals.csv"
    assert kt.ACCOUNT_KEY == "kiwoom_real" and kt.LABEL == "키움 본계좌" and kt.STRENGTH_ACCOUNT == "real_kiwoom"
    assert os.path.dirname(kt._LOCK_PATH).endswith(os.path.join("db", "kiwoom_real")), "모의·KIS 와 다른 자기 락"
    assert kt.GATE_LOG.startswith("./db/kiwoom_real/")


# ── 매수 ──────────────────────────────────────────────────────────────────────
def test_dryrun_buy_sends_nothing(real_kt):
    kt, RA, tmp = real_kt
    _signals(tmp, [("111111", "high_500d_filt", 10_000), ("222222", "high_52w_filt", 20_000)])
    api = FakeApi(quotes={"111111": (10_000, 13_000), "222222": (20_500, 26_000)})
    kt._API_BUNDLE = api
    kt.cmd_buy()
    assert api.orders == [], "드라이런은 주문을 하나도 보내지 않는다"
    d = tmp / "db" / "kiwoom_real"
    assert not list(d.glob("orders_*.csv")) and not (d / "kiwoom_positions.csv").exists(), "주문로그·원장 변경 없음"
    dr = pd.read_csv(d / f"dryrun_{TODAY}.csv", dtype={"code": str})
    assert set(dr["code"]) == {"111111", "222222"} and set(dr["side"]) == {"buy"}
    assert int(dr[dr["code"] == "111111"]["price"].iloc[0]) == 10_200, "현재가 +2% 지정가로 계산"


def test_live_buy_uses_limit_order_cash_budget_and_records_qty(real_kt):
    kt, RA, tmp = real_kt
    kt.MODE, kt.DRY = "live", False
    _signals(tmp, [("111111", "high_500d_filt", 10_000)])
    api = FakeApi(deposit=10_000_000, quotes={"111111": (10_000, 13_000)})
    kt._API_BUNDLE = api
    kt.cmd_buy()
    assert len(api.orders) == 1
    side, kw = api.orders[0]
    assert side == "buy" and kw["trde_tp"] == kt.ORDER_TYPE_LIMIT and kw["ord_uv"] == "10200", "지정가(시장가 아님)"
    budget = 10_000_000 / 10                                  # 빈 칸 10개 균등
    assert int(kw["ord_qty"]) == int(budget * kt.REAL_BUDGET_PAD // 10_200), "수량은 지정가·100% 현금 기준"
    led = kt.load_kiwoom_positions()
    assert led["111111"]["qty"] == int(kw["ord_qty"]), "본계좌 원장엔 프로그램이 산 수량"
    infl = pd.read_csv(tmp / "db" / "kiwoom_real" / f"inflight_{TODAY}.csv", dtype={"code": str})
    assert list(infl["code"]) == ["111111"], "전송 직전 기록"


def test_real_never_buys_user_holding_and_counts_only_own_slots(real_kt):
    kt, RA, tmp = real_kt
    kt.MODE, kt.DRY = "live", False
    _signals(tmp, [("111111", "high_500d_filt", 10_000), ("222222", "high_500d_filt", 10_000)])
    user = [_item(f"9{i:05d}", 10, 1000, 1000) for i in range(12)] + [_item("222222", 3, 10_000, 9_000)]
    api = FakeApi(pages=[user[:7], user[7:]], quotes={"111111": (10_000, 13_000), "222222": (10_000, 13_000)})
    kt._API_BUNDLE = api
    kt.cmd_buy()
    codes = [kw["stk_cd"] for s, kw in api.orders if s == "buy"]
    assert codes == ["111111"], "직접 보유 13종목은 칸을 차지하지 않고(연속조회로 다 읽음), 직접 보유 종목은 새로 사지 않는다"


def test_strategy_cap_limits_only_that_strategy(real_kt, monkeypatch):
    """§96.6 전략 총액 상한 — 넣은 전략은 (상한 − 이미 산 금액) ÷ 그 전략 남은 빈 칸, 비운 전략은 종전처럼 균등."""
    kt, RA, tmp = real_kt
    monkeypatch.setattr(kt, "STRATEGY_CAP", {"high_500d_filt": 1_000_000})
    kt.save_kiwoom_position("777001", 10_000, "high_500d_filt", SIG_DATE, 20, qty=40)     # 이미 40만원 투입
    _signals(tmp, [("111111", "high_500d_filt", 10_000), ("222222", "high_500d_filt", 10_000),
                   ("333333", "high_52w_filt", 10_000)])
    api = FakeApi(pages=[[_item("777001", 40, 10_000, 10_000)]], deposit=10_000_000,
                  quotes={c: (10_000, 13_000) for c in ("111111", "222222", "333333")})
    kt._API_BUNDLE = api
    kt.cmd_buy()                                              # 드라이런
    dr = pd.read_csv(tmp / "db" / "kiwoom_real" / f"dryrun_{TODAY}.csv", dtype={"code": str}).set_index("code")
    q1 = int(600_000 / 5 * kt.REAL_BUDGET_PAD // 10_200)       # 남은 60만 ÷ 이 전략 남은 칸 5
    assert int(dr.loc["111111", "qty"]) == q1
    spent = q1 * 10_200
    assert int(dr.loc["222222", "qty"]) == int((600_000 - spent) / 4 * kt.REAL_BUDGET_PAD // 10_200)
    assert int(dr.loc["333333", "qty"]) > 5 * q1, "상한 없는 전략은 주문가능금액 ÷ 남은 빈 칸(균등) 그대로"
    assert sum(int(dr.loc[c, "qty"]) * 10_200 for c in ("111111", "222222")) + 400_000 <= 1_000_000


def test_strategy_cap_reached_skips(real_kt, monkeypatch):
    kt, RA, tmp = real_kt
    monkeypatch.setattr(kt, "STRATEGY_CAP", {"high_500d_filt": 300_000})
    kt.save_kiwoom_position("777001", 10_000, "high_500d_filt", SIG_DATE, 20, qty=40)
    _signals(tmp, [("111111", "high_500d_filt", 10_000)])
    kt._API_BUNDLE = FakeApi(pages=[[_item("777001", 40, 10_000, 10_000)]], quotes={"111111": (10_000, 13_000)})
    kt.cmd_buy()
    assert not (tmp / "db" / "kiwoom_real" / f"dryrun_{TODAY}.csv").exists(), "상한(30만) 이미 넘음(40만) → 이 전략은 안 삼"


def test_live_config_strategy_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    import live_config as LC
    c = LC.load()
    R = dict(c["accounts"]["kiwoom_real"])
    R.pop("_mirror")
    R["strategies"] = [dict(s) for s in R["strategies"]]
    R["strategies"][0]["cap"] = 3_000_000
    c["accounts"]["kiwoom_real"] = R
    cfg, ch = LC.save(c)
    assert cfg["accounts"]["kiwoom_real"]["strategies"][0]["cap"] == 3_000_000
    assert LC.account("kiwoom_real")["cap"] == {LC.live_name(R["strategies"][0]["engine"]): 3_000_000}
    c2 = LC.load()
    c2["accounts"]["kiwoom_real"]["strategies"][0]["cap"] = 5_000_000
    assert any("금액 상한 3,000,000원→5,000,000원" in x for x in LC.diff(LC.load(), c2))
    bad = LC.load()
    bad["accounts"]["kiwoom"]["strategies"][0]["cap"] = 1_000_000
    assert LC.validate(bad), "모의계좌엔 금액 상한 없음"
    bad = LC.load()
    bad["accounts"]["kiwoom_real"]["strategies"][0]["cap"] = 50_000
    assert LC.validate(bad), "10만원 미만은 거부"
    assert LC.account("kiwoom")["cap"] == {}


def test_real_cash_basis_and_uncl_blocks_buy(real_kt, captured_notifications):
    kt, RA, tmp = real_kt
    kt.MODE, kt.DRY = "live", False
    api = FakeApi(deposit=5_000_000, uncl=12_345, quotes={"111111": (10_000, 13_000)})
    assert kt.get_deposit(api) == 5_000_000, "ord_alow_amt(증거금률 따라 현금보다 큼)가 아니라 100% 현금"
    _signals(tmp, [("111111", "high_500d_filt", 10_000)])
    kt._API_BUNDLE = api
    kt.cmd_buy()
    assert api.orders == [] and any("미수금" in m for m in captured_notifications), "미수금이 있으면 매수 중단 + 알림"


def test_inflight_record_prevents_duplicate_after_crash(real_kt):
    kt, RA, tmp = real_kt
    kt.MODE, kt.DRY = "live", False
    _signals(tmp, [("111111", "high_500d_filt", 10_000)])
    d = tmp / "db" / "kiwoom_real"
    (d / f"inflight_{TODAY}.csv").write_text(f"time,side,code,qty,price\n09:00:01,buy,111111,90,10200\n",
                                             encoding="utf-8-sig")   # 보낸 뒤 로그 쓰다 죽은 흔적
    api = FakeApi(quotes={"111111": (10_000, 13_000)})
    kt._API_BUNDLE = api
    kt.cmd_buy()
    assert api.orders == [], "재시도가 같은 종목을 또 주문하면 안 된다"


def test_loss_limit_engages_halt_and_blocks_buy_not_sell(real_kt, monkeypatch, captured_notifications):
    kt, RA, tmp = real_kt
    kt.MODE, kt.DRY = "live", False
    monkeypatch.setitem(kt._ACC, "daily_loss_limit", 100_000)
    monkeypatch.setitem(kt._ACC, "loss_action", "halt")
    kt.save_kiwoom_position("333333", 10_000, "high_500d_filt", SIG_DATE, 20, qty=100)
    api = FakeApi(pages=[[_item("333333", 100, 9_000, 10_000, pred=10_000)]],
                  quotes={"111111": (10_000, 13_000)})
    kt._API_BUNDLE = api
    kt._real_loss_check()                                     # -100,000원 = 한도
    assert RA.halt_status()[0] is True and any("신규 매수 정지" in m for m in captured_notifications)
    _signals(tmp, [("111111", "high_500d_filt", 10_000)])
    kt.cmd_buy()
    assert not [o for o in api.orders if o[0] == "buy"], "정지 중엔 새로 사지 않는다"
    lc = json.loads((tmp / "db" / "kiwoom_real" / "loss_check.json").read_text(encoding="utf-8"))
    assert lc["pnl"] == -100_000 and lc["hit"] is True


# ── 매도·원장 ─────────────────────────────────────────────────────────────────
def test_sell_only_program_qty_never_user_holdings(real_kt, monkeypatch):
    kt, RA, tmp = real_kt
    kt.MODE, kt.DRY = "live", False
    kt.save_kiwoom_position("444444", 1_000, "high_500d_filt", SIG_DATE, 20, qty=5)
    api = FakeApi(pages=[[_item("444444", 8, 1_000, 950),          # 프로그램 5주 + 사용자가 3주 더 삼
                          _item("555555", 100, 500, 1_000)]])       # 사용자 종목 -50%(모의라면 서킷브레이커)
    kt._API_BUNDLE = api
    monkeypatch.setattr(kt, "codes_due_for_exit", lambda: {"444444", "555555"})   # 옛 신호가 있어도
    kt.cmd_sell()
    sells = [(kw["stk_cd"], kw["ord_qty"]) for s, kw in api.orders if s == "sell"]
    assert sells == [("444444", "5")], "프로그램이 산 5주만 — 사용자 종목·추가분은 절대 안 판다"
    assert "444444" not in kt.load_kiwoom_positions()


def test_real_due_has_no_signal_csv_fallback(real_kt, monkeypatch):
    kt, RA, tmp = real_kt
    import strategies.daily_loader as dl
    monkeypatch.setitem(sys.modules, "strategies.daily_loader", dl)
    monkeypatch.setattr(dl, "load_macro_daily", lambda *a, **k: pd.DataFrame(
        [{"code": c, "date": d} for c in ("666666",) for d in ("20200102", "20200103", "20200106")]))
    _signals(tmp, [("666666", "high_500d_filt", 1_000)])            # 만기 한참 지난 옛 신호(원장엔 없음)
    assert "666666" not in kt.codes_due_for_exit(), "원장에 없는 종목 = 사용자 종목일 수 있어 신호로 팔지 않는다"


def test_real_ledger_never_adopts_and_restores_only_today(real_kt, captured_notifications):
    kt, RA, tmp = real_kt
    d = tmp / "db" / "kiwoom_real"
    d.mkdir(parents=True, exist_ok=True)
    row = {"time": "09:00", "side": "buy", "strategy": "high_500d_filt", "price": 1000, "order_type": "0",
           "ok": True, "order_no": "B1", "msg": ""}
    pd.DataFrame([dict(row, code="777777", name="R", qty=7)]).to_csv(d / f"orders_{TODAY}.csv", index=False,
                                                                      encoding="utf-8-sig")
    pd.DataFrame([dict(row, code="888888", name="O", qty=5)]).to_csv(d / "orders_20200102.csv", index=False,
                                                                      encoding="utf-8-sig")
    kt.ensure_kiwoom_ledger({"777777": {"qty": 9, "avg_price": 1010, "name": "R"},    # 오늘 산 기록 → 복구(7주만)
                             "888888": {"qty": 5, "avg_price": 990, "name": "O"},     # 옛 기록 → 알리기만
                             "999999": {"qty": 3, "avg_price": 500, "name": "U"}})    # 사용자 종목 → 절대 안 끌어옴
    led = kt.load_kiwoom_positions()
    assert set(led) == {"777777"} and led["777777"]["qty"] == 7
    assert any("888888" in m for m in captured_notifications)


def test_real_ledger_qty_reconcile_and_prune(real_kt):
    kt, RA, tmp = real_kt
    kt.save_kiwoom_position("121212", 1000, "high_500d_filt", SIG_DATE, 20, qty=10)
    kt.save_kiwoom_position("343434", 1000, "high_500d_filt", SIG_DATE, 20, qty=4)
    kt.ensure_kiwoom_ledger({"121212": {"qty": 6, "avg_price": 1000, "name": "A"},    # 일부를 직접 매도
                             "565656": {"qty": 1, "avg_price": 1, "name": "U"}})
    led = kt.load_kiwoom_positions()
    assert led["121212"]["qty"] == 6, "원장 수량을 잔고에 맞춘다"
    assert "343434" not in led, "잔고에 없는 원장 종목은 정리"


def test_real_ledger_empty_account_needs_two_sightings(real_kt, captured_notifications):
    kt, RA, tmp = real_kt
    kt.save_kiwoom_position("121212", 1000, "high_500d_filt", SIG_DATE, 20, qty=10)
    kt.ensure_kiwoom_ledger({})
    assert "121212" in kt.load_kiwoom_positions(), "잔고가 처음 비어 보인 건 글리치일 수 있어 보존"
    RA.write_json("empty_seen.json", {"first": (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")})
    kt.ensure_kiwoom_ledger({})
    assert kt.load_kiwoom_positions() == {}, "1시간 넘게 계속 비면(직접 전부 매도) 정리 — 매수 중단이 영영 안 풀리는 일 방지"
    assert any("정리했습니다" in m for m in captured_notifications)


def test_went_live_today_after_open(real_kt):
    kt, RA, tmp = real_kt
    d = datetime.now().strftime("%Y-%m-%d")
    RA.write_json("mode.json", {"mode": "live", "changed_at": d + " 10:00:00"})
    assert kt._went_live_today_after_open(), "장중에 켠 날은 오후 보조 매수(마감에 칸 몰아 채우기)를 건너뛴다"
    RA.write_json("mode.json", {"mode": "live", "changed_at": d + " 08:10:00"})
    assert not kt._went_live_today_after_open(), "장 전에 켰으면 그날 정상"
    RA.write_json("mode.json", {"mode": "live", "changed_at": "2026-01-01 10:00:00"})
    assert not kt._went_live_today_after_open(now=datetime(2026, 1, 2, 15, 21))
    src = open(os.path.join(OUTPUTS, "kiwoom_trader.py"), encoding="utf-8").read()
    assert "if REAL and not DRY and _went_live_today_after_open():" in src


def test_order_retries_once_on_token_error_only(real_kt):
    kt, RA, tmp = real_kt

    class TM:
        _access_token = "old"
    tm = TM()
    kt._API_BUNDLE = type("B", (), {"order": type("O", (), {"token_manager": tm})()})()
    calls = []

    def fn():
        calls.append(1)
        return ({"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"} if len(calls) == 1
                else {"return_code": 0, "ord_no": "0001"})
    assert kt._order_with_retry(fn, "매수")["ord_no"] == "0001" and len(calls) == 2
    assert tm._access_token is None, "토큰을 새로 받게 비움(같은 키를 다른 프로그램이 재발급하면 이전 토큰이 죽음)"
    calls.clear()
    with pytest.raises(RuntimeError):
        kt._order_with_retry(lambda: (calls.append(1), {"return_code": 2, "return_msg": "증거금 부족"})[1], "매수")
    assert len(calls) == 1, "인증 실패가 아닌 거부는 재시도하지 않는다(중복 주문 위험)"


def test_real_positions_unknown_response_raises(real_kt):
    kt, RA, tmp = real_kt

    class A:
        def account_evaluation_balance_detail_request_kt00018(self, **kw):
            return {"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"}
    with pytest.raises(RuntimeError):
        kt.get_positions(type("X", (), {"acct": A()})())     # 본계좌는 '빈 잔고'로 착각하지 않는다
    with pytest.raises(RuntimeError):
        kt._real_orderable({"return_code": 3, "return_msg": "8005"})


def test_get_positions_reads_all_pages():
    import kiwoom_trader as kt
    api = FakeApi(pages=[[_item("000001", 1, 10, 10)], [_item("000002", 2, 10, 10)], [_item("000003", 3, 10, 10)]])
    assert set(kt.get_positions(api)) == {"000001", "000002", "000003"}, "연속조회로 잘린 뒤쪽 종목도 읽는다"


# ── 설정·신호·대시보드·운영 파일 ──────────────────────────────────────────────
def test_live_config_real_mirror_until_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_CONFIG_PATH", str(tmp_path / "lc.json"))
    import live_config as LC
    assert LC.account("kiwoom_real")["mirror"] is True and "kiwoom_real" not in LC.ACCOUNTS
    new = LC.default_config()
    new["accounts"]["kiwoom"]["min_strength"] = 6.0
    cfg, ch = LC.save(new)
    assert "kiwoom_real" not in cfg["accounts"] and LC.account("kiwoom_real")["min_strength"] == 6.0, "저장 전엔 안C 를 따름"
    n2 = LC.load()
    R = dict(n2["accounts"]["kiwoom_real"])
    R.pop("_mirror")
    R.update(daily_loss_limit=300_000, loss_action="day")
    n2["accounts"]["kiwoom_real"] = R
    cfg, ch = LC.save(n2)
    assert any("본계좌 전용 설정으로 따로" in c for c in ch) and any("300,000원" in c for c in ch)
    a = LC.account("kiwoom_real")
    assert a["daily_loss_limit"] == 300_000 and a["loss_action"] == "day" and a["mirror"] is False
    bad = LC.load()
    bad["accounts"]["kiwoom_real"]["daily_loss_limit"] = 5
    assert LC.validate(bad), "한도 범위 밖은 거부"


def test_live_signal_real_signals_reuse_mock_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import live_signal as ls
    import real_account as RA
    import engine_signals as ES
    import strength_logger
    monkeypatch.setattr(RA, "REAL_DIR", str(tmp_path / "rd"))
    logged = []
    monkeypatch.setattr(strength_logger, "log_strength", lambda acc, df, ld, rows, **k: logged.append((acc, len(rows))))
    monkeypatch.setattr(ES, "_drop_ca_rows", lambda rows, *a, **k: rows)
    row = {"signal_date": SIG_DATE, "code": "123456", "name": "X", "entry_price_close": 1000.0, "target_exit_date": "",
           "lookback_high": 0.0, "market_strong": True, "strategy": "high_500d_filt", "holding_days": 20}
    groups = [("high_500d_filt", [row]), ("high_52w_filt", [])]
    ls._real_account_signals(pd.DataFrame(), SIG_DATE, [SIG_DATE], {}, {}, groups)
    s = pd.read_csv(tmp_path / "db" / "kiwoom_real" / "signals.csv", dtype={"code": str})
    assert list(s["code"]) == ["123456"], "모의에서 만든 같은 전략 신호를 그대로 씀(다시 계산 안 함)"
    assert logged == [("real_kiwoom", 1)], "강도는 본계좌 계좌명으로 따로 기록"
    ls._real_account_signals(pd.DataFrame(), SIG_DATE, [SIG_DATE], {}, {}, groups)
    assert len(pd.read_csv(tmp_path / "db" / "kiwoom_real" / "signals.csv")) == 1, "같은 신호는 한 번만"


def test_dashboard_real_switch_guards(tmp_path, monkeypatch):
    sys.argv = [sys.argv[0]]
    import integrated_dashboard_server as srv
    import real_account as RA
    from pathlib import Path
    monkeypatch.setattr(RA, "REAL_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "REAL_DIR", Path(tmp_path))
    c = srv.app.test_client()
    H, L = {"X-Dashboard-Action": "1"}, {"REMOTE_ADDR": "127.0.0.1"}
    assert c.post("/api/real/mode", json={"mode": "live", "confirm": 2}, environ_base=L).status_code == 403, "CSRF"
    assert c.post("/api/real/mode", json={"mode": "live", "confirm": 1}, headers=H, environ_base=L).status_code == 400
    assert c.post("/api/real/mode", json={"mode": "live", "confirm": 2}, headers=H, environ_base=L).status_code == 409, \
        "본계좌 연결(스냅샷) 확인 전엔 실주문을 켤 수 없다"
    (tmp_path / "snapshot.json").write_text(json.dumps({"profile": "real", "deposit": 1}), encoding="utf-8")
    assert c.post("/api/real/mode", json={"mode": "live", "confirm": 2}, headers=H, environ_base=L).get_json()["ok"]
    assert RA.get_mode() == "live"
    assert c.get("/api/real").get_json()["ok"]
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert '_RUN_ENV = {"real_status": {"KIWOOM_PROFILE": "real"}}' in src and 'if k != "KIWOOM_PROFILE"' in src, \
        "본계좌 프로필은 잔고 조회 작업에만 — 다른 수동 작업으로 새면 안 된다"
    assert "real_status" in srv._BROKER_TASKS and "market_grid" in srv._BROKER_TASKS
    assert "real_buy" not in srv._RUN_TASKS and "real_sell" not in srv._RUN_TASKS, "본계좌 수동 매수·매도 버튼은 만들지 않는다"


def test_ops_files_wired():
    import fnmatch
    bat = open(os.path.join(OUTPUTS, "run_kiwoom_real.bat"), "rb").read()
    assert b"\r\n" in bat and all(c < 128 for c in bat), "bat 는 ASCII + CRLF"
    t = bat.decode("ascii")
    assert 'set "KIWOOM_PROFILE=real"' in t and 'set "CMD=status"' in t, "인자 없이 더블클릭하면 잔고 조회만"
    assert "real_kiwoom_!TODAY!.log" in t and "Forfiles" not in t
    assert not fnmatch.fnmatch("real_kiwoom_20261103.log", "kiwoom_*.log"), "워치독의 모의 로그 글롭과 안 겹침"
    ps = open(os.path.join(OUTPUTS, "register_tasks.ps1"), encoding="utf-8").read()
    for n in ("KiwoomRealAM", "KiwoomRealPM", "KiwoomRealStatus"):
        assert f"StockAI\\{n}" in ps
    wd = open(os.path.join(OUTPUTS, "watchdog.py"), encoding="utf-8").read()
    assert "real_kiwoom_{TODAY_STR}.log" in wd and "def _real_active" in wd
