import pandas as pd
import numpy as np
import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
import pickle
import os
from src.config_db import get_connection
from src.logger import get_logger
from src.processor.indicators import FEATURE_COLS

logger = get_logger('train')

def load_data(sector, market='korea'):
    """DB에서 데이터 로드 (지표가 이미 계산된 indicators 테이블 전용 사용)"""
    conn = get_connection()
    table = 'korea_indicators' if market == 'korea' else 'usa_indicators'
    try:
        df = pd.read_sql(
            f"SELECT * FROM {table} WHERE sector=? ORDER BY ticker, date",
            conn, params=[sector]
        )
    except Exception as e:
        logger.error(f"[{table}] 테이블을 찾을 수 없습니다. 파이프라인에서 지표 생성을 먼저 진행해주세요.")
        df = pd.DataFrame()
    conn.close()
    return df

def train_model(sector=None, market=None):
    from src.collector.config import ACTIVE_SECTOR

    if sector is None:
        sector = ACTIVE_SECTOR

    if market is None:
        usa_sectors = [
            'Industrials', 'Financials', 'Information Technology',
            'Health Care', 'Consumer Discretionary', 'Consumer Staples',
            'Utilities', 'Real Estate', 'Materials',
            'Communication Services', 'Energy'
        ]
        market = 'usa' if sector in usa_sectors else 'korea'

    logger.info(f"=== {sector} 섹터 모델 학습 시작 ===")

    df = load_data(sector, market)
    if len(df) == 0:
        logger.warning("데이터 없음. 수집 및 지표생성을 먼저 완료해주세요.")
        return

    logger.info(f"데이터 로드: {len(df)}행 (21개 피처 포함)")

    # 보스께서 작성하신 3분류 타겟 생성 로직 완벽 보존
    features = []
    for ticker, group in df.groupby('ticker'):
        g = group.copy()
        future_return = (g['Close'].shift(-5) - g['Close']) / g['Close']
        g['target'] = 0
        g.loc[future_return >= 0.02, 'target'] = 1
        g.loc[future_return <= -0.02, 'target'] = 2
        features.append(g)

    df = pd.concat(features, ignore_index=True)

    # FEATURE_COLS 가 24개로 늘었지만 indicators 테이블이 옛 21개 스키마일 수도 있음.
    # 누락 컬럼은 0 으로 채워 dropna 에서 KeyError 방지 + 호환 유지.
    missing_feat = [c for c in FEATURE_COLS if c not in df.columns]
    if missing_feat:
        logger.warning(f"FEATURE_COLS 누락 (0 처리): {missing_feat}")
        for c in missing_feat:
            df[c] = 0.0

    # 결측치 제거
    df = df.dropna(subset=FEATURE_COLS + ['target'])
    logger.info(f"결측치 제거 후: {len(df)}행 ({len(FEATURE_COLS)}개 피처)")
    logger.info(f"매수(1): {(df['target']==1).sum()}개 / "
               f"관망(0): {(df['target']==0).sum()}개 / "
               f"매도(2): {(df['target']==2).sum()}개")

    # [중요] TimeSeriesSplit 은 행 인덱스 기준으로 분할한다.
    # 종전: (ticker, date) 정렬 → 같은 종목의 미래가 다른 종목의 과거에 누수됨.
    # 수정: 글로벌 date 정렬로 변경하여 분할 경계가 실제 시간 경계와 일치하도록.
    df = df.sort_values(['date', 'ticker']).reset_index(drop=True)

    X = df[FEATURE_COLS]
    y = df['target']

    # 하이퍼파라미터는 config.yaml 의 model 섹션에서 읽는다.
    # (기존: 코드에 중복 하드코딩 → config 수정이 학습에 반영 안 되던 문제)
    try:
        from src.collector.config import MODEL_CONFIG
        _mc = MODEL_CONFIG or {}
    except Exception:
        _mc = {}
    lgbm_params = dict(
        n_estimators=int(_mc.get('n_estimators', 100)),
        learning_rate=float(_mc.get('learning_rate', 0.05)),
        max_depth=int(_mc.get('max_depth', 6)),
        num_leaves=int(_mc.get('num_leaves', 31)),
        random_state=42,
        verbose=-1,
    )
    logger.info(f"LightGBM 파라미터 (config.yaml): {lgbm_params}")

    # 시계열 분할 + purge gap.
    # target 이 5일 미래 수익률이므로 train 구간 마지막 ~5 row 의 target 은
    # val 구간 데이터를 미리 본다. fold 마다 train 의 뒷부분 일부 행을 제거.
    HOLDING_DAYS = 5
    tscv = TimeSeriesSplit(n_splits=5)
    scores = []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        # 글로벌 date 정렬 상태에서 한 날짜에 여러 ticker 가 있으므로
        # 단순히 HOLDING_DAYS 행이 아니라 ticker 수 × HOLDING_DAYS 행을 제거.
        n_tickers = df['ticker'].nunique() if 'ticker' in df.columns else 1
        purge_rows = max(n_tickers * HOLDING_DAYS, HOLDING_DAYS)
        train_idx = train_idx[:-purge_rows] if len(train_idx) > purge_rows else train_idx

        X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

        model = lgb.LGBMClassifier(**lgbm_params)
        model.fit(X_train, y_train)
        pred = model.predict(X_val)
        try:
            proba = model.predict_proba(X_val)
            auc = roc_auc_score(y_val, proba, multi_class='ovr', average='macro')
        except Exception:
            auc = float('nan')
        f1 = f1_score(y_val, pred, average='macro', zero_division=0)
        acc = accuracy_score(y_val, pred)
        scores.append({'acc': acc, 'f1': f1, 'auc': auc})
        logger.info(f"  Fold {fold+1} acc={acc:.4f} f1_macro={f1:.4f} auc_ovr={auc:.4f}")

    avg_acc = np.mean([s['acc'] for s in scores])
    avg_f1  = np.mean([s['f1']  for s in scores])
    avg_auc = np.nanmean([s['auc'] for s in scores])
    logger.info(f"평균 acc={avg_acc:.4f} f1_macro={avg_f1:.4f} auc_ovr={avg_auc:.4f}")
    # 클래스 불균형 환경에서는 f1/auc 가 의사결정에 더 신뢰할 수 있는 지표.

    # 최종 모델 학습
    final_model = lgb.LGBMClassifier(**lgbm_params)
    final_model.fit(X, y)

    # 모델 저장 (feature 순서를 함께 보관하여 추론 시 컬럼 어긋남 방지)
    os.makedirs('src/models/saved', exist_ok=True)
    model_path = f'src/models/saved/{sector}_model.pkl'
    with open(model_path, 'wb') as f:
        pickle.dump({'model': final_model, 'features': list(FEATURE_COLS)}, f)
    logger.info(f"모델 저장 완료: {model_path}")

    return final_model

# src/models/train.py 맨 아래 덮어씌울 부분
if __name__ == "__main__":
    import sys
    from src.collector.config import ACTIVE_SECTOR, KOREA_SECTORS, USA_SECTORS

    # 터미널 인자가 있는지 확인
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'all':
            logger.info("🚀 전 섹터 일괄 학습 모드 가동 (한국 + 미국)")
            for s in KOREA_SECTORS:
                train_model(s, market='korea')
            for s in USA_SECTORS:
                train_model(s, market='usa')
        else:
            # 특정 섹터 지정 실행
            train_model(cmd)
    else:
        # 인자가 없으면 자동화 모드 작동
        logger.info(f"🤖 자동화 모드: 현재 활성 섹터[{ACTIVE_SECTOR}] 학습 진행")
        train_model(ACTIVE_SECTOR)