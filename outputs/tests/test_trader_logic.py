"""
트레이더 핵심 로직 단위테스트 — 2026-07-02~05 세션에서 실버그를 잡았던 임시 테스트의 영구화.

배경: 기존 test_feature_contract 는 '데이터 계약'만 커버 — 만기청산 날짜비교·매수 상한·
익절 헬퍼 같은 트레이더 로직 버그는 전부 그 밖이었고, 아래 테스트들의 임시판이 잡았다.
(잡은 버그: _glob 미정의 조용한 실패, 익절 전략 오배정, 매도 price=0, tp 체결 중복기록)

전부 임시 디렉토리/합성 데이터 사용 — 실주문·실파일 무접촉, 네트워크 불필요.
실행: cd C:/fin/outputs ; python -m pytest tests -q
"""
import os
import sys
from datetime import datetime

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)


# ─────────────────────────────────────────────────────────────────────────────
# 호가단위(_tick_floor) — 익절 지정가 유효가격
# ─────────────────────────────────────────────────────────────────────────────

def test_tick_floor_krx_bands():
    import kiwoom_trader as kt
    cases = [  # (원시가, 기대 tick, 기대 결과)
        (1999,          1, 1999),
        (2481 * 1.2,    5, 2975),
        (7450 * 1.2,   10, 8940),
        (32400 * 1.5,  50, 48600),
        (52600 * 1.5, 100, 78900),
        (250000 * 1.2, 500, 300000),
    ]
    for raw, tick, want in cases:
        got = kt._tick_floor(raw)
        assert got == want and got % tick == 0, (raw, got, want)


# ─────────────────────────────────────────────────────────────────────────────
# 익절 지정가 헬퍼 3종 — 임시 ORDERS_DIR 로 격리
#   (실제로 _glob 미정의 조용한 실패·전략 오배정을 잡아낸 테스트)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def kt_tmp_orders(tmp_path, monkeypatch):
    import kiwoom_trader as kt
    monkeypatch.setattr(kt, "ORDERS_DIR", str(tmp_path))
    today = datetime.today().strftime("%Y%m%d")
    pd.DataFrame([
        {"time": "09:03", "side": "buy", "code": "105330", "name": "KNW",
         "strategy": "rsi_vol", "qty": 247, "price": 6888,
         "order_type": "3", "ok": True, "order_no": "B1", "msg": ""},
        {"time": "09:03", "side": "tp_sell", "code": "105330", "name": "KNW",
         "strategy": "rsi_vol", "qty": 247, "price": 8260,
         "order_type": "0", "ok": True, "order_no": "T1", "msg": "target+20%"},
        {"time": "09:04", "side": "tp_sell", "code": "091590", "name": "NHT",
         "strategy": "high_52w_filt", "qty": 160, "price": 10840,
         "order_type": "0", "ok": False, "order_no": "", "msg": "err"},
    ]).to_csv(tmp_path / f"orders_{today}.csv", index=False, encoding="utf-8-sig")
    return kt, tmp_path, today


def test_today_tp_orders_filters_ok(kt_tmp_orders):
    kt, _, _ = kt_tmp_orders
    assert kt._today_tp_orders() == {"105330": "T1"}   # ok=False 는 제외


def test_held_strategy_map_uses_buy_time_strategy(kt_tmp_orders):
    """익절 목표%는 '매수 당시' 전략으로 — 최신 신호 기준이면 오배정(+20%↔+50%)."""
    kt, _, _ = kt_tmp_orders
    assert kt._held_strategy_map().get("105330") == "rsi_vol"


def test_reconcile_tp_fill_synthetic_sell_and_idempotent(kt_tmp_orders):
    """발주됐는데 미보유 = 체결 → 합성 sell 1회만 기록(재실행 멱등)."""
    kt, tmp_path, today = kt_tmp_orders
    kt._reconcile_tp_fills(pos={})                     # 105330 미보유 → 체결 처리
    assert "105330" in kt.today_ordered_codes("sell")
    df = pd.read_csv(tmp_path / f"orders_{today}.csv", dtype=str)
    row = df[(df["side"] == "sell") & (df["code"] == "105330")]
    assert len(row) == 1
    assert row.iloc[0]["price"] == "8260"
    assert "profit_target" in row.iloc[0]["msg"]
    kt._reconcile_tp_fills(pos={})                     # 멱등 — 중복 기록 없음
    df2 = pd.read_csv(tmp_path / f"orders_{today}.csv", dtype=str)
    assert len(df2[(df2["side"] == "sell") & (df2["code"] == "105330")]) == 1


def test_reconcile_tp_holding_not_filled(kt_tmp_orders):
    """아직 보유 중이면 체결 아님(합성 sell 금지). ok=False 발주도 체결 처리 금지."""
    kt, tmp_path, today = kt_tmp_orders
    kt._reconcile_tp_fills(pos={"105330": {"qty": 247, "name": "KNW"}})
    df = pd.read_csv(tmp_path / f"orders_{today}.csv", dtype=str)
    assert not len(df[df["side"] == "sell"])


# ─────────────────────────────────────────────────────────────────────────────
# 대시보드 실현손익 FIFO(_attach_realized_pnl) — 매도 price=0 종가보정 포함
# ─────────────────────────────────────────────────────────────────────────────

def _srv():
    sys.argv = [sys.argv[0]]   # 서버 모듈이 sys.argv[1] 을 PORT 로 파싱 → 차단
    import integrated_dashboard_server as srv
    return srv


# ─────────────────────────────────────────────────────────────────────────────
# 매수 후보 배정순서 — 강도(score_ic) 내림차순, 무기록은 tv순 폴백 (2026-07-06 역선택 수정)
# ─────────────────────────────────────────────────────────────────────────────

def test_order_candidates_strength_first_tv_fallback():
    import kiwoom_trader as kt
    sigs = [
        {"code": "000001", "strategy": "rsi_vol"},   # 강도 5.0
        {"code": "000002", "strategy": "rsi_vol"},   # 강도 없음, tv 최고
        {"code": "000003", "strategy": "rsi_vol"},   # 강도 6.5 (최강)
    ]
    strength = {("000001", "rsi_vol"): 5.0, ("000003", "rsi_vol"): 6.5}
    tv = {"000001": 1e9, "000002": 9e9, "000003": 2e9}
    out = [s["code"] for s in kt._order_candidates(sigs, strength, tv, "rsi_vol")]
    assert out == ["000003", "000001", "000002"], out   # 강도순 → 무기록은 맨 뒤

    # 강도 기록이 아예 없으면 기존 tv 내림차순으로 완전 폴백
    out2 = [s["code"] for s in kt._order_candidates(sigs, {}, tv, "rsi_vol")]
    assert out2 == ["000002", "000003", "000001"], out2


# ─────────────────────────────────────────────────────────────────────────────
# 키움 진입가 원장 — 저장/로드/삭제/자가치유 (2026-07-06 신설)
# ─────────────────────────────────────────────────────────────────────────────

def test_kiwoom_ledger_roundtrip_and_heal(tmp_path, monkeypatch):
    import kiwoom_trader as kt
    monkeypatch.setattr(kt, "ORDERS_DIR", str(tmp_path))
    monkeypatch.setattr(kt, "KIWOOM_POSITIONS_CSV", str(tmp_path / "kiwoom_positions.csv"))
    monkeypatch.setattr(kt, "SIGNALS_CSV", str(tmp_path / "paper_signals.csv"))  # 실파일 미접촉

    kt.save_kiwoom_position("105330", 6888, "rsi_vol", "20260702", 7)
    led = kt.load_kiwoom_positions()
    assert led["105330"]["entry_px"] == 6888 and led["105330"]["strategy"] == "rsi_vol"

    kt.remove_kiwoom_position("105330")
    assert kt.load_kiwoom_positions() == {}

    # 자가치유: 브로커 보유(매입평균 7,000)가 원장에 없으면 주문로그 전략으로 등록
    pd.DataFrame([{"time": "09:03", "side": "buy", "code": "091590", "name": "NHT",
                   "strategy": "high_52w_filt", "qty": 160, "price": 7230,
                   "order_type": "3", "ok": True, "order_no": "B9", "msg": ""}]) \
        .to_csv(tmp_path / "orders_20260706.csv", index=False, encoding="utf-8-sig")
    kt.ensure_kiwoom_ledger({"091590": {"qty": 160, "name": "NHT", "avg_price": 7000}})
    led = kt.load_kiwoom_positions()
    assert led["091590"]["entry_px"] == 7000          # 브로커 매입평균 우선
    assert led["091590"]["strategy"] == "high_52w_filt"
    assert led["091590"]["holding_days"] == 20        # 전략별 보유일(_HEAL_HOLDING)


# ─────────────────────────────────────────────────────────────────────────────
# 원장 좀비행 방지 3종 (2026-07-10: 부활 금지 / prune / float 승격 차단)
# ─────────────────────────────────────────────────────────────────────────────

def test_ensure_no_resurrect_sold_today_and_prune(tmp_path, monkeypatch):
    """15:21 매도(미체결) 직후 status: ① 오늘 판 코드는 재등록 금지
    ② 원장에만 남은 좀비 행은 prune ③ 오늘 매수분은 잔고 미반영이어도 보존."""
    from datetime import datetime as _dt
    import kiwoom_trader as kt
    monkeypatch.setattr(kt, "ORDERS_DIR", str(tmp_path))
    monkeypatch.setattr(kt, "KIWOOM_POSITIONS_CSV", str(tmp_path / "kiwoom_positions.csv"))
    monkeypatch.setattr(kt, "SIGNALS_CSV", str(tmp_path / "paper_signals.csv"))

    today = _dt.today().strftime("%Y%m%d")
    # 오늘 주문로그: 111111 매도(만기), 222222 매수
    pd.DataFrame([
        {"time": "15:21", "side": "sell", "code": "111111", "name": "A",
         "strategy": "rsi_vol", "qty": 10, "price": 1000, "order_type": "3",
         "ok": True, "order_no": "S1", "msg": ""},
        {"time": "09:03", "side": "buy", "code": "222222", "name": "B",
         "strategy": "rsi_reversal", "qty": 5, "price": 2000, "order_type": "3",
         "ok": True, "order_no": "B2", "msg": ""},
    ]).to_csv(tmp_path / f"orders_{today}.csv", index=False, encoding="utf-8-sig")

    # 원장: 222222(오늘 매수, 잔고 미반영) + 333333(좀비 — 브로커 미보유)
    kt.save_kiwoom_position("222222", 2000, "rsi_reversal", "20260707", 5)
    kt.save_kiwoom_position("333333", 500, "rsi_vol", "20260601", 7)

    # 브로커 잔고: 111111(방금 매도주문, 15:30 체결 전이라 아직 보임) + 444444(정상 보유)
    kt.ensure_kiwoom_ledger({
        "111111": {"qty": 10, "name": "A", "avg_price": 990},
        "444444": {"qty": 3, "name": "C", "avg_price": 3000},
    })
    led = kt.load_kiwoom_positions()
    assert "111111" not in led          # ① 오늘 매도분 부활 금지 (105330 실사례)
    assert "333333" not in led          # ② 좀비 행 prune
    assert "222222" in led              # ③ 오늘 매수분은 잔고 미반영이어도 보존
    assert "444444" in led              # 정상 보유는 자가치유 등록


def test_ensure_heal_backtracks_signal_date_and_holding(tmp_path, monkeypatch):
    """치유 시 signal_date 를 신호CSV에서 역추적(매수일이 아니라), holding 은 전략별."""
    from datetime import datetime as _dt
    import kiwoom_trader as kt
    monkeypatch.setattr(kt, "ORDERS_DIR", str(tmp_path))
    monkeypatch.setattr(kt, "KIWOOM_POSITIONS_CSV", str(tmp_path / "kiwoom_positions.csv"))
    monkeypatch.setattr(kt, "SIGNALS_CSV", str(tmp_path / "paper_signals.csv"))

    pd.DataFrame([
        {"signal_date": "20260701", "code": "555555", "strategy": "rsi_vol", "holding_days": 7},
        {"signal_date": "20260620", "code": "555555", "strategy": "rsi_vol", "holding_days": 7},
    ]).to_csv(tmp_path / "paper_signals.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"time": "09:03", "side": "buy", "code": "555555", "name": "D",
                   "strategy": "rsi_vol", "qty": 10, "price": 5000, "order_type": "3",
                   "ok": True, "order_no": "B5", "msg": ""}]) \
        .to_csv(tmp_path / "orders_20260702.csv", index=False, encoding="utf-8-sig")

    kt.ensure_kiwoom_ledger({"555555": {"qty": 10, "name": "D", "avg_price": 5100}})
    led = kt.load_kiwoom_positions()
    assert led["555555"]["signal_date"] == "20260701"   # 매수일(0702) 직전 신호일 역추적
    assert led["555555"]["holding_days"] == 7            # rsi_vol 전략별 보유일


def test_ledger_loader_survives_empty_signal_date(tmp_path, monkeypatch):
    """한 행의 signal_date 가 비어도 다른 행이 'YYYYMMDD.0' 으로 오염되지 않는다
    (pandas float 승격 → 원장 만기판정 전체 무력화 방지)."""
    import kiwoom_trader as kt
    p = tmp_path / "kiwoom_positions.csv"
    monkeypatch.setattr(kt, "KIWOOM_POSITIONS_CSV", str(p))
    p.write_text("code,entry_px,strategy,signal_date,holding_days\n"
                 "067290,3000.0,high_52w_filt,20260623,20\n"
                 "999999,1000.0,rsi_vol,,7\n", encoding="utf-8-sig")
    led = kt.load_kiwoom_positions()
    assert led["067290"]["signal_date"] == "20260623"   # '.0' 오염 없음
    assert led["999999"]["signal_date"] == ""
    assert led["067290"]["holding_days"] == 20 and led["999999"]["holding_days"] == 7


# ─────────────────────────────────────────────────────────────────────────────
# 만기 판정 — 원장 우선 (2026-07-07: 옛 신호 만기가 최근 매수분을 조기청산하던
# CJ ENM 사례의 일반형 수정. 원장에 없는 코드만 신호CSV 폴백)
# ─────────────────────────────────────────────────────────────────────────────

def _synth_macro_df(codes, dates):
    rows = [{"code": c, "date": d} for c in codes for d in dates]
    return pd.DataFrame(rows)


def test_expiry_due_helper():
    import kiwoom_trader as kt
    ds = ["20260102", "20260103", "20260106", "20260107", "20260108"]
    today = "20260108"
    # 신호 20260102, 보유 3일 → 진입 0103, 청산 0107(진입일 포함 3영업일째) ≤ today
    assert kt._expiry_due(ds, "20260102", 3, today) is True
    # 신호 20260106, 보유 3일 → 청산 인덱스가 달력 밖 → 미도래
    assert kt._expiry_due(ds, "20260106", 3, today) is False
    # 달력에 없는 신호일 → 판정 불가(None)
    assert kt._expiry_due(ds, "20251231", 3, today) is None
    assert kt._expiry_due(None, "20260102", 3, today) is None


def test_codes_due_ledger_priority_over_stale_signal(tmp_path, monkeypatch):
    """원장(최근 매수)이 있으면 같은 코드의 '만기 지난 옛 신호'가 due 를 오염시키지 않는다."""
    import kiwoom_trader as kt
    import strategies.daily_loader as dl

    dates = ["20260102", "20260103", "20260106", "20260107", "20260108",
             "20260109", "20260112", "20260113"]
    # sys.modules 고정 — 트레이더는 함수 내 `from strategies.daily_loader import ...` 로
    # 호출 시점에 sys.modules 를 조회한다. 앞선 테스트가 이 엔트리를 교체해 두면 아래
    # setattr 패치가 무력화됨(2026-07-17 11:02 preflight 순서-의존 실패 실사례). 고정으로 방탄.
    monkeypatch.setitem(sys.modules, "strategies.daily_loader", dl)
    monkeypatch.setattr(dl, "load_macro_daily",
                        lambda *a, **k: _synth_macro_df(["000030", "000040", "000050"], dates))
    monkeypatch.setattr(kt, "SIGNALS_CSV", str(tmp_path / "paper_signals.csv"))
    monkeypatch.setattr(kt, "KIWOOM_POSITIONS_CSV", str(tmp_path / "kiwoom_positions.csv"))
    monkeypatch.setattr(kt, "ORDERS_DIR", str(tmp_path))

    # 신호CSV: 세 코드 모두 '만기 한참 지난' 옛 신호 보유
    pd.DataFrame([
        {"signal_date": "20260102", "code": "000030", "strategy": "rsi_vol", "holding_days": 2},
        {"signal_date": "20260102", "code": "000040", "strategy": "rsi_vol", "holding_days": 2},
        {"signal_date": "20260102", "code": "000050", "strategy": "rsi_vol", "holding_days": 2},
    ]).to_csv(tmp_path / "paper_signals.csv", index=False, encoding="utf-8-sig")

    # 원장: 000030 은 최근 재매수(만기 미도래) / 000050 은 만기 도달 / 000040 은 원장 없음
    kt.save_kiwoom_position("000030", 1000, "rsi_vol", "20260112", 5)   # 만기 미도래
    kt.save_kiwoom_position("000050", 1000, "rsi_vol", "20260102", 2)   # 만기 도달

    due = kt.codes_due_for_exit()
    assert "000030" not in due   # 원장 우선 — 옛 신호가 조기청산 못 시킴 (핵심)
    assert "000040" in due       # 원장 없음 → 신호CSV 폴백 (유실 대비 안전망)
    assert "000050" in due       # 원장 기준 만기 도달


def test_kis_codes_due_ledger_expiry_and_stop(tmp_path, monkeypatch):
    """KIS: 만기·stop 모두 원장 기준. stop_pct 는 원장 전략으로 선택."""
    os.environ.setdefault("KIS_MOCK_APP_KEY", "test")
    os.environ.setdefault("KIS_MOCK_APP_SECRET", "test")
    os.environ.setdefault("KIS_MOCK_ACCOUNT", "12345678-01")
    import kis_trader as kx
    import strategies.daily_loader as dl

    dates = ["20260102", "20260103", "20260106", "20260107", "20260108",
             "20260109", "20260112", "20260113"]
    monkeypatch.setitem(sys.modules, "strategies.daily_loader", dl)   # 오염 방탄(위 테스트와 동일 사유)
    monkeypatch.setattr(dl, "load_macro_daily",
                        lambda *a, **k: _synth_macro_df(["000060", "000070", "000080"], dates))
    monkeypatch.setattr(kx, "SIGNALS_CSV", str(tmp_path / "kis_paper_signals.csv"))
    monkeypatch.setattr(kx, "KIS_POSITIONS_CSV", str(tmp_path / "kis_positions.csv"))

    # 옛 신호(만기 지남)는 000060 에만 존재 — 원장이 최근 재매수라 due 오염 금지
    pd.DataFrame([
        {"signal_date": "20260102", "code": "000060",
         "strategy": "h52w_for3d_mkt", "holding_days": 2},
    ]).to_csv(tmp_path / "kis_paper_signals.csv", index=False, encoding="utf-8-sig")

    kx.save_kis_position("000060", 10000, "h52w_for3d_mkt", "20260112", 20)  # 만기 미도래
    kx.save_kis_position("000070", 10000, "gc_for3d", "20260112", 15)        # stop 후보

    # 000070: gc_for3d stop -26% → 종가 7,300(-27%) 이면 stop 발동
    due = kx.codes_due_for_exit({"000070": 7300.0, "000060": 10500.0})
    assert due.get("000070") == "stop"
    assert "000060" not in due   # 옛 신호 만기 오염 차단 + stop 미달(-0%대 아님, +5%)

    # 종가가 stop 위면 발동 안 함 (-26% 경계)
    due2 = kx.codes_due_for_exit({"000070": 7500.0})
    assert "000070" not in due2

    # reasons 필터(2026-07-17 트리거 분리): am=stop만 / pm=expire만
    kx.save_kis_position("000080", 10000, "gc_for3d", "20260102", 2)   # 만기 도과
    due_am = kx.codes_due_for_exit({"000070": 7300.0, "000080": 9000.0}, reasons=("stop",))
    assert due_am.get("000070") == "stop" and "000080" not in due_am   # am: 만기 무시
    due_pm = kx.codes_due_for_exit({"000070": 7300.0, "000080": 9000.0}, reasons=("expire",))
    assert due_pm.get("000080") == "expire" and "000070" not in due_pm  # pm: stop 무시


def test_after_market_signed_price_and_snapshot_replace(tmp_path, monkeypatch):
    """① 키움 signed 가격(cur_prc '-3050')이 음수로 DB 누적되던 것 → abs 정규화
    ② 재실행 시 순위권 이탈 종목의 스테일 행 잔존 → (date,market,side) 통째 교체."""
    import sqlite3
    import after_market as am
    rows = [{"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "-3050",
             "flu_rt": "-4.2", "acc_trde_qty": "1000", "tdy_close_pric_flu_rt": "1.1"}]
    recs = am._parse_rows(rows, "KOSPI", "down")
    assert recs[0]["ovt_price"] == 3050          # 가격 부호 제거
    assert recs[0]["ovt_chg_rt"] == -4.2         # 등락률 부호는 유지

    db = tmp_path / "stock.db"
    monkeypatch.setattr(am, "_db_path", lambda: str(db))
    r1 = [{"date": "20260712", "market": "KOSPI", "side": "up", "rank": 1,
           "code": "000001", "name": "A", "ovt_price": 100, "ovt_chg_rt": 1.0,
           "ovt_volume": 10, "reg_close_chg_rt": 0.5}]
    am._save_db(r1)
    am._save_db([dict(r1[0], code="000002", name="B")])   # 2차 실행: A 이탈, B 진입
    con = sqlite3.connect(str(db))
    codes = [r[0] for r in con.execute("SELECT code FROM after_market WHERE side='up'")]
    con.close()
    assert codes == ["000002"]                    # 스테일 A 행이 남지 않음


def test_data_collector_preferred_filter_code_suffix():
    """data_collector 우선주 판정도 신호엔진과 동일하게 코드 끝자리 결합(유니버스 정합)."""
    import data_collector as dc
    assert dc._is_excluded("삼성전자우", "005935") is True
    assert dc._is_excluded("이오플로우", "294090") is False   # '우'로 끝나는 보통주
    assert dc._is_excluded("KODEX 200", "069500") is True


def test_factor_scorer_supply_dates_survive_beyond_8_rows():
    """[critical 회귀방지] supply_demand 날짜 정규화가 Series 앞 8'행' 슬라이스로
    9행째부터 'nan' 이 되어 수급 피처(for_net5_db)가 전 종목 소실되던 버그(2026-07-11 수정)."""
    import sqlite3
    from factor_scorer import FactorScorer
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE supply_demand (ticker TEXT, date TEXT, "
                "foreign_net_value REAL, institution_net_value REAL)")
    rows = [("000020", f"2026-06-{d:02d}", 100.0, 50.0) for d in range(1, 13)]   # 12행(>8)
    con.executemany("INSERT INTO supply_demand VALUES (?,?,?,?)", rows)
    con.commit()
    feats = FactorScorer().prepare_db_features(con, "20260612")
    got = feats.get("000020", {}).get("for_net5_db")
    assert got == 500.0, f"마지막 5일 합 500 이어야 함(버그면 None/결손): {got}"


def test_is_excluded_preferred_stock_needs_nonzero_code_suffix():
    """'우'로 끝나는 보통주(이오플로우 등) 오제외 수정 — 우선주는 코드 끝자리≠0 결합 판정."""
    import live_signal as ls
    import kis_live_signal as kls
    assert ls.is_excluded("삼성전자우", "005935") is True      # 진짜 우선주
    assert ls.is_excluded("미래에셋증권2우B", "00680K") is True
    assert ls.is_excluded("이오플로우", "294090") is False      # '우'로 끝나는 보통주
    assert ls.is_excluded("에코글로우", "159910") is False
    assert kls.is_excluded("성우", "458650") is False
    assert ls.is_excluded("삼성전자", "005930") is False
    assert ls.is_excluded("KODEX 200", "069500") is True       # ETF 는 그대로 제외


def test_append_signals_zero_byte_recreates_header(tmp_path, monkeypatch):
    """0바이트 신호 CSV 잔재에 헤더 없이 append 해 트레이더가 KeyError 로 죽던 것 방지."""
    import live_signal as ls
    p = tmp_path / "paper_signals.csv"
    p.write_text("")   # 직전 실행이 쓰다 죽은 잔재
    monkeypatch.setattr(ls, "SIGNALS_CSV", str(p))
    ls.append_signals([{
        "signal_date": "20260711", "code": "005930", "name": "삼성전자",
        "entry_price_close": 1000.0, "target_exit_date": "20260721",
        "lookback_high": 0.0, "market_strong": True,
        "strategy": "rsi_vol", "holding_days": 7,
    }])
    df = pd.read_csv(p, encoding="utf-8-sig")
    assert "signal_date" in df.columns and len(df) == 1


def test_order_with_retry_429(monkeypatch):
    """초당 주문한도(HTTP 429) 재시도 — 07-13 서킷브레이커 3연발 중 2건 유실 실사례.
    429 는 재시도(미접수 거부라 중복 없음), 그 외 예외는 즉시 전파(중복주문 위험)."""
    import pytest as _pt
    import kiwoom_trader as kt
    monkeypatch.setattr(kt.time, "sleep", lambda s: None)   # 대기 생략

    calls = {"n": 0}
    def flaky():   # 2번 429 후 성공
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("API Error (HTTP 429): Unknown error")
        return {"return_code": 0, "ord_no": "OK1"}
    r = kt._order_with_retry(flaky, "매도", retries=2)
    assert r["ord_no"] == "OK1" and calls["n"] == 3

    def always_429():
        raise RuntimeError("API Error (HTTP 429)")
    with _pt.raises(RuntimeError, match="429"):
        kt._order_with_retry(always_429, "매도", retries=2)   # 소진 후 전파

    calls2 = {"n": 0}
    def other_err():
        calls2["n"] += 1
        raise RuntimeError("증거금 부족")
    with _pt.raises(RuntimeError, match="증거금"):
        kt._order_with_retry(other_err, "매수", retries=2)
    assert calls2["n"] == 1   # 429 아님 → 재시도 없이 즉시 전파(중복주문 방지)

    def rejected():   # HTTP 200 + return_code!=0 은 _assert_order_ok 가 승격
        return {"return_code": 8005, "return_msg": "주문거부"}
    with _pt.raises(RuntimeError, match="8005"):
        kt._order_with_retry(rejected, "매수", retries=2)


def test_assert_order_ok_promotes_rejection():
    """키움 동기 응답의 주문 거부(HTTP 200 + return_code!=0)를 예외로 승격 —
    거부가 ok=True 로 기록되던 critical 수정(2026-07-11)."""
    import pytest as _pt
    import kiwoom_trader as kt
    ok = {"return_code": 0, "ord_no": "123"}
    assert kt._assert_order_ok(ok, "매수") is ok                    # 정상 통과
    assert kt._assert_order_ok({"ord_no": "123"}, "매수")            # return_code 없음 → 통과
    with _pt.raises(RuntimeError, match="8005"):
        kt._assert_order_ok({"return_code": 8005, "return_msg": "증거금 부족"}, "매수")
    with _pt.raises(RuntimeError):
        kt._assert_order_ok({"return_code": "-1"}, "매도")


def test_norm_stk_code_prefix_only():
    """접두어 A 만 제거 — 코드 내 알파벳(0001A0 덕양에너젠, 실신호 8건)은 보존."""
    import kiwoom_trader as kt
    assert kt._norm_stk_code("A005930") == "005930"
    assert kt._norm_stk_code("005930") == "005930"
    assert kt._norm_stk_code("0001A0") == "0001A0"    # 기존 replace('A','')는 '000010'로 파괴
    assert kt._norm_stk_code("A0001A0") == "0001A0"
    assert kt._norm_stk_code("5930") == "005930"      # zfill 유지


def test_offset_date_bdate_fallback():
    """target_exit_date 영구 공란 수정 — 달력 밖 만기는 주말 건너뛴 근사일(2026-07-11)."""
    import live_signal as ls
    import kis_live_signal as kls
    ds = ["20260708", "20260709", "20260710"]   # 수~금
    assert ls._offset_date(ds, "20260708", 2) == "20260710"       # 달력 안 — 기존 경로
    # 금요일(0710) 기준 +2 영업일 → 주말 건너뛰고 화요일(0714)
    assert ls._offset_date(ds, "20260710", 2) == "20260714"
    assert kls._offset_date(ds, "20260710", 2) == "20260714"
    assert ls._offset_date(ds, "20991231", 5) == ""               # 달력에 없는 신호일


def test_expiry_due_calendar_edge_off_by_one():
    """만기 오프바이원 수정(2026-07-10) — 실행 시점 달력은 '어제'까지라 만기일 당일
    판정이 항상 False 였음. 달력 마지막+1 영업일이 만기이고 today 가 그 뒤면 due."""
    import kiwoom_trader as kt
    import kis_trader as kx
    ds = ["20260102", "20260103", "20260106", "20260107"]   # '어제'(0107)까지만 존재
    # 신호 0102·보유 4일 → 진입 0103, 만기 4일째 = 0108(달력 밖 +1) → 오늘 0108 = 만기일
    assert kt._expiry_due(ds, "20260102", 4, "20260108") is True
    assert kx._expiry_due(ds, "20260102", 4, "20260108") is True
    # 보유 5일 → 만기 0109(달력 밖 +2) → 오늘 0108엔 미도래 (데이터 정체 시 보수적 대기)
    assert kt._expiry_due(ds, "20260102", 5, "20260108") is False
    # 만기가 달력 안(0107)이면 기존 경로 그대로
    assert kt._expiry_due(ds, "20260102", 3, "20260108") is True


def test_kis_ensure_ledger_heal_and_prune(tmp_path, monkeypatch):
    """KIS 원장 자가치유(2026-07-10 신설): 좀비 prune + 브로커 보유 heal(전략별 보유일,
    신호일 역추적) + 오늘 매도분 부활 금지 — 키움 ensure 와 대칭."""
    from datetime import datetime as _dt
    os.environ.setdefault("KIS_MOCK_APP_KEY", "test")
    os.environ.setdefault("KIS_MOCK_APP_SECRET", "test")
    os.environ.setdefault("KIS_MOCK_ACCOUNT", "12345678-01")
    import kis_trader as kx
    monkeypatch.setattr(kx, "ORDERS_DIR", str(tmp_path))
    monkeypatch.setattr(kx, "KIS_POSITIONS_CSV", str(tmp_path / "kis_positions.csv"))
    monkeypatch.setattr(kx, "SIGNALS_CSV", str(tmp_path / "kis_paper_signals.csv"))

    today = _dt.today().strftime("%Y%m%d")
    pd.DataFrame([{"time": "09:01", "side": "buy", "code": "777777", "name": "G",
                   "strategy": "gc_for3d", "qty": 10, "price": 5000,
                   "order_type": "m", "reason": "signal", "ok": True, "order_no": "B7", "msg": ""}]) \
        .to_csv(tmp_path / "kis_orders_20260702.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"time": "09:01", "side": "sell", "code": "888888", "name": "H",
                   "strategy": "h52w_for3d_mkt", "qty": 5, "price": 3000,
                   "order_type": "m", "reason": "expire", "ok": True, "order_no": "S8", "msg": ""}]) \
        .to_csv(tmp_path / f"kis_orders_{today}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"signal_date": "20260701", "code": "777777",
                   "strategy": "gc_for3d", "holding_days": 15}]) \
        .to_csv(tmp_path / "kis_paper_signals.csv", index=False, encoding="utf-8-sig")

    kx.save_kis_position("888888", 3000, "h52w_for3d_mkt", "20260601", 20)   # 오늘 매도된 잔재
    kx.save_kis_position("999999", 100, "gc_for3d", "20260601", 15)          # 좀비

    # 1단계(체결 전): 브로커 잔고에 888888 아직 보임 — 행 유지(매도 거부 가능성 보호),
    # 좀비 999999 는 prune, 777777 은 heal 등록.
    kx.ensure_kis_ledger({
        "777777": {"qty": 10, "name": "G", "avg_price": 5100},
        "888888": {"qty": 5, "name": "H", "avg_price": 3000},
    })
    led = kx.load_kis_positions()
    assert "888888" in led                          # 브로커가 아직 보유로 보임 → 보수적 유지
    assert "999999" not in led                      # 좀비 prune
    assert led["777777"]["entry_px"] == 5100        # 브로커 매입평균 우선
    assert led["777777"]["strategy"] == "gc_for3d"
    assert led["777777"]["holding_days"] == 15      # 전략별 보유일
    assert led["777777"]["signal_date"] == "20260701"  # 매수일(0702) 직전 신호일 역추적

    # 2단계(체결 후): 888888 이 잔고에서 사라짐 → prune + 오늘 매도분이라 부활 금지
    kx.ensure_kis_ledger({"777777": {"qty": 10, "name": "G", "avg_price": 5100}})
    led = kx.load_kis_positions()
    assert "888888" not in led
    assert "777777" in led


# ─────────────────────────────────────────────────────────────────────────────
# 서킷브레이커(-40%) + KIS 강도순 정렬 (2026-07-10 사용자 결정 도입)
# ─────────────────────────────────────────────────────────────────────────────

def test_circuit_breaker_codes_threshold_and_fallback():
    import kiwoom_trader as kt
    pos = {
        "067290": {"qty": 202, "name": "JW신약", "price": 1491, "avg_price": 3030},   # -50.8%
        "192410": {"qty": 223, "name": "오늘이엔엠", "price": 3325, "avg_price": 2920},  # +13.9%
        "105330": {"qty": 10, "name": "KNW", "price": 6500, "avg_price": 10000},      # -35.0% (경계 위)
        "900300": {"qty": 255, "name": "오가닉", "price": 0, "avg_price": 0,
                   "pnl_pct": -45.0},                                                  # 가격 결손 → pnl_pct 폴백
    }
    cb = kt._circuit_breaker_codes(pos)
    assert "067290" in cb and cb["067290"] <= -40
    assert "900300" in cb                        # 폴백 경로
    assert "105330" not in cb and "192410" not in cb

    # 비활성(None) 시 빈 dict
    orig = kt.CIRCUIT_BREAKER_PCT
    try:
        kt.CIRCUIT_BREAKER_PCT = None
        assert kt._circuit_breaker_codes(pos) == {}
    finally:
        kt.CIRCUIT_BREAKER_PCT = orig


def test_kiwoom_get_api_memoized(monkeypatch):
    """get_api() 는 프로세스당 1회만 토큰 발급 — 캐시가 있으면 즉시 반환(재발급 없음).
    (2026-07-21 daily-am 토큰 429 크래시 회귀 방지: 매도·매수·익절·상태조회가 각각
    get_api 를 불러 토큰을 여러 번 발급 → 모의서버 rate limit 429 로 cmd_status 즉사.)"""
    import kiwoom_trader as kt
    sentinel = object()
    monkeypatch.setattr(kt, "_API_BUNDLE", sentinel)
    # 캐시가 채워져 있으면 config/네트워크를 건드리지 않고 그대로 반환해야 한다.
    assert kt.get_api() is sentinel


def test_kis_order_candidates_strength_first_tv_fallback():
    """KIS 후보 정렬 — 강도 내림차순, 무기록은 tv순 뒤로. 필터(6.0 차단)는 미적용이므로
    낮은 강도도 '뒤로 밀릴 뿐' 제외되지 않는다."""
    os.environ.setdefault("KIS_MOCK_APP_KEY", "test")
    os.environ.setdefault("KIS_MOCK_APP_SECRET", "test")
    os.environ.setdefault("KIS_MOCK_ACCOUNT", "12345678-01")
    import kis_trader as kx
    sigs = [
        {"code": "000001", "strategy": "gc_for3d"},   # 강도 3.0 (낮지만 제외 안 됨)
        {"code": "000002", "strategy": "gc_for3d"},   # 무기록, tv 최고
        {"code": "000003", "strategy": "gc_for3d"},   # 강도 7.1
    ]
    strength = {("000001", "gc_for3d"): 3.0, ("000003", "gc_for3d"): 7.1}
    tv = {"000001": 1e9, "000002": 9e9, "000003": 2e9}
    out = [s["code"] for s in kx._order_candidates(sigs, strength, tv, "gc_for3d")]
    assert out == ["000003", "000001", "000002"], out
    out2 = [s["code"] for s in kx._order_candidates(sigs, {}, tv, "gc_for3d")]
    assert out2 == ["000002", "000003", "000001"], out2   # 무기록 전체 → tv순 폴백


def test_realized_pnl_fifo_and_close_backfill(monkeypatch):
    srv = _srv()
    # 매도일 종가 조회를 합성값으로 대체(파일 무접촉)
    monkeypatch.setattr(srv, "_daily_close_map",
                        lambda d: {"000010": 1100.0} if str(d) == "20260620" else {})
    df = pd.DataFrame([
        {"date": "20260601", "time": "09:01", "side": "buy",  "code": "000010",
         "qty": 10, "price": 1000, "ok": True},
        {"date": "20260620", "time": "15:21", "side": "sell", "code": "000010",
         "qty": 10, "price": 0, "ok": True},          # price=0 → 종가 1100 보정
        {"date": "20260602", "time": "09:01", "side": "buy",  "code": "000020",
         "qty": 5, "price": 2000, "ok": True},
        {"date": "20260621", "time": "15:21", "side": "sell", "code": "000020",
         "qty": 5, "price": 1800, "ok": True},
        {"date": "20260622", "time": "09:03", "side": "tp_sell", "code": "000020",
         "qty": 5, "price": 2400, "ok": True},        # tp '발주'는 체결 아님 → pnl 없음
    ]).fillna("")
    out = srv._attach_realized_pnl(df)
    s1 = out[(out["side"] == "sell") & (out["code"] == "000010")].iloc[0]
    assert s1["pnl"] == 1000 and s1["pnl_pct"] == 10.0
    assert s1["price"] == 1100                         # 표시가도 종가로 보정
    s2 = out[(out["side"] == "sell") & (out["code"] == "000020")].iloc[0]
    assert s2["pnl"] == -1000 and s2["pnl_pct"] == -10.0
    tp = out[out["side"] == "tp_sell"].iloc[0]
    assert tp["pnl"] == "" and tp["pnl_pct"] == ""     # 발주 행은 손익 미부여
    for _, b in out[out["side"] == "buy"].iterrows():
        assert b["pnl"] == ""
