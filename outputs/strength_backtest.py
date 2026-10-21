# -*- coding: utf-8 -*-
"""
strength_backtest.py — 과거 신호의 '강도(score_ic)'를 소급 계산한다. (2026-10-18, 기간 백테스트 강도 비교용)

왜: 라이브 강도(factor_scorer.score_ic)는 오늘 신호에만 기록된다. 기간 백테스트에서 "강도 ≥ X 였다면?"을
    보려면 과거 신호마다 **그날 기준** 피처를 만들고, **그때까지 알 수 있었던** 가중치로 채점해야 한다.

지키는 것
  1) 피처 정의 = make_trades_history_v3(강도 가중치의 학습 데이터)와 같다 — 같은 함수(compute_stock_features)와
     같은 규칙(신호일 이하 최신값, 5행 합, 7일 뉴스, 유니버스 거래대금 순위, 프로그램 5일 양수합/거래대금)을
     벡터화해 계산한다. 학습과 채점이 같은 잣대여야 IC 가 의미를 가진다(테스트로 대조).
  2) 워크포워드: Y년 신호는 trades_history 중 **Y년 1월 1일 이전 진입분**으로만 IC·백분위 기준을 만든다.
     라이브 6전략 행이 2,000건 이상이면 그 행만(factor_scorer 와 동일), 학습 표본 < 2,000 이면 그 해는 채점 안 함.
  3) 점수식 = factor_scorer.score_ic(available-only 정규화, 커버리지 40% 미만이면 전체 분모, 0~10 절단).

캐시: db/period_bt/strength_feat.pkl — (code, 신호일) 피처. 가격 마지막 날짜·피처 버전이 같을 때만 재사용,
      없는 쌍만 새로 계산해 덧붙인다(같은 날 여러 번 시험하면 두 번째부터 빠르다).
한계: 신용잔고는 1~2일 늦게 공개되지만 학습 데이터(trades_history)도 당일 값을 쓰므로 그대로 따른다(§56).
"""
import os
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "db", "period_bt", "strength_feat.pkl")
HIST_CSV = os.path.join(HERE, "trades_history_v3.csv")
FEATURE_VERSION = 1
MIN_TRAIN = 2000


def _ic_constants():
    from factor_scorer import IC_FEATURES, MIN_IC_COVERAGE, LIVE_STRATEGY_NAMES
    return list(IC_FEATURES), float(MIN_IC_COVERAGE), set(LIVE_STRATEGY_NAMES)


def _stock_db():
    from fin_paths import STOCK_DB
    return sqlite3.connect(f"file:{STOCK_DB}?mode=ro", uri=True, timeout=60)


def _iso(d8):
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"


def _d8(s):
    return s.astype(str).str.replace("-", "", regex=False).str[:8]


def sig_dates_for(df, trades):
    """매매 → (code, 신호일=그 종목의 진입일 직전 거래일). make_trades_history_v3.sig_date_of 와 같은 규칙."""
    codes = {str(t.code).zfill(6) for t in trades}
    cd = {c: g for c, g in df[df["code"].isin(codes)].groupby("code")["date"]}
    cd = {c: np.sort(g.astype(str).to_numpy()) for c, g in cd.items()}
    out = []
    for t in trades:
        c, e = str(t.code).zfill(6), str(t.entry_date)
        ds = cd.get(c)
        if ds is None:
            out.append(e)
            continue
        i = np.searchsorted(ds, e)
        out.append(ds[i - 1] if i < len(ds) and ds[i] == e and i > 0 else e)
    return out


# ── 피처 계산(벡터화) ───────────────────────────────────────────────────────
def _asof(left, right, cols):
    """left(code, d) 에 right(code, d, cols) 의 'd 이하 최신 행'을 붙인다(종목별)."""
    L = left.copy()
    L["_k"] = L["date"].astype(np.int64)
    R = right.copy()
    R["_k"] = R["date"].astype(np.int64)
    L = L.sort_values("_k")
    R = R.sort_values("_k")
    m = pd.merge_asof(L, R[["code", "_k"] + cols], on="_k", by="code", direction="backward")
    return m.drop(columns=["_k"])


def compute_features(df, pairs, con=None, log=print):
    """pairs: DataFrame[code, date(신호일 YYYYMMDD)] → 같은 행 순서 + IC 피처 열."""
    from make_trades_history_v2 import compute_stock_features
    from make_trades_history_v3 import load_macro_ind
    from strategies.daily_loader import filter_universe

    P = pairs[["code", "date"]].drop_duplicates().reset_index(drop=True)
    if P.empty:
        return P
    codes = sorted(P["code"].unique())
    dmin = P["date"].min()
    lo = (datetime.strptime(dmin, "%Y%m%d") - timedelta(days=60)).strftime("%Y%m%d")
    own = con is None
    con = con or _stock_db()
    try:
        # 1) 가격 기반(종목 피처) — v2 함수 그대로. 필요한 종목·기간만.
        #    RSI·ATR 은 Wilder 지수평활이라 시작점에 따라 값이 달라진다 → 400일(약 270거래일) 여유를 둬
        #    학습 데이터(전 기간으로 계산)와 오차를 없앤다(60일 여유에선 rsi14 일치율 78% → 실측 검증).
        lo_px = (datetime.strptime(dmin, "%Y%m%d") - timedelta(days=400)).strftime("%Y%m%d")
        sub = df[df["code"].isin(codes) & (df["date"] >= lo_px)]
        sf = compute_stock_features(sub)
        sf["date"] = sf["date"].astype(str)
        X = P.merge(sf[["code", "date", "rsi14", "atr_pct", "vol_ratio", "tv_ratio", "for_5d", "ins_5d"]],
                    on=["code", "date"], how="left")
        # 2) 거래대금 순위(유니버스 기준, v3 와 동일하게 filter_universe 후 날짜별 순위)
        dates = set(P["date"])
        u = filter_universe(df[df["date"].isin(dates)].copy())
        u = u[["date", "code", "trading_value"]].drop_duplicates(["date", "code"])
        u["score_tv"] = u.groupby("date")["trading_value"].rank(ascending=False, method="min")
        X = X.merge(u[["date", "code", "score_tv", "trading_value"]], on=["date", "code"], how="left")
        # 3) 매크로(날짜별)
        mi = load_macro_ind(con)
        if mi is not None:
            mi["date"] = mi["date"].astype(str)
            X = X.merge(mi[["date"] + [c for c in ("vix", "vix_chg_5d", "sox_ret_5d", "usdkrw_chg_5d", "kospi_ret_20d")
                                       if c in mi.columns]], on="date", how="left")
        log(f"  [강도] 가격·매크로 피처 {len(X):,}쌍")
        q_codes = ",".join("'" + c + "'" for c in codes)
        # 4) 수급 DB: 신호일 이하 최근 5행 합(None 은 0으로 — supply_features_db 와 동일)
        sd = pd.read_sql(f"SELECT date, ticker AS code, foreign_net_value AS f, institution_net_value AS i "
                         f"FROM supply_demand WHERE date >= ? AND ticker IN ({q_codes})", con, params=(_iso(lo),))
        if len(sd):
            sd["date"] = _d8(sd["date"])
            sd = sd.sort_values(["code", "date"])
            g = sd.groupby("code")
            sd["for_net5_db"] = g["f"].transform(lambda s: s.fillna(0).rolling(5, min_periods=1).sum())
            sd["ins_net5_db"] = g["i"].transform(lambda s: s.fillna(0).rolling(5, min_periods=1).sum())
            X = _asof(X, sd, ["for_net5_db", "ins_net5_db"])
        # 5) 기술지표 DB(korea_indicators): 신호일 이하 최신 행
        ti = pd.read_sql(f"SELECT date, ticker AS code, rsi AS rsi_db, macd_hist AS macd_hist_db, bb_upper, bb_lower, Close "
                         f"FROM korea_indicators WHERE rsi IS NOT NULL AND date >= ? AND ticker IN ({q_codes})",
                         con, params=(_iso(lo),))
        if len(ti):
            ti["date"] = _d8(ti["date"])
            bw = ti["bb_upper"] - ti["bb_lower"]
            ti["bb_pct_db"] = (ti["Close"] - ti["bb_lower"]) / bw.where(bw > 0)
            X = _asof(X, ti, ["rsi_db", "macd_hist_db", "bb_pct_db"])
        # 6) 신용잔고율: 신호일 이하 최신 + 키움 백필 CSV 로 결측 보완(v3 와 동일)
        cr = pd.read_sql(f"SELECT date, ticker AS code, credit_remain_rt AS crd_remn_rt FROM credit_balance "
                         f"WHERE credit_remain_rt IS NOT NULL AND date >= ? AND ticker IN ({q_codes})",
                         con, params=(_iso(lo),))
        if len(cr):
            cr["date"] = _d8(cr["date"])
            X = _asof(X, cr, ["crd_remn_rt"])
        kw = os.path.join(HERE, "ai_data", "kiwoom_hist_features.csv")
        if os.path.exists(kw):
            k = pd.read_csv(kw, dtype={"code": str, "date": str})
            if "crd_remn_rt" in k.columns:
                k["code"] = k["code"].str.zfill(6)
                X = X.merge(k[["code", "date", "crd_remn_rt"]].rename(columns={"crd_remn_rt": "_kw"}),
                            on=["code", "date"], how="left")
                X["crd_remn_rt"] = (X["crd_remn_rt"] if "crd_remn_rt" in X else np.nan)
                X["crd_remn_rt"] = X["crd_remn_rt"].fillna(X["_kw"])
                X = X.drop(columns=["_kw"])
        # 7) 프로그램 매매: 신호일 포함 최근 5행 양수합 / 당일 거래대금(v3·factor_scorer 와 동일)
        pt = pd.read_sql(f"SELECT ticker AS code, date, prm_net_amt FROM program_trading "
                         f"WHERE prm_net_amt IS NOT NULL AND ticker IN ({q_codes})", con)
        if len(pt):
            pt["date"] = _d8(pt["date"])
            pt = pt.drop_duplicates(["code", "date"]).sort_values(["code", "date"])
            pt["_raw"] = pt.groupby("code")["prm_net_amt"].transform(
                lambda s: s.clip(lower=0).rolling(5, min_periods=5).sum())
            X = X.merge(pt[["code", "date", "_raw"]], on=["code", "date"], how="left")
            X["prm_net_5d_ratio"] = X["_raw"] / X["trading_value"].where(X["trading_value"] > 0)
            X = X.drop(columns=["_raw"])
        # 8) 뉴스 감성: 신호일 포함 직전 7일(달력) 가중평균 — news_window_feature 와 동일
        nw = pd.read_sql("SELECT ticker AS code, pubDate AS d, AVG(sentiment) AS sent, COUNT(*) AS n "
                         "FROM news WHERE sentiment IS NOT NULL GROUP BY ticker, pubDate", con)
        if len(nw):
            nw["d"] = _d8(nw["d"])
            nw = nw[nw["code"].isin(codes)].sort_values(["code", "d"])
            val = np.full(len(X), np.nan)
            Xi = X.reset_index(drop=True)
            for c, g in nw.groupby("code"):
                idx = np.flatnonzero(Xi["code"].to_numpy() == c)
                if not len(idx):
                    continue
                dd = g["d"].to_numpy()
                cs_n = np.concatenate([[0], np.cumsum(g["n"].to_numpy(float))])
                cs_s = np.concatenate([[0], np.cumsum((g["sent"] * g["n"]).to_numpy(float))])
                sig = Xi["date"].to_numpy()[idx]
                d0 = np.array([(datetime.strptime(s, "%Y%m%d") - timedelta(days=7)).strftime("%Y%m%d") for s in sig])
                a = np.searchsorted(dd, d0, side="left")
                b = np.searchsorted(dd, sig, side="right")
                n = cs_n[b] - cs_n[a]
                with np.errstate(invalid="ignore", divide="ignore"):
                    val[idx] = np.where(n > 0, (cs_s[b] - cs_s[a]) / n, np.nan)
            X = Xi
            X["news_sent_7d"] = val
    finally:
        if own:
            con.close()
    X = X.drop(columns=[c for c in ("trading_value",) if c in X.columns])
    return X


def features_cached(df, pairs, log=print):
    """캐시에서 꺼내고 없는 쌍만 계산해 덧붙인다. → DataFrame[code, date, 피처…]"""
    # [2026-10-22] 캐시를 날마다 버리지 않는다 — 과거 날짜의 피처는 바뀌지 않으므로 유지하고, 최근 15거래일 쌍만
    # 다시 계산한다(잠정 수급의 확정치 교체·신용 공표 지연 등으로 최근 값은 바뀔 수 있음). 강도 v2 학습이 매일
    # 수만 쌍을 재계산하지 않게 하려는 것.
    key = (FEATURE_VERSION,)
    import glob as _glob                  # 기준 = 실제 최신 거래일(잘라 넘긴 df 의 끝이 아니라 일봉 폴더 전체)
    dates = sorted(os.path.basename(f)[:8] for f in _glob.glob(os.path.join(HERE, "macro_data", "daily", "*.csv"))
                   if os.path.basename(f)[:8].isdigit())
    recent = dates[-15] if len(dates) >= 15 else "00000000"
    have = None
    try:
        obj = pd.read_pickle(CACHE)
        if obj.get("key") == key or (isinstance(obj.get("key"), tuple) and obj["key"][:1] == key):
            have = obj["df"]
            have = have[have["date"].astype(str) < recent]
    except Exception:
        pass
    need = pairs[["code", "date"]].drop_duplicates()
    if have is not None and len(have):
        need = need.merge(have[["code", "date"]], on=["code", "date"], how="left", indicator=True)
        need = need[need["_merge"] == "left_only"].drop(columns="_merge")
    if len(need):
        log(f"  [강도] 피처 새로 계산 {len(need):,}쌍" + (f" (저장분 {len(have):,}쌍 재사용)" if have is not None else ""))
        new = compute_features(df, need, log=log)
        have = new if have is None else pd.concat([have, new], ignore_index=True)
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            tmp = CACHE + ".tmp"
            pd.to_pickle({"key": key, "df": have}, tmp)
            os.replace(tmp, CACHE)
        except Exception as e:
            log(f"  [강도] 캐시 저장 실패(무시): {e}")
    else:
        log(f"  [강도] 저장된 피처 {len(have):,}쌍 재사용")
    return pairs[["code", "date"]].merge(have, on=["code", "date"], how="left")


# ── 워크포워드 채점 ──────────────────────────────────────────────────────────
def fit_scorer(train, feats):
    """factor_scorer._load_ic_weights 와 같은 방식: 피처별 Spearman IC + 정렬된 기준분포."""
    from scipy.stats import spearmanr
    ic, ref = {}, {}
    y = train["net_pct"].astype(float)
    for f in feats:
        if f not in train.columns:
            continue
        x = train[f].astype(float)
        m = x.notna() & y.notna()
        if m.sum() < 100:
            continue
        r, _ = spearmanr(x[m], y[m])
        if not np.isnan(r):
            ic[f] = float(r)
            ref[f] = np.sort(x[m].to_numpy())
    return ic, ref


def score_rows(X, ic, ref, min_cov):
    """factor_scorer.score_ic 의 벡터판: 5 + Σ ic·(백분위−0.5)/분모·5, available-only 분모, 0~10."""
    full = sum(abs(v) * 0.5 for v in ic.values())
    raw, avail = np.zeros(len(X)), np.zeros(len(X))
    for f, v in ic.items():
        if f not in X.columns:
            continue
        col = X[f].to_numpy(float)
        ok = ~np.isnan(col)
        if not ok.any():
            continue
        sv = ref[f]
        pct = np.searchsorted(sv, col[ok], side="left") / len(sv)
        raw[ok] += v * (pct - 0.5)
        avail[ok] += abs(v) * 0.5
    cov = np.divide(avail, full, out=np.zeros_like(avail), where=full > 0)
    denom = np.where((avail > 0) & (cov >= min_cov), avail, full)
    return np.clip(5.0 + np.divide(raw, denom, out=np.zeros_like(raw), where=denom > 0) * 5.0, 0.0, 10.0)


def load_history():
    h = pd.read_csv(HIST_CSV, low_memory=False, dtype={"code": str})
    h["entry_date"] = _d8(h["entry_date"])
    return h


def walk_forward_scores(X, years, hist=None):
    """X(피처, 행마다 연도 years) → (점수 배열, 연도별 정보 {Y: {n_train, n_ic, top}}). 학습 부족한 해는 NaN."""
    feats, min_cov, live = _ic_constants()
    hist = load_history() if hist is None else hist
    out = np.full(len(X), np.nan)
    info = {}
    yrs = np.asarray(years)
    for Y in sorted(set(yrs)):
        tr = hist[hist["entry_date"] < f"{Y}0101"]
        lv = tr[tr["strategy"].astype(str).isin(live)]
        tr = lv if len(lv) >= MIN_TRAIN else tr
        if len(tr) < MIN_TRAIN:
            info[Y] = {"n_train": int(len(tr)), "n_ic": 0, "top": [], "skipped": True}
            continue
        ic, ref = fit_scorer(tr, feats)
        m = yrs == Y
        out[m] = score_rows(X[m], ic, ref, min_cov)
        info[Y] = {"n_train": int(len(tr)), "n_ic": len(ic), "skipped": False,
                   "top": [f"{k} {v:+.3f}" for k, v in sorted(ic.items(), key=lambda kv: -abs(kv[1]))[:3]]}
    return out, info


def trade_features(df, trades, log=print):
    """매매 목록 → 같은 순서의 피처 표(신호일 기준)."""
    sig = sig_dates_for(df, trades)
    pairs = pd.DataFrame({"code": [str(t.code).zfill(6) for t in trades], "date": sig})
    return features_cached(df, pairs, log=log)


def score_trades(df, trades, log=print):
    """매매 목록 → 같은 순서의 강도(v1) 점수 배열 + 연도별 정보."""
    if not trades:
        return np.array([]), {}
    X = trade_features(df, trades, log=log)
    years = [str(t.entry_date)[:4] for t in trades]
    return walk_forward_scores(X, years)
