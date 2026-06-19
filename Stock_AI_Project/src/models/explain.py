import pandas as pd
import pickle
import os
from src.logger import get_logger
from src.processor.indicators import FEATURE_COLS
from src.collector.config import ACTIVE_SECTOR

logger = get_logger('explain')

def explain_model(sector=None):
    if sector is None:
        sector = ACTIVE_SECTOR
    
    # 팩트 체크: 확장자를 .txt에서 .pkl로 수정
    model_path = f"src/models/saved/{sector}_model.pkl"
    
    if not os.path.exists(model_path):
        logger.error(f"❌ [{sector}] 모델 파일을 찾을 수 없습니다. 경로: {model_path}")
        return

    # 1. 피클(Pickle)로 모델 로드 (신/구 두 포맷 모두 지원)
    with open(model_path, 'rb') as f:
        obj = pickle.load(f)
    if isinstance(obj, dict) and 'model' in obj:
        model = obj['model']
        feat_names = obj.get('features') or FEATURE_COLS
    else:
        model = obj
        feat_names = FEATURE_COLS

    # 2. 피처 중요도 추출 (LightGBM 호환성 처리)
    try:
        if hasattr(model, 'booster_'):
            importance_values = model.booster_.feature_importance(importance_type='gain')
        else:
            importance_values = model.feature_importances_
    except Exception:
        importance_values = model.feature_importance(importance_type='gain')

    # 3. 데이터프레임 생성 (길이 안 맞아도 안전하게 자름)
    importance_df = pd.DataFrame({
        'Feature': list(feat_names)[:len(importance_values)],
        'Importance': importance_values
    }).sort_values(by='Importance', ascending=False)

    # 4. 결과 출력
    print(f"\n{'='*60}")
    print(f"🧠 [{sector}] AI 모델 의사결정 기여도 TOP 10")
    print(f"{'='*60}")
    for i, (idx, row) in enumerate(importance_df.head(10).iterrows()):
        print(f" {i+1}위: {row['Feature']:<15} | 기여도: {row['Importance']:.2f}")
    print(f"{'='*60}\n")

    # 5. 뉴스 지표 순위 확인
    try:
        news_rank = importance_df[importance_df['Feature'] == 'news_sentiment'].index[0]
        news_val = importance_df.loc[news_rank, 'Importance']
        total_features = len(importance_df)
        current_rank = importance_df.index.get_loc(news_rank) + 1
        print(f"🚩 [뉴스/공시 지표] 전체 {total_features}개 중 {current_rank}위 (기여도: {news_val:.2f})")
    except IndexError:
        print(f"⚠️ 'news_sentiment' 지표가 학습 모델에 포함되지 않았습니다.")

if __name__ == "__main__":
    import sys
    sector_arg = sys.argv[1] if len(sys.argv) > 1 else None
    explain_model(sector_arg)