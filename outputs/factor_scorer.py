"""
factor_scorer.py — 다인수 팩터 점수 엔진

두 가지 방법:
  Method B (IC Score): Spearman 상관 기반 0-10 점수
                       각 피처와 미래수익(net_pct) 간의 IC(정보계수)로 가중
  Method A (SHAP):     XGBoost meta_model_v4 SHAP 기여도
                       AI 모델이 왜 이 종목을 좋게/나쁘게 봤는지 근거 설명

사용법:
  from factor_scorer import FactorScorer
  scorer = FactorScorer()                            # IC 계산 + 모델 로드
  price_feats = scorer.prepare_price_features(df)   # 전체 일봉 df 에서 1회 계산
  macro_feats = scorer.prepare_macro_features()     # indicators.csv 로드
  db_feats    = scorer.prepare_db_features(con, date_str)   # stock.db (옵션)
  for sig in signals:
      feats  = scorer.build_feature_vec(sig['code'], price_feats, macro_feats, db_feats)
      result = scorer.score(sig['code'], sig['name'], feats)
      print(scorer.format_report(result))
"""

import os
import bisect
import numpy as np
import pandas as pd
from datetime import datetime as _dt, timedelta as _td

# ─────────────────────────────────────────────────────────────────────────────
# 설정: 파일 경로
# ─────────────────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))

TRADES_CSV     = os.path.join(_HERE, "trades_history_v3.csv")
MODEL_JSON     = os.path.join(_HERE, "ai_data", "meta_model_v4.json")
FEATURES_CSV   = os.path.join(_HERE, "ai_data", "meta_model_v4.features.csv")
MACRO_IND_CSV  = os.path.join(_HERE, "macro_data", "indicators.csv")

# ─────────────────────────────────────────────────────────────────────────────
# 인간 친화적 피처 레이블 & IC 방향 힌트
# (direction: +1이면 높을수록 유리, -1이면 높을수록 불리)
# ─────────────────────────────────────────────────────────────────────────────
FEATURE_META = {
    # 이름                    : (한글 레이블,              예상 방향)
    "vix":              ("VIX 공포지수",               -1),  # 낮을수록 유리
    "vix_chg_5d":       ("VIX 5일 변화",               -1),  # 하락이 유리
    "kospi_ret_20d":    ("KOSPI 20일 추세",             +1),
    "sox_ret_5d":       ("SOX 반도체 5일",              +1),
    "usdkrw_chg_5d":    ("원/달러 5일 변화",            -1),  # 강달러=불리
    "for_net5_db":      ("외국인 5일 순매수(DB)",        +1),
    "ins_net5_db":      ("기관 5일 순매수(DB)",          +1),
    "for_5d":           ("외국인 5일(pykrx)",           +1),
    "ins_5d":           ("기관 5일(pykrx)",             +1),
    "rsi14":            ("RSI-14 (고속)",               -1),  # 과매수=불리
    "rsi_db":           ("RSI (DB)",                   -1),
    "macd_hist_db":     ("MACD Hist (DB)",              +1),
    "bb_pct_db":        ("BB 위치 (DB)",                -1),  # 상단=과매수
    "atr_pct":          ("변동성 ATR%",                 -1),  # 고변동=불리
    "vol_ratio":        ("거래량 비율",                  +1),
    "tv_ratio":         ("거래대금 비율",                +1),
    "score_tv":         ("거래대금 순위",                +1),
    "news_sent_7d":     ("뉴스 감성 7일",               +1),
    "crd_remn_rt":      ("신용잔고율",                   -1),  # 높을수록 불리
    "prm_net_5d_ratio": ("프로그램 순매수 5일",          +1),
    "mcap_class":       ("시가총액 분류",                -1),  # 1=대형(낮은값=대형)
}

# IC 가중에 사용할 피처 (coverage 상위 + 해석 가능한 것만)
IC_FEATURES = [
    "vix", "vix_chg_5d", "kospi_ret_20d", "sox_ret_5d", "usdkrw_chg_5d",
    "for_net5_db", "ins_net5_db", "for_5d", "ins_5d",
    "rsi14", "rsi_db", "macd_hist_db", "bb_pct_db",
    "atr_pct", "vol_ratio", "tv_ratio", "score_tv",
    "news_sent_7d", "crd_remn_rt", "prm_net_5d_ratio",
]

# XGBoost 모델 feature 순서 (meta_model_v4.features.csv 기준)
MODEL_FEATURE_ORDER = [
    "rsi14", "atr_pct", "vol_ratio", "tv_ratio", "for_5d", "ins_5d", "mcap_class",
    "score_tv", "news_sent_7d", "news_cnt_7d", "vix", "vix_chg_5d", "sox_ret_5d",
    "usdkrw_chg_5d", "kospi_ret_20d", "for_net5_db", "ins_net5_db", "rsi_db",
    "macd_hist_db", "bb_pct_db", "prm_net_5d_ratio",
    "strat_h252_40", "strat_h500_20", "strat_h500_40_MKT",
]


class FactorScorer:
    """다인수 팩터 점수 엔진 (IC + SHAP)."""

    def __init__(self,
                 trades_csv: str = TRADES_CSV,
                 model_json: str = MODEL_JSON,
                 features_csv: str = FEATURES_CSV):
        self.ic_weights: dict = {}      # {feat: IC 값}
        self.feat_stats: dict = {}      # {feat: (sorted_values, mean)} for percentile
        self._model = None              # XGBoost model (lazy load)
        self._explainer = None          # SHAP explainer (lazy load)
        self._model_json = model_json

        self._load_ic_weights(trades_csv)

    # ─────────────────────────────────────────────────────────────────────
    # 초기화: IC 가중치 계산
    # ─────────────────────────────────────────────────────────────────────

    def _load_ic_weights(self, trades_csv: str):
        """trades_history_v3.csv 에서 각 피처의 Spearman IC 계산."""
        if not os.path.exists(trades_csv):
            print(f"[FactorScorer] {trades_csv} 없음 — IC 가중치 계산 불가")
            return

        try:
            from scipy.stats import spearmanr
        except ImportError:
            print("[FactorScorer] scipy 없음 — IC 계산 불가. 'pip install scipy'")
            return

        df = pd.read_csv(trades_csv)
        target = "net_pct"
        if target not in df.columns:
            print("[FactorScorer] trades_history_v3.csv 에 net_pct 컬럼 없음")
            return

        y = df[target].astype(float)
        computed = 0

        for feat in IC_FEATURES:
            # trades_history 컬럼명 매핑 (crd_remn_rt_y 가 DB 기반 최장 커버리지)
            col = feat
            if feat == "crd_remn_rt" and "crd_remn_rt_y" in df.columns:
                col = "crd_remn_rt_y"
            elif feat == "crd_remn_rt" and "crd_remn_rt_x" in df.columns:
                col = "crd_remn_rt_x"

            if col not in df.columns:
                continue

            x = df[col].astype(float)
            mask = x.notna() & y.notna()
            if mask.sum() < 100:   # 샘플 너무 적으면 신뢰 불가
                continue

            ic, pval = spearmanr(x[mask], y[mask])
            if np.isnan(ic):
                continue

            self.ic_weights[feat] = float(ic)
            # 백분위 계산용 정렬 배열 저장
            vals = sorted(x[mask].tolist())
            self.feat_stats[feat] = vals
            computed += 1

        if computed:
            print(f"[FactorScorer] IC 계산 완료: {computed}개 피처")
            # 상위 5개 출력
            top = sorted(self.ic_weights.items(), key=lambda kv: abs(kv[1]), reverse=True)[:5]
            for f, ic in top:
                label = FEATURE_META.get(f, (f, 0))[0]
                print(f"  {label:20s}: IC={ic:+.4f}")

    # ─────────────────────────────────────────────────────────────────────
    # 피처 추출 — 가격 데이터 (전체 일봉 df 에서 1회 계산)
    # ─────────────────────────────────────────────────────────────────────

    def prepare_price_features(self, df: pd.DataFrame) -> dict:
        """
        load_macro_daily() 결과 df 를 받아 모든 종목의 최신 기술지표 계산.
        Returns: {code: {feat: value, ...}}
        """
        df = df.copy()
        df["date"] = df["date"].astype(str)
        df["code"] = df["code"].astype(str).str.zfill(6)
        df = df.sort_values(["code", "date"])

        # ── RSI-14 (Wilder)
        delta = df.groupby("code")["close"].diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)
        avg_g = gain.groupby(df["code"]).transform(
            lambda x: x.ewm(alpha=1/14, adjust=False).mean())
        avg_l = loss.groupby(df["code"]).transform(
            lambda x: x.ewm(alpha=1/14, adjust=False).mean())
        rs = avg_g / avg_l.replace(0, np.nan)
        df["rsi14"] = 100 - (100 / (1 + rs))

        # ── ATR-14
        df["prev_close"] = df.groupby("code")["close"].shift(1)
        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(
                (df["high"] - df["prev_close"]).abs(),
                (df["low"]  - df["prev_close"]).abs()
            )
        )
        atr14 = df.groupby("code")["tr"].transform(
            lambda x: x.ewm(alpha=1/14, adjust=False).mean())
        df["atr_pct"] = (atr14 / df["close"].replace(0, np.nan)) * 100

        # ── Volume / TradingValue ratio (20일 이동평균 대비)
        vol_ma20 = df.groupby("code")["volume"].transform(
            lambda x: x.shift(1).rolling(20, min_periods=10).mean())
        tv_ma20  = df.groupby("code")["trading_value"].transform(
            lambda x: x.shift(1).rolling(20, min_periods=10).mean())
        df["vol_ratio"] = df["volume"] / vol_ma20.replace(0, np.nan)
        df["tv_ratio"]  = df["trading_value"] / tv_ma20.replace(0, np.nan)

        # ── 외국인/기관 5일 합 / 시총 정규화
        if "foreign_net" in df.columns:
            f5 = df.groupby("code")["foreign_net"].transform(
                lambda x: x.rolling(5, min_periods=5).sum())
            df["for_5d"] = f5 / df["market_cap"].replace(0, np.nan) * 1000
        else:
            df["for_5d"] = np.nan

        if "inst_net" in df.columns:
            i5 = df.groupby("code")["inst_net"].transform(
                lambda x: x.rolling(5, min_periods=5).sum())
            df["ins_5d"] = i5 / df["market_cap"].replace(0, np.nan) * 1000
        else:
            df["ins_5d"] = np.nan

        # ── 시가총액 분류
        df["mcap_class"] = 3
        df.loc[df["market_cap"] >= 1_000_000_000_000,  "mcap_class"] = 2
        df.loc[df["market_cap"] >= 10_000_000_000_000, "mcap_class"] = 1

        # ── score_tv: 당일 거래대금 전체 순위 (1=최대)
        last_date = df["date"].max()
        ld = df[df["date"] == last_date].copy()
        ld["score_tv"] = ld["trading_value"].rank(ascending=False, method="min")

        # ── 최신값만 추출
        result = {}
        latest = df[df["date"] == last_date].set_index("code")
        score_map = ld.set_index("code")["score_tv"].to_dict()

        feat_cols = ["rsi14", "atr_pct", "vol_ratio", "tv_ratio",
                     "for_5d", "ins_5d", "mcap_class"]
        for code in latest.index:
            row = latest.loc[code]
            feats = {c: _safe_float(row.get(c)) for c in feat_cols}
            feats["score_tv"] = _safe_float(score_map.get(code))
            result[code] = feats

        return result

    # ─────────────────────────────────────────────────────────────────────
    # 피처 추출 — 매크로 (indicators.csv)
    # ─────────────────────────────────────────────────────────────────────

    def prepare_macro_features(self, csv_path: str = MACRO_IND_CSV) -> dict:
        """
        macro_data/indicators.csv → 최신 매크로 지표 dict.
        Returns: {indicator_key: value}
        """
        if not os.path.exists(csv_path):
            return {}

        try:
            m = pd.read_csv(csv_path)
            m["date"] = m["date"].astype(str).str.replace("-", "")
            # 최신 날짜 기준 피벗
            m = m.sort_values("date")
            w = m.pivot_table(index="date", columns="indicator",
                              values="close", aggfunc="last")
            # 최신 행
            latest_row = w.iloc[-1]
            out = {}

            if "VIX" in w.columns:
                vix_series = w["VIX"].dropna()
                out["vix"] = float(vix_series.iloc[-1])
                if len(vix_series) >= 6:
                    out["vix_chg_5d"] = float(
                        (vix_series.iloc[-1] - vix_series.iloc[-6]) / vix_series.iloc[-6] * 100
                    )

            if "SOX" in w.columns:
                sox = w["SOX"].dropna()
                if len(sox) >= 6:
                    out["sox_ret_5d"] = float(
                        (sox.iloc[-1] - sox.iloc[-6]) / sox.iloc[-6] * 100
                    )

            if "KRW_USD" in w.columns:
                krw = w["KRW_USD"].dropna()
                if len(krw) >= 6:
                    out["usdkrw_chg_5d"] = float(
                        (krw.iloc[-1] - krw.iloc[-6]) / krw.iloc[-6] * 100
                    )

            if "KOSPI" in w.columns:
                kospi = w["KOSPI"].dropna()
                if len(kospi) >= 21:
                    out["kospi_ret_20d"] = float(
                        (kospi.iloc[-1] - kospi.iloc[-21]) / kospi.iloc[-21] * 100
                    )

            return out
        except Exception as e:
            print(f"[FactorScorer] 매크로 피처 로드 실패: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────
    # 피처 추출 — stock.db (공급수요, 기술지표, 신용잔고)
    # ─────────────────────────────────────────────────────────────────────

    def prepare_db_features(self, con, date_str: str) -> dict:
        """
        stock.db 연결에서 모든 종목의 최신 DB 피처 로드.
        date_str: 'YYYYMMDD' 형식 신호일
        Returns: {code: {feat: value, ...}}
        """
        if con is None:
            return {}

        result = {}
        date_str = str(date_str).replace("-", "")[:8]

        # supply_demand
        try:
            _cutoff = (_dt.strptime(date_str, "%Y%m%d") - _td(days=150)).strftime("%Y%m%d")
            sd = pd.read_sql(
                "SELECT ticker, date, foreign_net_value, institution_net_value "
                "FROM supply_demand WHERE date >= ? ORDER BY ticker, date",
                con, params=[_cutoff]
            )
            sd["date"] = sd["date"].astype(str).str.replace("-", "")[:8] if len(sd) else sd["date"]
            if len(sd) > 0:
                sd["date"] = sd["date"].astype(str).str.replace("-", "").str[:8]
            for code, g in sd.groupby("ticker"):
                g = g.sort_values("date")
                latest5 = g[g["date"] <= date_str].tail(5)
                if len(latest5) == 0:
                    continue
                code = str(code).zfill(6)
                if code not in result:
                    result[code] = {}
                result[code]["for_net5_db"] = float(latest5["foreign_net_value"].sum())
                result[code]["ins_net5_db"] = float(latest5["institution_net_value"].sum())
        except Exception as e:
            print(f"[FactorScorer] supply_demand 로드 실패: {e}")

        # korea_indicators
        try:
            ki = pd.read_sql(
                "SELECT ticker, date, rsi, macd_hist, bb_upper, bb_lower, Close "
                "FROM korea_indicators WHERE date >= ? AND rsi IS NOT NULL "
                "ORDER BY ticker, date",
                con, params=[f"{int(date_str) - 100:08d}"]
            )
            if len(ki) > 0:
                ki["date"] = ki["date"].astype(str).str.replace("-", "").str[:8]
                bw = ki["bb_upper"] - ki["bb_lower"]
                ki["bb_pct"] = (ki["Close"] - ki["bb_lower"]) / bw.where(bw > 0)
                for code, g in ki.groupby("ticker"):
                    g = g[g["date"] <= date_str]
                    if len(g) == 0:
                        continue
                    row = g.iloc[-1]
                    code = str(code).zfill(6)
                    if code not in result:
                        result[code] = {}
                    result[code]["rsi_db"]       = _safe_float(row["rsi"])
                    result[code]["macd_hist_db"]  = _safe_float(row["macd_hist"])
                    result[code]["bb_pct_db"]     = _safe_float(row["bb_pct"])
        except Exception as e:
            print(f"[FactorScorer] korea_indicators 로드 실패: {e}")

        # credit_balance
        try:
            cb = pd.read_sql(
                "SELECT ticker, date, credit_remain_rt "
                "FROM credit_balance WHERE date >= ? AND credit_remain_rt IS NOT NULL "
                "ORDER BY ticker, date",
                con, params=[f"{int(date_str) - 100:08d}"]
            )
            if len(cb) > 0:
                cb["date"] = cb["date"].astype(str).str.replace("-", "").str[:8]
                for code, g in cb.groupby("ticker"):
                    g = g[g["date"] <= date_str]
                    if len(g) == 0:
                        continue
                    code = str(code).zfill(6)
                    if code not in result:
                        result[code] = {}
                    result[code]["crd_remn_rt"] = _safe_float(g.iloc[-1]["credit_remain_rt"])
        except Exception as e:
            print(f"[FactorScorer] credit_balance 로드 실패: {e}")

        return result

    # ─────────────────────────────────────────────────────────────────────
    # 피처 벡터 조합
    # ─────────────────────────────────────────────────────────────────────

    def build_feature_vec(self, code: str,
                          price_feats: dict,
                          macro_feats: dict,
                          db_feats: dict,
                          strategy: str = "h500_40_MKT") -> dict:
        """세 소스의 피처를 하나의 dict 로 통합."""
        code = str(code).zfill(6)
        feats: dict = {}
        feats.update(price_feats.get(code, {}))
        feats.update(macro_feats)                   # 매크로는 종목 공통
        feats.update(db_feats.get(code, {}))        # DB 피처 (있으면 덮어씀)

        # 전략 더미 피처
        feats["strat_h252_40"]     = 1.0 if strategy == "h252_40"     else 0.0
        feats["strat_h500_20"]     = 1.0 if strategy == "h500_20"     else 0.0
        feats["strat_h500_40_MKT"] = 1.0 if strategy == "h500_40_MKT" else 0.0

        return feats

    # ─────────────────────────────────────────────────────────────────────
    # Method B: IC 기반 팩터 점수 (0-10)
    # ─────────────────────────────────────────────────────────────────────

    def score_ic(self, feats: dict) -> dict:
        """
        각 피처의 IC 가중합 → 0-10 점수.
        반환: {
          'total': float (0-10),
          'contributions': [(label, contrib_pts, pct_rank), ...]  # 절댓값 내림차순
          'n_available': int  # 값 있는 피처 수
        }
        """
        if not self.ic_weights:
            return {"total": 5.0, "contributions": [], "n_available": 0}

        max_possible = sum(abs(ic) * 0.5 for ic in self.ic_weights.values())
        if max_possible == 0:
            return {"total": 5.0, "contributions": [], "n_available": 0}

        contributions = []
        n_avail = 0

        for feat, ic in self.ic_weights.items():
            val = feats.get(feat)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue

            # 백분위 계산 (0~1)
            sorted_vals = self.feat_stats.get(feat, [])
            if not sorted_vals:
                continue

            pct_rank = bisect.bisect_left(sorted_vals, val) / len(sorted_vals)
            # IC × (pct_rank - 0.5) : IC>0이고 상위 pct이면 양수 기여
            raw_contrib = ic * (pct_rank - 0.5)
            # 10점 척도로 변환
            contrib_pts = raw_contrib / max_possible * 5.0

            label = FEATURE_META.get(feat, (feat, 0))[0]
            contributions.append((label, contrib_pts, pct_rank))
            n_avail += 1

        # 기여도 절댓값 내림차순 정렬
        contributions.sort(key=lambda x: abs(x[1]), reverse=True)

        total_raw = sum(c[1] for c in contributions)
        # 기준 5점에서 합산
        total = max(0.0, min(10.0, 5.0 + total_raw))

        return {
            "total": round(total, 2),
            "contributions": contributions,
            "n_available": n_avail,
        }

    # ─────────────────────────────────────────────────────────────────────
    # Method A: SHAP 기여도
    # ─────────────────────────────────────────────────────────────────────

    def _load_model(self):
        """XGBoost 모델 + SHAP explainer 지연 로드."""
        if self._model is not None:
            return True
        try:
            import xgboost as xgb
            import shap

            if not os.path.exists(self._model_json):
                print(f"[FactorScorer] 모델 없음: {self._model_json}")
                return False

            model = xgb.XGBClassifier()
            model.load_model(self._model_json)
            self._model = model
            self._explainer = shap.TreeExplainer(model)
            return True
        except ImportError as e:
            print(f"[FactorScorer] SHAP/XGBoost 미설치: {e}")
            return False
        except Exception as e:
            print(f"[FactorScorer] 모델 로드 실패: {e}")
            return False

    def score_shap(self, feats: dict) -> dict:
        """
        XGBoost SHAP 값 계산.
        반환: {
          'available': bool,
          'top_positive': [(label, shap_val), ...],  # 상위 3개 유리 요인
          'top_negative': [(label, shap_val), ...],  # 상위 3개 불리 요인
          'model_proba': float,                       # 모델 예측 확률 (긍정 클래스)
        }
        """
        if not self._load_model():
            return {"available": False}

        try:
            import pandas as pd

            # 모델 입력 벡터 구성
            vec = []
            for f in MODEL_FEATURE_ORDER:
                v = feats.get(f)
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    # 평균값 대체 (학습 시 사용한 중앙값이 없으므로 0 대체)
                    v = 0.0
                vec.append(float(v))

            X = pd.DataFrame([vec], columns=MODEL_FEATURE_ORDER)
            shap_vals = self._explainer.shap_values(X)

            # XGBClassifier 이진분류: shap_values 는 [n_samples, n_features] or list
            if isinstance(shap_vals, list):
                sv = shap_vals[1][0]  # 클래스=1 (긍정) SHAP
            else:
                sv = shap_vals[0]

            feat_shap = list(zip(MODEL_FEATURE_ORDER, sv.tolist()))
            feat_shap.sort(key=lambda x: x[1], reverse=True)

            def _label(f):
                return FEATURE_META.get(f, (f, 0))[0]

            top_pos = [(f, _label(f), v) for f, v in feat_shap if v > 0][:3]
            top_neg = [(f, _label(f), v) for f, v in feat_shap if v < 0][-3:][::-1]

            # 모델 확률
            proba = float(self._model.predict_proba(X)[0][1])

            return {
                "available": True,
                "top_positive": [(lbl, val) for _, lbl, val in top_pos],
                "top_negative": [(lbl, val) for _, lbl, val in top_neg],
                "model_proba": proba,
            }
        except Exception as e:
            print(f"[FactorScorer] SHAP 계산 실패: {e}")
            return {"available": False}

    # ─────────────────────────────────────────────────────────────────────
    # 통합 점수 계산
    # ─────────────────────────────────────────────────────────────────────

    def score(self, code: str, name: str, feats: dict) -> dict:
        """IC 점수 + SHAP 결합 결과 dict."""
        ic_result   = self.score_ic(feats)
        shap_result = self.score_shap(feats)

        return {
            "code": code,
            "name": name,
            "feats": feats,
            "ic": ic_result,
            "shap": shap_result,
        }

    # ─────────────────────────────────────────────────────────────────────
    # 리포트 포맷
    # ─────────────────────────────────────────────────────────────────────

    def format_report(self, result: dict, max_factors: int = 6) -> str:
        """터미널 출력용 다인수 팩터 분석 리포트."""
        code  = result["code"]
        name  = result["name"]
        ic    = result["ic"]
        shap  = result["shap"]
        total = ic["total"]
        stars = _stars(total)

        lines = []
        lines.append(f"\n[{name} ({code})]  팩터점수: {total:.1f}/10  {stars}")

        if ic["contributions"]:
            lines.append("  ─── IC 팩터 기여도 ───────────────────────")
            for label, pts, pct in ic["contributions"][:max_factors]:
                sign = "+" if pts >= 0 else ""
                pct_str = f"({pct*100:.0f}%ile)"
                lines.append(f"  {label:22s}: {sign}{pts:+.2f}점  {pct_str}")
        else:
            lines.append("  IC 피처 데이터 없음")

        if shap.get("available"):
            lines.append("  ─── AI 모델 근거 (SHAP) ─────────────────")
            proba = shap["model_proba"]
            lines.append(f"  AI 긍정 확률: {proba*100:.1f}%")
            for label, val in shap["top_positive"]:
                lines.append(f"  ▲ {label:20s}: +{val:.4f}")
            for label, val in shap["top_negative"]:
                lines.append(f"  ▼ {label:20s}: {val:.4f}")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 유틸 함수
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(v):  # type: Optional[float]  — Python 3.9 호환
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _stars(score: float) -> str:
    filled = round(score / 2)  # 0-10 → 0-5
    filled = max(0, min(5, filled))
    return "★" * filled + "☆" * (5 - filled)


# ─────────────────────────────────────────────────────────────────────────────
# 단독 실행: IC 가중치 & 분포 점검
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=== FactorScorer 초기화 테스트 ===")
    scorer = FactorScorer()

    if not scorer.ic_weights:
        print("IC 가중치 없음. trades_history_v3.csv 를 먼저 생성하세요.")
        sys.exit(1)

    print(f"\n[IC 가중치 전체]")
    print(f"{'피처':<22} {'IC':>8}  {'해석'}")
    print("-" * 50)
    for feat, ic in sorted(scorer.ic_weights.items(), key=lambda x: abs(x[1]), reverse=True):
        label = FEATURE_META.get(feat, (feat, 0))[0]
        bar = ("▲" * int(abs(ic) * 20)) if ic > 0 else ("▼" * int(abs(ic) * 20))
        print(f"  {label:<22} {ic:+.4f}  {bar}")

    print(f"\n총 IC 가중치 합계: {sum(abs(v) for v in scorer.ic_weights.values()):.4f}")
    print(f"IC 가중 피처 수:  {len(scorer.ic_weights)}개")
    print("\n단독 테스트 완료.")
