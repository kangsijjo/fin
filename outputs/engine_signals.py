# -*- coding: utf-8 -*-
"""
engine_signals.py — 백테스트 엔진 전략(55개)으로 '오늘 신호'를 만든다. (2026-10-21)

왜: 모의매매 신호는 전략마다 손으로 짠 감지 함수(live_signal / kis_live_signal)로 만들어 왔다. 대시보드에서
    아무 전략이나 고르게 되면서, 손으로 짠 함수가 없는 전략은 **백테스트와 같은 코드**로 신호를 만든다.
방법: 모든 엔진 전략은 신호 열(df["signal"])을 만든 뒤 _swing_base 의 매매 생성 함수(_make_trades_for_signals /
      _make_trades_with_stops)를 부른다. 그 두 함수를 잠시 '신호표 가로채기'로 바꿔 df 를 받아 오고,
      마지막 날짜의 신호 행만 뽑는다 → 백테스트와 조건이 1:1 같다(가격 이후 정보는 쓰지 않음).
      손으로 짠 감지 함수가 있는 기존 5개 전략은 검증된 그 함수를 그대로 쓴다(이 모듈은 나머지용).
보조 데이터: 개인 순매수·신용잔고·공매도 비중이 필요한 전략은 **오늘 값이 있어야** 신호가 맞다.
      없으면 그 전략은 오늘 건너뛰고 이유를 돌려준다(가짜 0 으로 신호를 만들지 않는다).
"""
import os
import sys
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_TRADE_FUNCS = ("_make_trades_for_signals", "_make_trades_with_stops")


@contextmanager
def _capture_signal_frame(box):
    """strategies.* 모듈에 import 된 매매 생성 함수를 '신호표 저장 후 빈 목록 반환'으로 잠시 바꾼다."""
    import strategies._swing_base as SB
    real = {n: getattr(SB, n) for n in _TRADE_FUNCS}

    def fake(df_with_sig, *a, **k):
        box["df"] = df_with_sig
        box["sig_col"] = k.get("sig_col", "signal")
        return []

    patched = []
    for mod in list(sys.modules.values()):
        name = getattr(mod, "__name__", "") or ""
        if not (name == "strategies._swing_base" or name.startswith("strategies.")):
            continue
        for n in _TRADE_FUNCS:
            if getattr(mod, n, None) is real[n]:
                setattr(mod, n, fake)
                patched.append((mod, n))
    try:
        yield
    finally:
        for mod, n in patched:
            setattr(mod, n, real[n])


def signal_frame(strategy, df):
    """전략의 신호표(df + signal 열). 매매 생성 단계에 닿지 못하면 None."""
    import strategy_engine as E
    box = {}
    with _capture_signal_frame(box):
        strategy.backtest(df.copy(), E.DEFAULT_COSTS)
    return box.get("df"), box.get("sig_col", "signal")


def aux_ready(strategy, df, last_date, all_dates=None):
    """오늘 보조 데이터가 있는가 → (True, '') 또는 (False, 이유).
    [2026-10-23] 공개 지연 반영판(data_lag>0, 예 h52w_cred_dec_lag2)은 신용·공매도를 **lag 거래일 전** 값으로 쓰므로
    그날짜 값만 있으면 된다(실매매에서도 신호가 나옴)."""
    import period_backtest as P
    need = P.aux_needs(strategy)
    if not need:
        return True, ""
    lag = int(getattr(strategy, "data_lag", 0) or 0)
    lag_date = str(last_date)
    if lag and all_dates:
        ad = [str(x) for x in all_dates]
        i = ad.index(str(last_date)) if str(last_date) in ad else len(ad) - 1
        lag_date = ad[max(0, i - lag)]
    today = df[df["date"].astype(str) == str(last_date)]
    for k in need:
        if k in ("foreign", "inst"):
            col = "foreign_net" if k == "foreign" else "inst_net"
            if col not in today or today[col].notna().mean() < 0.5:
                return False, f"오늘({last_date}) {P._AUX_LABEL[k]} 값이 없음"
        elif k == "individual":
            if "individual_net" not in today or today["individual_net"].notna().mean() < 0.5:
                n = _fill_individual_today(df, last_date)      # 18:30 엔 DB 에 아직 없다 → KRX 에서 바로 받아 채움
                today = df[df["date"].astype(str) == str(last_date)]
                if "individual_net" not in today or today["individual_net"].notna().mean() < 0.5:
                    return False, (f"오늘({last_date}) 개인 순매수를 받지 못함(KRX {n}종목) — "
                                   "이 전략은 오늘 신호를 만들지 않음")
        elif k == "short":
            f = os.path.join(HERE, "macro_data", "shortfund_kosdaq", f"{lag_date}.csv")
            if not os.path.exists(f):
                return False, (f"{lag_date} 공매도 비중 파일이 없음 — KRX 공표가 늦어 다음날 채워짐"
                               + ("" if lag else " (공개 지연 반영판 h52w_short_dec_lag2/_wait2 를 쓰면 실매매 가능)"))
        elif k == "credit":
            try:
                import sqlite3
                from fin_paths import STOCK_DB
                con = sqlite3.connect(f"file:{STOCK_DB}?mode=ro", uri=True, timeout=30)
                n = con.execute("SELECT count(*) FROM credit_balance WHERE date=?",
                                (f"{lag_date[:4]}-{lag_date[4:6]}-{lag_date[6:]}",)).fetchone()[0]
                con.close()
            except Exception:
                n = 0
            if n < 100:
                return False, (f"{lag_date} 신용잔고가 없음 — 1~2거래일 늦게 공개됨"
                               + ("" if lag else " (공개 지연 반영판 h52w_cred_dec_lag2/_wait2 를 쓰면 실매매 가능)"))
    return True, ""


def _fill_individual_today(df, last_date):
    """KRX(pykrx)에서 그날 개인 순매수를 받아 df 의 그날 행에 채운다(메모리만 — DB 는 20:00 자동수집이 채움).
    backfill_individual 의 검증된 호출(_pull_individual: 재시도·스로틀)을 그대로 쓴다. → 채운 종목 수."""
    try:
        import backfill_individual as BI
        from pykrx import stock
        vals = {}
        for mk in BI.MARKETS:
            vals.update(BI._pull_individual(stock, str(last_date), mk) or {})
        if not vals:
            return 0
        if "individual_net" not in df.columns:
            df["individual_net"] = float("nan")
        m = df["date"].astype(str) == str(last_date)
        df.loc[m, "individual_net"] = df.loc[m, "code"].astype(str).str.zfill(6).map(vals)
        return int(df.loc[m, "individual_net"].notna().sum())
    except Exception:
        return 0


def today_signals(strategy, df, last_date, all_dates, name_cache, live_name, holding, is_excluded, offset_date):
    """엔진 전략 → 신호 CSV 행 목록(live_signal 과 같은 열). 보조 데이터가 없으면 ([], 이유)."""
    import period_backtest as P
    ok, why = aux_ready(strategy, df, last_date, all_dates)
    if not ok:
        return [], why
    # 계산량 줄이기: 전략이 필요로 하는 과거 + 여유만 남긴다
    warm = P.warmup_days(strategy) + 60
    i = all_dates.index(str(last_date)) if str(last_date) in all_dates else len(all_dates) - 1
    lo = all_dates[max(0, i - warm)]
    sub = df[(df["date"].astype(str) >= lo) & (df["date"].astype(str) <= str(last_date))]
    sdf, col = signal_frame(strategy, sub)
    if sdf is None or col not in sdf:
        return [], "신호표를 만들지 못함(전략 코드가 매매 생성 단계에 닿지 않음)"
    t = sdf[(sdf["date"].astype(str) == str(last_date)) & sdf[col].fillna(False).astype(bool)]
    tgt = offset_date(all_dates, last_date, holding)
    rows = []
    for _, r in t.iterrows():
        code = str(r["code"]).zfill(6)
        nm = str(r.get("name", "") or "").strip() or name_cache.get(code, "")
        if is_excluded(nm, code):
            continue
        rows.append({"signal_date": str(last_date), "code": code, "name": nm,
                     "entry_price_close": float(r["close"]), "target_exit_date": tgt,
                     "strategy": live_name, "holding_days": int(holding),
                     "lookback_high": 0.0, "market_strong": bool(r.get("mkt_strong", False))})
    return rows, ""


def _drop_ca_rows(rows, strategy, df, last_date, live_name, log=print):
    """[2026-10-23] 백테스트와 같은 규칙 — 신호 창(lookback)에 액면분할·병합·감자가 낀 종목, 긴 거래정지 뒤 재개 10거래일
    (정리매매 포함) 종목은 오늘 신호에서 뺀다. strategies/base.py CA_LOOKBACK_FILTER 와 같은 스위치."""
    try:
        import strategies.base as B
        import strategies._swing_base as SB
        if not B.CA_LOOKBACK_FILTER or strategy is None or "change_pct" not in df.columns:
            return rows
        ca_map, dates, resume = SB._ca_map_cached(df)
        L = SB.signal_lookback(strategy)
        keep = [r for r in rows                                  # 행·df 모두 6자리 코드
                if not SB.ca_in_lookback(ca_map, dates, str(r["code"]), last_date, L)
                and not SB.halt_resume_recent(resume, dates, str(r["code"]), last_date)]
        if len(keep) < len(rows):
            gone = sorted({str(r["code"]).zfill(6) for r in rows} - {str(r["code"]).zfill(6) for r in keep})
            log(f"  [{live_name}] 최근 {L}거래일 안 분할·병합 또는 긴 정지 뒤 재개(정리매매 등) 종목 "
                f"{len(gone)}개 제외: {', '.join(gone[:8])}")
        return keep
    except Exception as e:
        log(f"  [{live_name}] 기업행위 필터 건너뜀: {type(e).__name__}: {e}")
        return rows


def run_configured(account, df, last_date, all_dates, name_cache, known, is_excluded, offset_date, log=print):
    """설정(live_config)의 계좌 전략 전부 신호 생성.
    known: {라이브이름: 감지함수(df,last_date,all_dates,name_cache)} — 손으로 짠 검증된 감지기가 있는 전략.
    → (전략별 [(라이브이름, 행목록)], 건너뛴 전략 [(라이브이름, 이유)])"""
    import live_config as LC
    import strategy_engine as E
    acc = LC.account(account)
    smap = {s.name: s for s in E.ALL_STRATEGIES}
    out, skipped = [], []
    for ln in acc["priority"]:
        try:
            if ln in known:
                rows = known[ln](df.copy(), last_date, all_dates, name_cache)
                why = ""
            else:
                eng = acc["engine"][ln]
                if eng not in smap:
                    rows, why = [], f"엔진에 없는 전략({eng})"
                else:
                    rows, why = today_signals(smap[eng], df, last_date, all_dates, name_cache, ln,
                                              acc["holding"][ln], is_excluded, offset_date)
        except Exception as e:
            rows, why = [], f"신호 계산 실패: {type(e).__name__}: {str(e)[:120]}"
        if rows:
            rows = _drop_ca_rows(rows, smap.get(acc["engine"].get(ln, "")), df, last_date, ln, log)
        if why:
            skipped.append((ln, why))
            log(f"  [{ln}] 건너뜀 — {why}")
        out.append((ln, rows))
    return out, skipped
