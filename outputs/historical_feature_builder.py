"""
과거 3년 치 AI 학습용 거시경제(Macro) Feature 통합 수집기.

데이터 소스:
  - KOSDAQ 지수: FinanceDataReader (Naver Finance)
  - 시장 전체 수급: macro_data 활용 (종목별 foreign_net, inst_net 의 daily groupby)

이전 버전 (pykrx 직접 호출 1,456회) 대비:
  - KRX 서버 호출 0회 → 차단 무관
  - 24분+ → 1초 이내
  - 같은 데이터를 두 번 가져오던 비효율 제거
"""

import os
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

from strategies.daily_loader import load_macro_daily

FEATURE_DIR = "./ai_data"
os.makedirs(FEATURE_DIR, exist_ok=True)


def build_historical_features(start_date, end_date, market="KOSDAQ"):
    print(f"[{market}] {start_date} ~ {end_date} AI 학습용 데이터 통합 시작\n")

    # 1. KOSDAQ 지수 (FDR)
    print("1. 코스닥 지수 다운로드 중 (FDR)...")
    df_index = fdr.DataReader('KQ11', start_date, end_date)
    if df_index.empty:
        print("[에러] 코스닥 지수 가져오지 못함.")
        return

    df_index = df_index.reset_index()
    df_index.columns = [c.lower() for c in df_index.columns]

    # 지수 파생 feature
    df_index['kosdaq_return'] = df_index['close'].pct_change() * 100
    df_index['kosdaq_ma5'] = df_index['close'].rolling(5).mean()
    df_index['kosdaq_disparity'] = (df_index['close'] / df_index['kosdaq_ma5']) * 100
    df_index['kosdaq_volatility'] = ((df_index['high'] - df_index['low']) / df_index['open']) * 100

    print(f"   지수 영업일: {len(df_index)} 일")

    # 2. 시장 전체 수급 — macro_data 활용 (KRX 호출 0)
    print("\n2. 시장 전체 수급 집계 (macro_data groupby)...")
    macro = load_macro_daily()
    if 'foreign_net' not in macro.columns or 'inst_net' not in macro.columns:
        print("[에러] macro_data 에 foreign_net/inst_net 컬럼 없음. pykrx_collector 재실행 필요.")
        return

    macro['date'] = pd.to_datetime(macro['date'].astype(str), format='%Y%m%d')
    daily_agg = macro.groupby('date').agg(
        kosdaq_foreign_net=('foreign_net', 'sum'),
        kosdaq_inst_net=('inst_net', 'sum'),
        n_codes=('code', 'nunique'),
    ).reset_index()
    print(f"   집계 영업일: {len(daily_agg)}, 평균 종목 수/일: {daily_agg['n_codes'].mean():.0f}")

    # 3. 병합
    print("\n3. 병합 + 저장...")
    df_index['date'] = pd.to_datetime(df_index['date'])
    df_final = df_index.merge(daily_agg.drop(columns=['n_codes']),
                              on='date', how='left')
    df_final = df_final.dropna().copy()
    df_final['date'] = df_final['date'].dt.strftime('%Y%m%d')

    save_path = f"{FEATURE_DIR}/historical_features.csv"
    df_final.to_csv(save_path, index=False, encoding='utf-8-sig')

    print(f"\n[성공] {len(df_final)} 일 -> {save_path}")
    print(f"컬럼: {list(df_final.columns)}")


if __name__ == "__main__":
    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=365 * 3)
    build_historical_features(start_dt.strftime("%Y-%m-%d"),
                              end_dt.strftime("%Y-%m-%d"))
