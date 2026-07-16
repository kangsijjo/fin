"""
AI 학습셋 v3 — 풀링 전략 + 종목 피처 + stock.db 외부 피처.

v2 대비 변경:
  1. 매매 풀링: h500_40_MKT + h252_40 + h500_20
  2. stock.db 피처:
     - news_sentiment_7d / news_count_7d      : 종목 뉴스 감성
     - vix, vix_chg_5d, sox_ret_5d ...       : 매크로 레짐
     - crd_remn_rt, crd_remn_chg_5d          : 신용잔고율 (stock.db credit_balance 2022~)
     - for_net_5d_db, ins_net_5d_db          : 수급 5일 누적 (stock.db supply_demand 2015~)
     - rsi_db, macd_hist_db, bb_pct_db       : 기술지표 (stock.db korea_indicators)
  3. stock.db 없으면 해당 피처 NaN (graceful).

저장: ./trades_history_v3.csv
실행: python make_trades_history_v3.py
"""

import os
import sqlite3
from datetime import datetime, timedelta

import bisect
import numpy as np
import pandas as pd

from strategies.daily_loader import load_macro_daily, default_costs, filter_universe
from strategies.high_with_filters import HighWithFiltersStrategy
from strategies.high_52w import FiftyTwoWeekHighStrategy
from strategies.rsi_reversal import RsiReversalStrategy
from strategies.rsi_volume import RsiVolumeStrategy
from strategies.high52w_foreign import High52wForeignStrategy
from strategies.foreign_high import ForeignHighStrategy
from strategies.gc_foreign import GcForeignStrategy
from make_trades_history_v2 import compute_stock_features

OUT = "./trades_history_v3.csv"

STOCK_DB_CANDIDATES = [
    os.getenv("STOCK_DB", ""),
    "../Stock_AI_Project/data/stock.db",
    "C:/fin/Stock_AI_Project/data/stock.db",
]

STRATEGIES = [
    ("h500_40_MKT", HighWithFiltersStrategy(lookback_days=500, holding_days=40,
                     use_market_filter=True, use_volume_filter=False, name="h500_40_MKT")),
    ("h252_40",     FiftyTwoWeekHighStrategy(lookback_days=252, holding_days=40, name="h252_40")),
    ("h500_20",     FiftyTwoWeekHighStrategy(lookback_days=500, holding_days=20, name="h500_20")),
    # ── 라이브 운용 6전략 (2026-07-17 추가) ──────────────────────────────────
    # 목적: IC 가중치(FactorScorer)·AI 학습·paper_audit Drift 비교가 '실제 운용 중인
    # 전략' 기준이 되도록. 종전엔 위 레거시 3전략 수익률로 "어떤 지표가 먹히나"를
    # 학습해 그 잣대로 안C/안D 신호를 채점하는 구조적 불일치가 있었음.
    # 이름은 라이브 신호CSV(paper_signals/kis_paper_signals)와 정확히 일치시킨다
    # — 백테스트 별칭 star_high_52w_20_filt 는 여기서 high_52w_filt 로(클래스·파라미터
    # 동일, strategy_engine 인스턴스와 같은 기본값: 시장게이트+거래량 1.5x).
    # ※ KIS 3전략의 라이브 손절(-15/-10/-26%)은 미적용(레거시와 동일한 '만기 보유
    #   순수익' 규약 유지 — IC 는 신호 품질 측정이므로 청산 오버레이 없이 학습).
    ("high_52w_filt",  HighWithFiltersStrategy(lookback_days=252, holding_days=20,
                        name="high_52w_filt")),
    ("rsi_reversal",   RsiReversalStrategy(name="rsi_reversal")),
    ("rsi_vol",        RsiVolumeStrategy(holding_days=7, name="rsi_vol")),
    ("h52w_for3d_mkt", High52wForeignStrategy(foreign_n=3, holding_days=20,
                        use_market_filter=True, name="h52w_for3d_mkt")),
    ("for_high20_mkt", ForeignHighStrategy(n_days=3, high_period=20, holding_days=20,
                        use_market_filter=True, name="for_high20_mkt")),
    ("gc_for3d",       GcForeignStrategy(fast_ma=20, slow_ma=60, foreign_n=3,
                        holding_days=15, name="gc_for3d")),
]


def open_stock_db():
    for p in STOCK_DB_CANDIDATES:
        if p and os.path.exists(p):
            try:
                con = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
                con.execute("SELECT 1")
                print(f"[stock.db] 연결: {p}")
                return con
            except Exception as e:
                print(f"[stock.db] {p} 열기 실패: {e}")
    print("[stock.db] 없음 — 외부 피처는 NaN 으로 저장")
    return None


def load_news_daily(con):
    """종목·일자별 감성 평균/건수 (전 기간 1회 집계)."""
    q = """SELECT ticker, pubDate AS d, AVG(sentiment) AS sent, COUNT(*) AS n
           FROM news WHERE sentiment IS NOT NULL GROUP BY ticker, pubDate"""
    df = pd.read_sql(q, con)
    df["d"] = df["d"].astype(str).str.replace("-", "").str[:8]
    return df


def load_macro_ind(con):
    """stock.db(과거) + macro_data/indicators.csv(연속 수집분) 병합 — 최신분 우선."""
    parts = []
    if con is not None:
        parts.append(pd.read_sql("SELECT date, indicator, close FROM macro_indicators", con))
    if os.path.exists("./macro_data/indicators.csv"):
        parts.append(pd.read_csv("./macro_data/indicators.csv"))
    if not parts:
        return None
    m = pd.concat(parts, ignore_index=True)
    m["date"] = m["date"].astype(str).str.replace("-", "").str[:8]
    m = m.drop_duplicates(subset=["date", "indicator"], keep="last")
    w = m.pivot_table(index="date", columns="indicator", values="close", aggfunc="last").sort_index()
    out = pd.DataFrame(index=w.index)
    if "VIX" in w:
        out["vix"] = w["VIX"]
        out["vix_chg_5d"] = w["VIX"].pct_change(5, fill_method=None) * 100
    if "SOX" in w:
        out["sox_ret_5d"] = w["SOX"].pct_change(5, fill_method=None) * 100
    if "KRW_USD" in w:
        out["usdkrw_chg_5d"] = w["KRW_USD"].pct_change(5, fill_method=None) * 100
    if "KOSPI" in w:
        out["kospi_ret_20d"] = w["KOSPI"].pct_change(20, fill_method=None) * 100
    out = out.ffill()
    return out.reset_index().rename(columns={"index": "date"})


def load_credit_balance_db(con):
    """stock.db credit_balance (2022~) → 종목별 신용잔고율 시계열.
    반환: {ticker: {"d": [...], "rt": [...]} }  (bisect 고속 조회용)
    """
    try:
        df = pd.read_sql(
            "SELECT date, ticker, credit_remain_rt FROM credit_balance "
            "WHERE credit_remain_rt IS NOT NULL ORDER BY ticker, date", con)
        df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
        result = {}
        for tk, g in df.groupby("ticker"):
            result[tk] = {"d": g["date"].tolist(), "rt": g["credit_remain_rt"].tolist()}
        print(f"[credit_balance] {len(result):,}종목 로드 (2022~)")
        return result
    except Exception as e:
        print(f"[credit_balance] 로드 실패: {e}")
        return {}


def load_supply_demand_db(con):
    """stock.db supply_demand (2015~) → 외국인/기관 순매수 시계열.
    반환: {ticker: {"d": [...], "f": [...], "i": [...]} }
    """
    try:
        df = pd.read_sql(
            "SELECT date, ticker, foreign_net_value, institution_net_value "
            "FROM supply_demand ORDER BY ticker, date", con)
        df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
        result = {}
        for tk, g in df.groupby("ticker"):
            result[tk] = {
                "d": g["date"].tolist(),
                "f": g["foreign_net_value"].tolist(),
                "i": g["institution_net_value"].tolist(),
            }
        print(f"[supply_demand_db] {len(result):,}종목 로드 (2015~)")
        return result
    except Exception as e:
        print(f"[supply_demand_db] 로드 실패: {e}")
        return {}


def load_tech_indicators_db(con):
    """stock.db korea_indicators → RSI / MACD hist / BB 위치 (사전 계산 기술지표).
    반환: {ticker: {"d":[], "rsi":[], "macd_h":[], "bb_pct":[]} }
    bb_pct = (Close - bb_lower) / (bb_upper - bb_lower) : BB 내 상대 위치 0~1
    """
    try:
        df = pd.read_sql(
            "SELECT date, ticker, rsi, macd_hist, bb_upper, bb_lower, Close "
            "FROM korea_indicators "
            "WHERE rsi IS NOT NULL ORDER BY ticker, date", con)
        df["date"] = df["date"].astype(str).str.replace("-", "").str[:8]
        bw = df["bb_upper"] - df["bb_lower"]
        df["bb_pct"] = (df["Close"] - df["bb_lower"]) / bw.where(bw > 0)
        result = {}
        for tk, g in df.groupby("ticker"):
            result[tk] = {
                "d":      g["date"].tolist(),
                "rsi":    g["rsi"].tolist(),
                "macd_h": g["macd_hist"].tolist(),
                "bb_pct": g["bb_pct"].tolist(),
            }
        print(f"[korea_indicators] {len(result):,}종목 기술지표 로드")
        return result
    except Exception as e:
        print(f"[korea_indicators] 로드 실패: {e}")
        return {}


# ─── 개별 피처 조회 헬퍼 ───────────────────────────────────────────────────

def news_window_feature(news_by_code, code, sig_date, days=7):
    """신호일 직전 7일 감성 평균/건수."""
    if not news_by_code:
        return np.nan, np.nan
    g = news_by_code.get(code)
    if g is None:
        return np.nan, 0
    d0 = (datetime.strptime(sig_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    lo = bisect.bisect_left(g["d"], d0)
    hi = bisect.bisect_right(g["d"], sig_date)
    if lo >= hi:
        return np.nan, 0
    n = sum(g["n"][lo:hi])
    s = sum(se * nn for se, nn in zip(g["sent"][lo:hi], g["n"][lo:hi]))
    return float(s / n), int(n)


def _bisect_val(series_dict, code, date_str, key):
    """시계열 dict 에서 date_str 이하 최신값 조회."""
    g = series_dict.get(code)
    if not g:
        return np.nan
    idx = bisect.bisect_right(g["d"], date_str) - 1
    if idx < 0:
        return np.nan
    v = g[key][idx]
    return float(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else np.nan


def credit_features(credit_db, code, sig_date):
    """신용잔고율 + 5일 변화."""
    rt = _bisect_val(credit_db, code, sig_date, "rt")
    if np.isnan(rt):
        return np.nan, np.nan
    # 5영업일 전 값 (달력일 -8일 이내에서 가장 가까운 값)
    d_prev = (datetime.strptime(sig_date, "%Y%m%d") - timedelta(days=8)).strftime("%Y%m%d")
    rt_prev = _bisect_val(credit_db, code, d_prev, "rt")
    chg5 = (rt - rt_prev) if not np.isnan(rt_prev) else np.nan
    return rt, chg5


def supply_features_db(sd_db, code, sig_date, days=5):
    """외국인/기관 순매수 직전 5영업일 합계 (supply_demand DB)."""
    g = sd_db.get(code)
    if not g:
        return np.nan, np.nan
    hi = bisect.bisect_right(g["d"], sig_date)
    lo = max(0, hi - days)
    if lo >= hi:
        return np.nan, np.nan
    f_sum = sum(v for v in g["f"][lo:hi] if v is not None)
    i_sum = sum(v for v in g["i"][lo:hi] if v is not None)
    return float(f_sum), float(i_sum)


def tech_features(tech_db, code, sig_date):
    """RSI / MACD hist / BB 위치."""
    rsi    = _bisect_val(tech_db, code, sig_date, "rsi")
    macd_h = _bisect_val(tech_db, code, sig_date, "macd_h")
    bb_pct = _bisect_val(tech_db, code, sig_date, "bb_pct")
    return rsi, macd_h, bb_pct


# ─── main ──────────────────────────────────────────────────────────────────

def main():
    print("=== trades_history v3 (풀링 + 외부 피처) ===\n")
    df = load_macro_daily()
    df = filter_universe(df)
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)
    costs = default_costs()

    # 1) 풀링 백테스트
    all_trades = []
    for sid, strat in STRATEGIES:
        ts = strat.backtest(df, costs)
        for t in ts:
            all_trades.append((sid, t))
        print(f"  {sid:14s}: {len(ts):,} 매매")
    print(f"[pool] 합계 {len(all_trades):,} 매매")

    # 2) 종목 피처 (v2 재사용)
    stock_feat = compute_stock_features(df)
    stock_feat["date"] = stock_feat["date"].astype(str)
    stock_feat["code"] = stock_feat["code"].astype(str).str.zfill(6)
    sf_map = stock_feat.set_index(["code", "date"]).to_dict("index")

    # 3) 외부 피처 (stock.db)
    con = open_stock_db()
    news_by_code, macro_ind, credit_db, sd_db, tech_db = None, None, {}, {}, {}

    if con is not None:
        # 뉴스 감성
        try:
            news = load_news_daily(con)
            print(f"[news] {len(news):,} 종목-일 집계")
            news = news.sort_values(["ticker", "d"])
            news_by_code = {
                t: {"d": g["d"].tolist(), "sent": g["sent"].tolist(), "n": g["n"].tolist()}
                for t, g in news.groupby("ticker")
            }
        except Exception as e:
            print(f"[news] 집계 실패: {e}")

        # 신용잔고 (2022~)
        credit_db = load_credit_balance_db(con)

        # 수급 DB (2015~)
        sd_db = load_supply_demand_db(con)

        # 기술지표 DB
        tech_db = load_tech_indicators_db(con)

    # 매크로 (stock.db + indicators.csv 병합)
    try:
        mi = load_macro_ind(con)
        if mi is not None:
            macro_ind = mi.set_index("date").to_dict("index")
            print(f"[macro_ind] {len(macro_ind):,} 일")
    except Exception as e:
        print(f"[macro_ind] 실패: {e}")

    # signal_date = entry 직전 영업일
    code_dates = {c: sorted(g["date"].tolist()) for c, g in df.groupby("code")}

    def sig_date_of(code, entry_date):
        ds = code_dates.get(code, [])
        try:
            i = ds.index(entry_date)
            return ds[i - 1] if i > 0 else entry_date
        except ValueError:
            return entry_date

    rows = []
    feat_keys = ["rsi14", "atr_pct", "vol_ratio", "tv_ratio", "for_5d", "ins_5d", "mcap_class"]
    macro_keys = ["vix", "vix_chg_5d", "sox_ret_5d", "usdkrw_chg_5d", "kospi_ret_20d"]

    for sid, t in all_trades:
        sd = sig_date_of(t.code, t.entry_date)
        sf = sf_map.get((t.code, sd), {})
        sent, n_news = news_window_feature(news_by_code, t.code, sd)
        mi_row = macro_ind.get(sd, {}) if macro_ind else {}

        # 신용잔고 (stock.db credit_balance 2022~)
        crd_rt, crd_chg5 = credit_features(credit_db, t.code, sd)

        # 수급 DB (supply_demand 2015~) — pykrx CSV 피처와 병행, DB 쪽이 더 장기
        for_net5_db, ins_net5_db = supply_features_db(sd_db, t.code, sd)

        # 기술지표 DB
        rsi_db, macd_h_db, bb_pct_db = tech_features(tech_db, t.code, sd)

        row = {
            "date": sd, "code": t.code, "strategy": sid,
            "entry_date": t.entry_date, "exit_date": t.exit_date,
            "net_pct": round(t.net_pct, 4), "gross_pct": round(t.gross_pct, 4),
            "score_tv": t.score,
            # 뉴스
            "news_sent_7d": None if pd.isna(sent) else round(sent, 4),
            "news_cnt_7d":  n_news if not pd.isna(n_news) else None,
            # 신용잔고 (stock.db, 2022~)
            "crd_remn_rt":      None if np.isnan(crd_rt) else round(crd_rt, 4),
            "crd_remn_chg_5d":  None if np.isnan(crd_chg5) else round(crd_chg5, 4),
            # 수급 DB (stock.db, 2015~)
            "for_net5_db":  None if np.isnan(for_net5_db) else round(for_net5_db, 0),
            "ins_net5_db":  None if np.isnan(ins_net5_db) else round(ins_net5_db, 0),
            # 기술지표 DB (stock.db korea_indicators)
            "rsi_db":       None if np.isnan(rsi_db) else round(rsi_db, 2),
            "macd_hist_db": None if np.isnan(macd_h_db) else round(macd_h_db, 4),
            "bb_pct_db":    None if np.isnan(bb_pct_db) else round(bb_pct_db, 4),
        }
        for k in feat_keys:
            v = sf.get(k)
            row[k] = None if v is None or pd.isna(v) else round(float(v), 4)
        for k in macro_keys:
            v = mi_row.get(k)
            row[k] = None if v is None or pd.isna(v) else round(float(v), 4)
        rows.append(row)

    out = pd.DataFrame(rows).sort_values("date")

    # score_tv 를 라이브(factor_scorer.prepare_price_features:233-235)와 '정확히 동일하게'
    # 신호일 '전체 유니버스' 거래대금 순위(1=최대)로 맞춘다. df(load_macro_daily, 전 종목·전일자)에서
    # 날짜별 순위를 구해 (signal_date=out.date, code)로 매핑.
    #   ※ t.score(거래대금 원시값 ~1e9~1e12)도, 매매종목 '내부' 순위(out.groupby)도 라이브의
    #     '유니버스' 순위와 분모가 달라 train/live 스케일이 어긋났음 — 2026-06-30 수정, 재학습 필요.
    _rk = df[["date", "code", "trading_value"]].copy()
    _rk["date"] = _rk["date"].astype(str)
    _rk["code"] = _rk["code"].astype(str).str.zfill(6)
    _rk["tv_rank"] = _rk.groupby("date")["trading_value"].rank(ascending=False, method="min")
    _rk = _rk.drop_duplicates(["date", "code"])[["date", "code", "tv_rank"]]
    out["date"] = out["date"].astype(str)
    out["code"] = out["code"].astype(str).str.zfill(6)
    out = out.merge(_rk, on=["date", "code"], how="left")
    out["score_tv"] = out["tv_rank"]
    out = out.drop(columns=["tv_rank"])

    # 키움 백필 피처 (신용잔고·프로그램매매) — kiwoom_backfill.py merge 산출물
    KW_FEAT = "./ai_data/kiwoom_hist_features.csv"
    if os.path.exists(KW_FEAT):
        kw = pd.read_csv(KW_FEAT, dtype={"code": str, "date": str})
        kw["code"] = kw["code"].str.zfill(6)
        # crd_remn_rt/crd_remn_chg_5d 가 out·kw 양쪽에 있어 단순 merge 하면 _x/_y 로 갈려
        # ai_trainer_v4 가 평문 컬럼명을 못 찾아 credit 그룹이 학습에서 영구 누락됐음.
        # coalesce 로 평문 컬럼 유지(2026-06-30 수정 — 재학습 필요).
        out = out.merge(kw, on=["code", "date"], how="left", suffixes=("", "_kw"))
        for _c in ["crd_remn_rt", "crd_remn_chg_5d"]:
            if _c + "_kw" in out.columns:
                out[_c] = out[_c].fillna(out[_c + "_kw"])
                out = out.drop(columns=[_c + "_kw"])
        cov = out.get("crd_remn_rt")
        if cov is not None:
            print(f"[kiwoom_feat] 병합 — 신용 커버리지 {cov.notna().mean()*100:.0f}%")

    out.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n[saved] {OUT}, {len(out):,} 행 "
          f"({out['date'].min()} ~ {out['date'].max()})")

    # 커버리지 리포트
    for col, label in [
        ("news_sent_7d",  "뉴스 감성"),
        ("crd_remn_rt",   "신용잔고율(DB)"),
        ("for_net5_db",   "외국인수급(DB)"),
        ("rsi_db",        "RSI(DB)"),
    ]:
        if col in out:
            pct = out[col].notna().mean() * 100
            print(f"  {label} 피처 커버리지: {pct:.1f}%")


if __name__ == "__main__":
    main()
