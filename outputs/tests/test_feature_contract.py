"""
통합 스모크 테스트 — '조용한 데이터-계약 drift' 버그 클래스 회귀 방지.

이 시스템의 치명 버그들(매수 2개월 0건/신호 2개월 전멸/221행 어긋남/ai_prob 조용한
None)은 전부 같은 클래스였다: writer↔파일↔reader 또는 학습↔추론 사이의 '암묵적
계약'이 어긋났는데 try/except 로 조용히 굴러간 것. 그 계약을 여기서 명시적으로 고정한다.

실행: cd C:/fin/outputs ; python -m pytest tests -q
"""
import os
import csv
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)

AI_DIR = os.path.join(OUTPUTS, "ai_data")
MODEL_JSON = os.path.join(AI_DIR, "meta_model_v4.json")
FEATURES_CSV = os.path.join(AI_DIR, "meta_model_v4.features.csv")


# ─────────────────────────────────────────────────────────────────────────────
# feature_spec 내부 정합성 (계산 없이 선언만 검사 — 항상 실행)
# ─────────────────────────────────────────────────────────────────────────────

def test_feature_spec_internal_consistency():
    """CORE ∪ 그룹 == ALL_FEATURES, 중복 0 — 그룹탐색이 모든 피처를 토글할 수 있어야."""
    import feature_spec as fs

    assert len(fs.ALL_FEATURES) == len(set(fs.ALL_FEATURES)), "ALL_FEATURES 중복"
    assert set(fs.CORE_FEATURES) <= set(fs.ALL_FEATURES), "CORE 가 ALL 밖"
    for g, cols in fs.FEATURE_GROUPS.items():
        assert set(cols) <= set(fs.ALL_FEATURES), f"그룹 {g} 가 ALL 밖"

    union = set(fs.CORE_FEATURES)
    for cols in fs.FEATURE_GROUPS.values():
        union |= set(cols)
    assert union == set(fs.ALL_FEATURES), "CORE+그룹 != ALL_FEATURES (피처 누락/잉여)"

    # 폴백 순서: 중복 0 + strat one-hot 으로 끝남
    assert len(fs.FALLBACK_MODEL_ORDER) == len(set(fs.FALLBACK_MODEL_ORDER)), "폴백 중복"
    assert fs.FALLBACK_MODEL_ORDER[-len(fs.STRAT_FEATURES):] == fs.STRAT_FEATURES


def test_consumers_share_single_source():
    """factor_scorer·dashboard·ai_trainer 가 feature_spec 을 단일 출처로 쓰는지 확인.
    (3곳 하드코딩이 어긋나 ai_prob 가 조용히 None 이던 버그의 재발 방지)."""
    import feature_spec as fs
    import factor_scorer as fsc

    # factor_scorer 의 폴백 별칭이 feature_spec 과 동일 객체/내용이어야
    assert list(fsc.MODEL_FEATURE_ORDER) == list(fs.FALLBACK_MODEL_ORDER)
    # 로더가 동일 함수(같은 출처)인지
    assert fsc.load_model_feature_order is fs.load_model_feature_order

    import ai_trainer_v4 as tr
    assert list(tr.FEATURES) == list(fs.ALL_FEATURES)
    assert tr.CORE_FEATURES is fs.CORE_FEATURES
    assert tr.FEATURE_GROUPS is fs.FEATURE_GROUPS


# ─────────────────────────────────────────────────────────────────────────────
# 학습↔추론 차원 계약 (모델/산출물 있을 때만 — 핵심: ai_prob None 회귀 방지)
# ─────────────────────────────────────────────────────────────────────────────

def test_trained_features_are_declared():
    """학습 산출물의 모든 피처가 feature_spec 에 선언돼 있어야(미선언 = 계약 drift)."""
    import feature_spec as fs
    if not os.path.exists(FEATURES_CSV):
        pytest.skip("meta_model_v4.features.csv 없음")
    trained = fs.load_model_feature_order(FEATURES_CSV)
    declared = set(fs.ALL_FEATURES) | set(fs.STRAT_FEATURES)
    unknown = [c for c in trained if c not in declared]
    assert not unknown, f"학습 피처가 feature_spec 에 미선언: {unknown}"


def test_model_inference_dimension_matches_trained():
    """추론 피처 순서(features.csv 로드) 개수 == 모델이 기대하는 피처 수,
    그리고 그 벡터로 predict 가 예외 없이 확률을 반환해야 한다.
    (24피처를 11피처 모델에 넣어 feature_names mismatch → 조용한 None 이던 버그 회귀 방지)."""
    import feature_spec as fs
    if not (os.path.exists(MODEL_JSON) and os.path.exists(FEATURES_CSV)):
        pytest.skip("model/features.csv 없음")
    try:
        import xgboost as xgb
        import pandas as pd
    except ImportError:
        pytest.skip("xgboost 미설치")

    order = fs.load_model_feature_order(FEATURES_CSV)
    m = xgb.XGBClassifier()
    m.load_model(MODEL_JSON)

    n_expected = m.get_booster().num_features()
    assert len(order) == n_expected, (
        f"추론 피처 {len(order)}개 != 모델 기대 {n_expected}개 — 차원 drift!")

    X = pd.DataFrame([[0.0] * len(order)], columns=order)
    p = float(m.predict_proba(X)[0][1])
    assert 0.0 <= p <= 1.0, f"predict_proba 비정상: {p}"


def test_factor_scorer_model_proba_not_none():
    """라이브 경로(FactorScorer.model_proba)가 실제 확률을 반환해야 한다(None 아님)."""
    if not (os.path.exists(MODEL_JSON) and os.path.exists(FEATURES_CSV)):
        pytest.skip("model/features.csv 없음")
    try:
        import xgboost  # noqa: F401
    except ImportError:
        pytest.skip("xgboost 미설치")

    import feature_spec as fs
    from factor_scorer import FactorScorer

    scorer = FactorScorer()  # use_shap=False (라이브 기본)
    order = fs.load_model_feature_order(FEATURES_CSV)
    feats = {f: 0.5 for f in order}            # 학습 피처를 모두 채운 더미 신호
    p = scorer.model_proba(feats)
    assert p is not None, "model_proba None — 차원 불일치 회귀!"
    assert 0.0 <= float(p) <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# 신호 CSV 헤더 계약 (writer 필드셋 == 파일 헤더셋 — 221행 어긋남 회귀 방지)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mod_name,fname", [
    ("live_signal", "paper_signals.csv"),
    ("kis_live_signal", "kis_paper_signals.csv"),
])
def test_paper_signals_header_contract(mod_name, fname):
    """기존 신호 CSV 의 헤더 집합 == 코드의 CSV_FIELDS 집합.
    (어긋나면 DictWriter append 가 컬럼을 어긋나게/누락해 매매·대시보드 오류)."""
    path = os.path.join(OUTPUTS, fname)
    if not os.path.exists(path):
        pytest.skip(f"{fname} 없음")

    mod = __import__(mod_name)
    with open(path, encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f), [])
    header = [h.strip() for h in header if h.strip()]

    assert set(header) == set(mod.CSV_FIELDS), (
        f"{fname} 헤더가 {mod_name}.CSV_FIELDS 와 불일치(컬럼 drift!)\n"
        f"  file={header}\n  spec={mod.CSV_FIELDS}")
