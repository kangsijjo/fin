"""
feature_spec.py — AI 피처 '계약(contract)' 단일 출처 (Single Source of Truth)

학습(ai_trainer_v4)·라이브 추론(factor_scorer)·대시보드(_ai_score_signals)가
"어떤 피처를, 어떤 이름으로, 어떤 순서로" 쓰는지에 대한 선언적 계약을 한 곳에 모은다.

배경: 과거 이 정의(특히 추론 피처 '순서')가 3곳에 하드코딩되어 어긋나며
      'ai_prob 조용한 None'(11피처 모델에 24피처 입력 → feature_names mismatch,
      2026-06-29 수정) 같은 계약-drift 버그가 반복됐다. 이 모듈로 목록/순서/그룹/로더를
      단일화해 그 버그 클래스를 차단한다.

주의(범위): 피처 '계산 로직'(RSI/ATR/vol_ratio 등)은 데이터 소스가 모듈마다 달라
      (load_macro_daily df vs stock.db) 각 모듈이 그대로 보유한다. 여기서는 '계산'이
      아니라 '계약(목록·순서·그룹)'만 단일화한다. 계산 자체의 train/live 정합성은
      별건(통합 스모크 테스트 tests/test_feature_contract.py 로 일부 가드).
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# 학습 산출물 — 실제 학습된 피처 순서의 단일 진실원천(그룹탐색으로 가변).
FEATURES_CSV = os.path.join(_HERE, "ai_data", "meta_model_v4.features.csv")

# ─────────────────────────────────────────────────────────────────────────────
# 학습 투입 후보 '전체' 피처 (= ai_trainer_v4 의 마스터 목록).
#   순서는 학습 코드의 기존 정의와 동일하게 유지(재현성).
# ─────────────────────────────────────────────────────────────────────────────
ALL_FEATURES = [
    # 종목 (pykrx CSV)
    "rsi14", "atr_pct", "vol_ratio", "tv_ratio", "for_5d", "ins_5d", "mcap_class",
    # 신호 강도/유동성
    "score_tv",
    # 뉴스 (stock.db news)
    "news_sent_7d", "news_cnt_7d",
    # 매크로 레짐 (stock.db macro_indicators + indicators.csv)
    "vix", "vix_chg_5d", "sox_ret_5d", "usdkrw_chg_5d", "kospi_ret_20d",
    # 신용잔고 (stock.db credit_balance)
    "crd_remn_rt", "crd_remn_chg_5d",
    # 수급 DB (stock.db supply_demand) — pykrx for_5d/ins_5d 보완
    "for_net5_db", "ins_net5_db",
    # 기술지표 DB (stock.db korea_indicators — 사전 계산)
    "rsi_db", "macd_hist_db", "bb_pct_db",
    # 키움 프로그램매매 (kiwoom_backfill merge 후 존재 — 없으면 자동 제외)
    "prm_net_5d_ratio",
]

# ─────────────────────────────────────────────────────────────────────────────
# 지표조합 그룹탐색(ai_trainer_v4)용 — CORE 는 항상 포함, 나머지 '그룹'만 on/off.
#   불변식: CORE_FEATURES ∪ (모든 FEATURE_GROUPS) == ALL_FEATURES (누락/잉여 0)
# ─────────────────────────────────────────────────────────────────────────────
CORE_FEATURES = ["rsi14", "atr_pct", "vol_ratio", "tv_ratio", "score_tv", "mcap_class"]
FEATURE_GROUPS = {
    "supply_pykrx": ["for_5d", "ins_5d"],
    "news":         ["news_sent_7d", "news_cnt_7d"],
    "macro":        ["vix", "vix_chg_5d", "sox_ret_5d", "usdkrw_chg_5d", "kospi_ret_20d"],
    "credit":       ["crd_remn_rt", "crd_remn_chg_5d"],
    "supply_db":    ["for_net5_db", "ins_net5_db"],
    "tech_db":      ["rsi_db", "macd_hist_db", "bb_pct_db"],
    "program":      ["prm_net_5d_ratio"],
}

# 전략 one-hot 피처 (현재 학습 풀: h252_40 / h500_20 / h500_40_MKT). 항상 포함.
STRAT_FEATURES = ["strat_h252_40", "strat_h500_20", "strat_h500_40_MKT"]

# ─────────────────────────────────────────────────────────────────────────────
# 추론 폴백 순서 — meta_model_v4.features.csv 가 없을 때만 쓰는 '전체' 하드코딩.
#   정상 경로는 load_model_feature_order() 가 실제 학습 산출물에서 읽는다.
#   (주의: 폴백은 credit 그룹 미포함 — 과거 추론 하드코딩과의 하위호환을 위해 그대로 둠.)
# ─────────────────────────────────────────────────────────────────────────────
FALLBACK_MODEL_ORDER = [
    "rsi14", "atr_pct", "vol_ratio", "tv_ratio", "for_5d", "ins_5d", "mcap_class",
    "score_tv", "news_sent_7d", "news_cnt_7d", "vix", "vix_chg_5d", "sox_ret_5d",
    "usdkrw_chg_5d", "kospi_ret_20d", "for_net5_db", "ins_net5_db", "rsi_db",
    "macd_hist_db", "bb_pct_db", "prm_net_5d_ratio",
] + STRAT_FEATURES


def load_model_feature_order(features_csv: str = FEATURES_CSV):
    """학습 산출물(meta_model_v4.features.csv)의 실제 피처 순서를 읽는다.

    ai_trainer_v4 의 지표조합 그룹탐색으로 학습 피처셋이 매 학습마다 가변이므로,
    추론(factor_scorer / dashboard)도 학습과 동일한 순서·개수를 써야 XGBoost 의
    차원·feature_names 불일치(predict 예외 → 조용한 None)를 피할 수 있다.
    파일이 없거나 비면 FALLBACK_MODEL_ORDER(하드코딩)로 폴백한다.
    """
    try:
        import pandas as pd  # 지연 import — 모듈 자체는 의존성 가볍게 유지
        if os.path.exists(features_csv):
            cols = pd.read_csv(features_csv).iloc[:, 0].astype(str).tolist()
            cols = [c for c in cols if c and c.lower() != "nan"]
            if len(cols) >= 3:
                return cols
    except Exception:
        pass
    return list(FALLBACK_MODEL_ORDER)
