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


def test_strength_thresholds_declared_per_account():
    """계좌별 강도 임계가 '의도한 값'으로 고정돼 있는지 + 가상매매가 키움과 동기인지.

    (2026-07-21 사용자 신고 회귀 방지: KIS 는 필터 자체가 없어 6 미만이 전량 체결됐고,
    키움은 5.7 인데 로그가 '< 6' 으로 표시돼 약속과 실제가 어긋나 있었다.)
    계좌별로 값이 '다른 것'은 의도된 설계다 — KIS 는 신호 풀이 희소(월 14건)해 6.0 이면
    사실상 매매 정지라 5.7. 다만 값이 조용히 표류하면 같은 사고가 재발하므로 못박는다."""
    import kiwoom_trader as kt
    import kis_trader as kx

    assert kt.MIN_STRENGTH_SCORE == 5.7, "키움 강도 임계가 의도(5.7)와 다름"
    assert kx.MIN_STRENGTH_SCORE == 5.7, "KIS 강도 임계가 의도(5.7)와 다름"

    # 가상매매(strength 포트폴리오)는 키움 라이브 룰을 미러링 → 같은 값이어야 비교 성립
    src = open(os.path.join(OUTPUTS, "ai_paper_trader.py"), encoding="utf-8").read()
    assert f'min_score={kt.MIN_STRENGTH_SCORE}' in src, \
        "ai_paper_trader 의 strength 시뮬 min_score 가 키움 라이브 임계와 불일치"


def test_verify_strength_recovers_and_fails_closed(tmp_path, monkeypatch):
    """매매 전 강도 재확인: 무기록이면 재계산 시도, 그래도 없으면 '무기록'으로 보고.

    (2026-07-21 사용자 요청 "매매 시행 전 강도 재확인 후 매매" — 호출부는 무기록을
    차단(fail-closed)하므로, 이 함수가 미확보분을 정확히 돌려주는 것이 안전의 핵심.)"""
    import kiwoom_trader as kt

    sigs = [{"code": "005930", "strategy": "rsi_reversal", "signal_date": "20260721"},
            {"code": "000660", "strategy": "rsi_reversal", "signal_date": "20260721"}]

    # (1) 전원 기록 있음 → 재계산 없이 그대로 통과, 미확보 0건
    full = {("005930", "rsi_reversal"): 6.4, ("000660", "rsi_reversal"): 7.1}
    out, missing = kt.verify_strength(sigs, dict(full))
    assert missing == set() and out == full

    # (2) 일부 무기록 + 재계산도 실패 → 그 항목이 '미확보'로 보고돼야(호출부가 차단)
    def _boom(*a, **k):
        raise RuntimeError("강도 로거 사용 불가(테스트)")
    monkeypatch.setattr(kt, "_strength_map", lambda *a, **k: {("005930", "rsi_reversal"): 6.4})
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "strength_logger",
                        type("M", (), {"log_strength": staticmethod(_boom)}))
    out2, missing2 = kt.verify_strength(sigs, {("005930", "rsi_reversal"): 6.4})
    assert ("000660", "rsi_reversal") in missing2, "재계산 실패분이 미확보로 보고되지 않음"
    assert ("005930", "rsi_reversal") not in missing2


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


# ─────────────────────────────────────────────────────────────────────────────
# 유니버스 신선도 게이트 (market_calendar) — 2026-08-20 신설
#   실사고: latest_macro_date() 는 '폴더의 마지막 파일'일 뿐이라 수집이 밀리면
#   며칠 묵은 신호로 조용히 매수한다. 주말·휴장일을 stale 로 오판하면 반대로
#   멀쩡한 매매를 막으므로 두 방향 모두 못박는다.
# ─────────────────────────────────────────────────────────────────────────────

def test_universe_gap_ignores_weekend_and_holiday_marker(tmp_path):
    import market_calendar as mc
    d = str(tmp_path)
    (tmp_path / "20260814.csv").write_text("x", encoding="utf-8")       # 금
    (tmp_path / "20260817.csv.holiday").write_text("", encoding="utf-8")  # 대체휴일

    # 금 → 화(주말 + 휴장일 마커) = 빠진 거래일 없음
    assert mc.universe_gap("20260814", "20260818", d) == (0, [])
    assert mc.check_signal_freshness("20260814", "T", "20260818", d) == (True, None)

    # 마커가 없으면 그 평일은 '빠진 거래일'
    (tmp_path / "20260817.csv.holiday").unlink()
    n, missing = mc.universe_gap("20260814", "20260818", d)
    assert (n, missing) == (1, ["20260817"])


def test_signal_freshness_warns_then_blocks(tmp_path):
    import market_calendar as mc
    d = str(tmp_path)
    (tmp_path / "20260814.csv").write_text("x", encoding="utf-8")
    (tmp_path / "20260817.csv.holiday").write_text("", encoding="utf-8")

    # 1거래일 지연 → 경고하되 매수는 진행
    ok, msg = mc.check_signal_freshness("20260814", "T", "20260819", d)
    assert ok is True and msg and "1거래일" in msg

    # 2거래일 지연 → 매수 차단
    ok, msg = mc.check_signal_freshness("20260814", "T", "20260820", d)
    assert ok is False and msg and "차단" in msg


def test_signal_freshness_fails_open_on_bad_input(tmp_path):
    """달력 판정 실패가 매매를 멈추면 안 된다(fail-open)."""
    import market_calendar as mc
    assert mc.universe_gap("bad-date", "20260820", str(tmp_path)) == (0, [])
    assert mc.check_signal_freshness("bad-date", "T", "20260820", str(tmp_path)) \
        == (True, None)


# ─────────────────────────────────────────────────────────────────────────────
# KIS 계좌 주문불가 분류 — 2026-08-20 신설
#   실사고: 08-10 이후 모의계좌가 msg_cd=40910000 으로 모든 주문을 거부했는데
#   종목 단위 실패와 동일 취급돼 열흘간 아무도 몰랐다(만기 청산 실패 누적).
# ─────────────────────────────────────────────────────────────────────────────

def test_kis_account_blocked_is_distinguished_from_order_error():
    import kis_trader as kx

    assert kx._is_account_blocked("40910000", "") is True
    assert kx._is_account_blocked("", "모의투자 주문이 불가한 계좌입니다.") is True
    assert kx._is_account_blocked("40580000", "주문가능금액이 부족합니다") is False

    with pytest.raises(kx.AccountBlocked):
        kx._raise_order_error("매도", {"msg_cd": "40910000",
                                       "msg1": "모의투자 주문이 불가한 계좌입니다."})

    # 평범한 주문 거부는 AccountBlocked 가 아니어야 한다(긴급 경보 오발 방지)
    with pytest.raises(RuntimeError) as ei:
        kx._raise_order_error("매수", {"msg_cd": "40580000",
                                       "msg1": "주문가능금액이 부족합니다"})
    assert not isinstance(ei.value, kx.AccountBlocked)


# ─────────────────────────────────────────────────────────────────────────────
# score_ic available-only 정규화 — 2026-08-20
#   결측 피처가 분모만 키워 라이브 강도를 5.0 쪽으로 누르던 것 수정.
# ─────────────────────────────────────────────────────────────────────────────

def test_score_ic_normalizes_on_available_features_only():
    import factor_scorer as fs

    sc = fs.FactorScorer.__new__(fs.FactorScorer)      # __init__(IC 계산) 우회
    sc.ic_weights = {"a": 0.2, "b": 0.2, "c": 0.2, "d": 0.2}
    sc.feat_stats = {k: [0.0, 1.0] for k in "abcd"}    # 값 1.0 → pct_rank = 0.5

    # 전부 최상위(값 2.0 → pct_rank 1.0): 가용 피처만으로 정규화하면 만점 쪽
    full = sc.score_ic({k: 2.0 for k in "abcd"})
    assert full["coverage"] == 1.0 and full["normalized_on"] == "available"
    assert full["total"] == 10.0

    # 절반만 존재해도 '아는 것 기준'으로는 동일한 만점이어야 한다
    half = sc.score_ic({"a": 2.0, "b": 2.0})
    assert half["n_available"] == 2 and half["coverage"] == 0.5
    assert half["normalized_on"] == "available"
    assert half["total"] == 10.0, "결측 피처가 분모를 키워 점수를 누르면 안 된다"


def test_score_ic_falls_back_to_full_denominator_when_coverage_too_low():
    """가용 질량이 하한 미만이면 소수 피처의 과증폭 대신 보수적(=5.0 쪽) 판정."""
    import factor_scorer as fs

    sc = fs.FactorScorer.__new__(fs.FactorScorer)
    sc.ic_weights = {k: 0.2 for k in "abcde"}          # 5개 균등 → 1개면 커버리지 0.2
    sc.feat_stats = {k: [0.0, 1.0] for k in "abcde"}

    r = sc.score_ic({"a": 2.0})
    assert r["coverage"] == 0.2 and r["normalized_on"] == "full"
    assert r["total"] == 6.0        # 5.0 + (0.2*0.5)/(0.5) * 5 * (1/5)


# ─────────────────────────────────────────────────────────────────────────────
# 장전 탐침 재탐침 범위 — 2026-08-21
#   실사고: 계좌 이상 시 캐시를 무시하고 재탐침하게 바꿨더니(08-20) 양 계좌를
#   매일 다시 찔러, 이미 '수용'으로 확정된 키움에도 매일 실주문이 나갔다.
#   탐침 주문(24.1만원)이 장중 내내 예수금을 묶었다(08-21 실측:
#   9,158,011 -> 8,916,171, 미체결 1건). 이상이 있는 계좌만 찔러야 한다.
# ─────────────────────────────────────────────────────────────────────────────

def test_probe_reprobes_only_the_broken_account():
    import preopen_probe as pp

    prev = {
        "kiwoom": {"accepted": True,  "msg": "접수됨(주문번호 0001240)", "fatal": False},
        "kis":    {"accepted": False, "msg": "거부: 40910000 모의투자 주문이 불가한 계좌입니다.",
                   "fatal": True},
    }
    assert pp.probe_targets(prev) == ["kis"], \
        "정상 계좌까지 재탐침하면 실주문이 예수금을 묶는다"

    # 양쪽 정상 → 재탐침 자체가 불필요
    ok = {"kiwoom": {"accepted": True, "msg": "접수됨", "fatal": False},
          "kis":    {"accepted": True, "msg": "접수됨", "fatal": False}}
    assert pp.probe_targets(ok) == []

    # fatal 키가 없던 옛 기록도 msg 로 판정된다(2026-08-20 이전 형식 호환)
    legacy = {"kiwoom": {"accepted": True, "msg": "접수됨(주문번호 1)"},
              "kis":    {"accepted": False,
                         "msg": "거부: 40910000 모의투자 주문이 불가한 계좌입니다."}}
    assert pp.probe_targets(legacy) == ["kis"]

    # 시간대 거부(정상 결과)는 재탐침 대상이 아니다
    timeband = {"kiwoom": {"accepted": False, "msg": "거부: RC4057 모의투자 장시작전"},
                "kis":    {"accepted": False, "msg": "거부: RC4057 모의투자 장시작전"}}
    assert pp.probe_targets(timeband) == []


def test_probe_cancel_helper_parses_order_no():
    """접수 메시지에서 주문번호를 못 뽑으면 취소를 시도하지 않아야 한다(오취소 방지)."""
    import preopen_probe as pp
    assert "취소 생략" in pp._cancel_kiwoom("", "005930")
    assert "취소 생략" in pp._cancel_kiwoom("접수됨", "005930")


# ─────────────────────────────────────────────────────────────────────────────
# 크래시 알림 문구 ↔ bat 재시도 정책 정합 — 2026-08-21
#   실사고: 08-21 15:22 KIS daily-pm 이 ReadTimeout 으로 죽으며
#   "실행 중단(자동 재시도 없음)" 이라고 알렸는데, bat 에는 2026-07-21 부터
#   5분 간격 2회 재시도 루프가 있었다. 문구만 옛 상태로 남아 사용자가
#   '완전히 멈췄다'고 오해했다. 둘이 어긋나면 다시 같은 오해가 생긴다.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mod_name,bat_name", [
    ("kis_trader", "run_kis_trader.bat"),
    ("kiwoom_trader", "run_kiwoom.bat"),
])
def test_crash_notice_matches_bat_retry_policy(mod_name, bat_name):
    import importlib
    mod = importlib.import_module(mod_name)

    note = mod._retry_note()
    assert bat_name in note, "알림이 어느 러너가 재시도하는지 밝혀야 한다"
    assert "재시도 없음" not in note, "bat 에 재시도 루프가 있는데 없다고 알리면 안 된다"

    bat = open(os.path.join(OUTPUTS, bat_name), encoding="utf-8").read()
    assert "goto :attempt" in bat and "TRIES" in bat, \
        f"{bat_name} 의 재시도 루프가 사라졌다면 _retry_note 문구도 함께 고쳐야 한다"
    # 대기 전/후 마커가 모두 있어야 '재시도 대기 중 사망'을 구분할 수 있다
    assert "retry in 300s" in bat and "wait done - starting attempt" in bat, \
        f"{bat_name}: 재시도 대기 전후 마커가 모두 있어야 진단이 가능하다"


def test_expire_sell_gets_a_larger_balance_retry_budget():
    """만기 청산은 마감 동시호가(15:20~15:30) 안에서 끝내면 되므로 더 끈질기게 기다린다.

    08-21 실사고: 15:21:07 시작 → 잔고조회 3회 전부 타임아웃 → 15:22:18 크래시.
    그 시점에 창이 8분 남아 있었고 15:40 조회는 정상이었다.
    """
    import inspect
    import kis_trader as kx

    sig = inspect.signature(kx.KISMockClient.get_balance)
    assert "attempts" in sig.parameters
    assert sig.parameters["attempts"].default == 3, "기본값(매수 경로)은 종전 유지"

    src = inspect.getsource(kx.cmd_sell)
    assert 'if "expire" in reasons else 3' in src, \
        "만기 경로와 손절 경로의 재시도 예산이 분리돼 있어야 한다"


def test_token_cache_is_bound_to_the_app_key():
    """자격증명 재발급 후 옛 토큰이 재사용되면 안 된다.

    [2026-08-21] 모의계좌 재발급 시 APP_KEY/SECRET 이 바뀌는데, 토큰 캐시에는
    '어느 키로 받은 토큰인지' 정보가 없어서 만료 전(최대 24시간)까지 옛 토큰을
    새 자격증명과 섞어 계속 보냈다. 증상은 인증 실패인데 원인은 캐시라
    '재발급했는데도 안 된다'로 오진하기 쉽다.
    """
    import json
    import time
    import kis_trader as kx

    cl = kx.KISMockClient.__new__(kx.KISMockClient)      # __init__ 우회(네트워크 무접촉)
    fp = kx.KISMockClient._key_fingerprint()
    assert fp and len(fp) == 12

    import tempfile
    import os as _os
    d = tempfile.mkdtemp()
    cache = _os.path.join(d, "tok.json")
    orig = kx.TOKEN_CACHE
    try:
        kx.TOKEN_CACHE = cache

        # 지문이 맞고 만료 전 → 재사용
        json.dump({"token": "GOOD", "expire": time.time() + 3600, "key_fp": fp},
                  open(cache, "w"))
        assert cl._load_cached_token() == "GOOD"

        # 지문이 다르면(=키 교체) 만료 전이어도 폐기
        json.dump({"token": "OLD", "expire": time.time() + 3600, "key_fp": "deadbeef1234"},
                  open(cache, "w"))
        assert cl._load_cached_token() is None, "앱키가 바뀌었는데 옛 토큰을 재사용했다"

        # 구 형식(지문 없음)도 폐기 — 교체 여부를 알 수 없으므로 안전한 쪽
        json.dump({"token": "LEGACY", "expire": time.time() + 3600}, open(cache, "w"))
        assert cl._load_cached_token() is None
    finally:
        kx.TOKEN_CACHE = orig


def test_verify_kis_account_script_places_no_orders():
    """점검 스크립트가 주문 API 를 건드리지 않는지 못박는다(조회 전용 보장)."""
    src = open(os.path.join(OUTPUTS, "verify_kis_account.py"), encoding="utf-8").read()
    for forbidden in ("order_buy", "order_sell", "order-cash", "probe_kis", "probe_kiwoom"):
        assert forbidden not in src, f"점검 스크립트에 주문 경로({forbidden})가 들어갔다"


# ─────────────────────────────────────────────────────────────────────────────
# bat 파일 위생 — 2026-08-26
#   실사고 2건이 겹쳐 KIS 트레이더가 08-24~26 사흘간 로그조차 없이 죽었다.
#   ① 08-21 에 넣은 REM 주석이 괄호 블록 안에서 따옴표 짝을 깨뜨려 cmd 가 닫는
#      괄호를 잃고 파싱 붕괴 → '/b' is not recognized, 로그 0줄, exit 1.
#   ② 파일 끝 NUL 패딩(수백 바이트)이 goto 레이블 탐색을 깨뜨려
#      `goto :attempt` 가 "cannot find the batch label" 로 실패 → 재시도 루프가
#      2026-07-21 도입 이후 한 번도 작동한 적이 없었다(08-21 PM 미스터리의 답).
#   둘 다 파일을 열어보면 즉시 보이는 것이라 테스트로 고정한다.
# ─────────────────────────────────────────────────────────────────────────────

def _bat_files():
    import glob
    return sorted(glob.glob(os.path.join(OUTPUTS, "*.bat")))


def test_bats_have_no_nul_padding():
    """NUL 이 하나라도 있으면 goto/레이블 탐색이 깨진다."""
    bad = []
    for p in _bat_files():
        n = open(p, "rb").read().count(b"\x00")
        if n:
            bad.append(f"{os.path.basename(p)}({n})")
    assert not bad, f"bat 에 NUL 패딩이 있다 — goto 가 실패한다: {', '.join(bad)}"


def test_bats_end_with_newline():
    """마지막 줄이 개행으로 끝나지 않으면 cmd 가 그 줄을 잃을 수 있다."""
    bad = [os.path.basename(p) for p in _bat_files()
           if not open(p, "rb").read().endswith(b"\n")]
    assert not bad, f"bat 이 개행으로 끝나지 않는다: {', '.join(bad)}"


def test_no_quoted_rem_inside_paren_block():
    """괄호 블록 안 REM 에 홀수 개의 따옴표가 있으면 cmd 파싱이 붕괴한다.

    2026-08-21 실사고 재발 방지. 블록 안에는 주석을 두지 말고 블록 밖에 쓴다.
    """
    import re
    bad = []
    for p in _bat_files():
        text = open(p, encoding="utf-8", errors="replace").read()
        depth = 0
        for i, line in enumerate(text.split("\n"), 1):
            st = line.strip()
            if depth > 0 and re.match(r"(?i)^rem\b", st) and st.count('"') % 2:
                bad.append(f"{os.path.basename(p)}:{i}")
            depth = max(0, depth + st.count("(") - st.count(")"))
    assert not bad, (
        "괄호 블록 안 REM 에 홀수 따옴표가 있다(cmd 파싱 붕괴) — "
        f"주석을 블록 밖으로 옮길 것: {', '.join(bad)}")


def test_bats_are_ascii_only():
    """한국어 Windows 의 cmd 는 UTF-8 한글 바이트열을 CP949 로 오독해 줄을 잘못 자른다."""
    bad = []
    for p in _bat_files():
        n = sum(1 for c in open(p, "rb").read() if c > 127)
        if n:
            bad.append(f"{os.path.basename(p)}({n}bytes)")
    assert not bad, f"bat 에 비ASCII 바이트가 있다: {', '.join(bad)}"


# ─────────────────────────────────────────────────────────────────────────────
# 로그 에러 집계 — 2026-08-26
#   실사고: 하루요약이 "파이프라인 에러 18건"이라고 알렸는데 실제 크래시는 6건,
#   그중 1건은 재시도로 회복(최종 ExitCode=0), 5건은 손절 감시가 KIS 서버
#   브라운아웃에 걸린 것으로 매매 영향 0 이었다. 부풀림 원인은 파이썬의
#   chained exception 이 사건 하나에 Traceback 헤더를 3번 찍는 것.
# ─────────────────────────────────────────────────────────────────────────────

def _load_scan_helper():
    """daily_summary 를 import 하면 텔레그램을 보내므로 헬퍼만 떼어내 평가한다."""
    src = open(os.path.join(OUTPUTS, "daily_summary.py"), encoding="utf-8").read()
    i = src.find("def _scan_today_errors")
    j = src.find("\n\n# ── 파이프라인", i)
    assert i != -1 and j != -1, "_scan_today_errors 헬퍼를 찾지 못했다"
    ns = {}
    exec(src[i:j], ns)
    return ns["_scan_today_errors"]


CHAINED = """Traceback (most recent call last):
  File "x", line 1
TimeoutError: read timed out

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "y", line 2
ReadTimeoutError: pool

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "z", line 3
ReadTimeout: boom
"""


def test_chained_exception_counts_as_one_event(tmp_path):
    scan = _load_scan_helper()
    (tmp_path / "kis_stop_20260826.log").write_text(CHAINED, encoding="utf-8")

    total, rec, detail = scan(str(tmp_path), "20260826")
    assert total == 1, f"체인 예외 1건이 {total}건으로 부풀려졌다"
    assert rec == 0 and detail[0][1] == 1


def test_retry_recovered_crash_is_reported_as_recovered(tmp_path):
    scan = _load_scan_helper()
    (tmp_path / "kis_trader_20260826_1521.log").write_text(
        CHAINED + "\n[..] KIS trader attempt 1 done. ExitCode=1\n"
                  "[..] retry in 300s\n"
                  "[..] KIS trader attempt 2 done. ExitCode=0\n", encoding="utf-8")

    total, rec, detail = scan(str(tmp_path), "20260826")
    assert (total, rec) == (1, 1), "재시도로 회복된 크래시가 회복으로 분류되지 않았다"
    assert detail[0][2] is True


def test_audit_does_not_count_its_own_report(tmp_path):
    """리포트 본문에 'Traceback' 이라는 단어가 들어가면 다음 실행이 자기를 센다."""
    scan = _load_scan_helper()
    (tmp_path / "daily_audit_20260826.log").write_text(
        "  [!! ] 오늘 로그 에러(Traceback (most recent call last)) — 3건\n",
        encoding="utf-8")

    total, _, _ = scan(str(tmp_path), "20260826", exclude_prefix=("daily_audit",))
    assert total == 0, "감사 리포트가 자기 자신을 에러로 셌다"


# ─────────────────────────────────────────────────────────────────────────────
# 3층 안전장치 — 계좌 정지 스위치 (2026-08-27 신설)
#   2026-08 실사고: bat 손상 3일 무실행 / KIS 계좌 12일 주문불가 / 청산 반복 실패가
#   전부 '경보는 갔지만 매매는 계속'이었다. watchdog 이 알리기만 하고 멈추지 않았다.
#   이 스위치의 계약을 못박는다 — 특히 '매도는 절대 막지 않는다'.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def ks_tmp(tmp_path, monkeypatch):
    import kill_switch as ks
    monkeypatch.setattr(ks, "STATE_PATH", str(tmp_path / "kill_switch.json"))
    return ks


def test_kill_switch_engage_release_cycle(ks_tmp):
    ks = ks_tmp
    assert ks.status()[0] is False, "초기 상태는 허용이어야 한다"

    assert ks.engage("no_run", "테스트", notify=False) is True
    assert ks.status()[0] is True

    # 이미 걸려 있으면 재발동하지 않는다(알림 스팸 방지)
    assert ks.engage("order_blocked", "다른 사유", notify=False) is False
    assert ks.status()[1]["reason"] == "no_run", "최초 사유가 보존돼야 한다"

    assert ks.release("확인함", notify=False) is True
    assert ks.status()[0] is False
    assert ks.release("또", notify=False) is False


def test_kill_switch_blocks_buy_but_exits_zero(ks_tmp):
    """의도된 정지는 exit 0 — exit 1 이면 bat 재시도 루프가 5분 뒤 또 시도한다."""
    ks = ks_tmp
    ks.engage("no_run", "테스트", notify=False)
    with pytest.raises(SystemExit) as ei:
        ks.guard_buy("테스트계좌")
    assert ei.value.code == 0, "정지는 실패가 아니라 의도된 종료다"


def test_kill_switch_fails_open_on_unreadable_state(ks_tmp, monkeypatch):
    """상태를 못 읽었다고 매매를 멈추면, 막으려던 '조용한 실패'를 새로 만드는 셈이다."""
    ks = ks_tmp
    with open(ks.STATE_PATH, "w", encoding="utf-8") as f:
        f.write("{ 깨진 json")
    on, _, err = ks.status()
    assert on is False and err, "읽기 실패는 fail-open 이어야 한다"
    ks.guard_buy("테스트계좌")          # SystemExit 이 나면 안 된다


def test_sell_paths_do_not_consult_kill_switch():
    """매도·만기청산·손절은 스위치를 타지 않아야 한다(청산 중단 = 위험 증가)."""
    import inspect
    import kiwoom_trader as kt
    import kis_trader as kx

    for mod, name in ((kt, "kiwoom"), (kx, "kis")):
        buy = inspect.getsource(mod.cmd_buy)
        sell = inspect.getsource(mod.cmd_sell)
        assert "kill_switch.guard_buy" in buy, f"{name}: cmd_buy 에 스위치가 없다"
        assert "kill_switch" not in sell, \
            f"{name}: cmd_sell 이 스위치를 참조한다 — 청산은 막으면 안 된다"


# ─────────────────────────────────────────────────────────────────────────────
# 일일 후보수 게이트 (2026-08-27 신설, 모니터 전용)
#   슬롯 10 제약이 비대칭으로 작동해(후보 적은 날 전부 사고, 많은 날 10건만)
#   per-trade +1.72% 우위가 실현 -0.75% 로 뒤집히는 것을 겨냥한 규칙.
#   Phase 1 은 차단하지 않고 기록만 한다 — 그 계약을 못박는다.
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_is_monitor_only_for_now():
    """Phase 1 동안 GATE_ENFORCE 는 False 여야 한다(실수로 켜진 채 배포 방지)."""
    import kiwoom_trader as kt
    assert kt.GATE_ENFORCE is False, (
        "게이트가 차단 모드로 켜져 있다. 모니터 2~4주 관찰 후 사용자 승인으로만 켠다.")
    assert kt.GATE_MIN_CANDIDATES == 12


def test_gate_monitor_never_blocks(tmp_path, monkeypatch):
    import kiwoom_trader as kt
    monkeypatch.setattr(kt, "GATE_LOG", str(tmp_path / "g.csv"))
    monkeypatch.setattr(kt, "GATE_ENFORCE", False)
    # 후보가 기준에 한참 못 미쳐도 모니터 모드면 매수를 막지 않는다
    assert kt._gate_check(0, 5) is True
    assert kt._gate_check(99, 120) is True


def test_gate_enforce_blocks_only_below_threshold(tmp_path, monkeypatch):
    import kiwoom_trader as kt
    monkeypatch.setattr(kt, "GATE_LOG", str(tmp_path / "g.csv"))
    monkeypatch.setattr(kt, "GATE_ENFORCE", True)
    monkeypatch.setattr(kt, "GATE_MIN_CANDIDATES", 12)
    assert kt._gate_check(11, 30) is False, "기준 미만이면 차단해야 한다"
    assert kt._gate_check(12, 30) is True, "기준 이상이면 통과해야 한다"


def test_gate_records_every_day(tmp_path, monkeypatch):
    """차단하든 말든 매일 기록돼야 관찰 데이터가 쌓인다."""
    import csv as _csv
    import kiwoom_trader as kt
    p = tmp_path / "g.csv"
    monkeypatch.setattr(kt, "GATE_LOG", str(p))
    monkeypatch.setattr(kt, "GATE_ENFORCE", False)
    kt._gate_check(3, 20)
    kt._gate_check(40, 60)
    rows = list(_csv.DictReader(open(p, encoding="utf-8-sig")))
    assert len(rows) == 2
    assert rows[0]["blocked"] == "1" and rows[1]["blocked"] == "0"
    assert rows[0]["enforced"] == "0", "모니터 모드가 기록에 남아야 사후 해석이 된다"
