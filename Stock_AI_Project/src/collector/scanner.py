import pandas as pd
import numpy as np
from src.config_db import get_connection
from src.logger import get_logger
from src.collector.config import ACTIVE_SECTOR, update_active_sector

logger = get_logger('scanner')

# 쓰레기 데이터 및 비정상 섹터 원천 차단
EXCLUDE_SECTORS = [
    None, 'None', '관리종목(소속부없음)', 'SPAC(소속부없음)',
    '투자주의환기종목(소속부없음)', '외국기업(소속부없음)',
    '벤처기업부', '우량기업부', '중견기업부', '기술성장기업부'
]

# 교체 방어 기준 (잦은 매매로 인한 수수료 출혈 방지)
LGBM_SWITCH_MARGIN = 0.03  # 기존 섹터 확률보다 3%p(0.03) 이상 높아야 교체
MOMENTUM_SWITCH_THRESHOLD = 1.15 # 모멘텀 점수는 15% 이상 차이 날 때 교체

def scan_best_sector(market='korea'):
    logger.info(f"=== 🚀 주도 섹터 스캔 시작 (현재 타겟: {ACTIVE_SECTOR}) ===")
    conn = get_connection()
    best_sector = None

    # 1. 1순위: LightGBM 예측 확률 기준
    try:
        table = 'finrl_dataset_kr' if market == 'korea' else 'finrl_dataset_us'
        # 'now' 대신 MAX(date)를 사용하여 데이터 누락 방지
        lgbm_df = pd.read_sql(f"""
            SELECT sector, AVG(lgbm_prob) as avg_prob, COUNT(DISTINCT tic) as cnt
            FROM {table}
            WHERE date >= date((SELECT MAX(date) FROM {table}), '-30 days')
            AND sector IS NOT NULL
            GROUP BY sector
            ORDER BY avg_prob DESC
        """, conn)
        
        if not lgbm_df.empty:
            lgbm_df = lgbm_df[~lgbm_df['sector'].isin(EXCLUDE_SECTORS)]
            lgbm_df = lgbm_df[lgbm_df['cnt'] >= 5] # 최소 5종목 이상
            
            if not lgbm_df.empty:
                logger.info("📊 [LightGBM 확률 기준 TOP 3]")
                for _, row in lgbm_df.head(3).iterrows():
                    marker = " (현재)" if row['sector'] == ACTIVE_SECTOR else ""
                    logger.info(f"  {row['sector']}: {row['avg_prob']:.3f}{marker}")
                
                new_best = lgbm_df.iloc[0]['sector']
                new_prob = lgbm_df.iloc[0]['avg_prob']
                
                # 현재 섹터의 점수 파악
                curr_row = lgbm_df[lgbm_df['sector'] == ACTIVE_SECTOR]
                curr_prob = curr_row.iloc[0]['avg_prob'] if not curr_row.empty else 0.0

                if new_best == ACTIVE_SECTOR:
                    logger.info(f"✅ [{ACTIVE_SECTOR}]가 여전히 확률 1위입니다. 유지를 결정합니다.")
                    best_sector = ACTIVE_SECTOR
                elif new_prob >= (curr_prob + LGBM_SWITCH_MARGIN):
                    logger.info(f"🔄 강력한 상승 신호 감지: [{ACTIVE_SECTOR}] -> [{new_best}] 교체")
                    best_sector = new_best
                else:
                    logger.info(f"🛡️ 1위가 [{new_best}]로 바뀌었으나 차이가 미미하여(수수료 방어) 기존 [{ACTIVE_SECTOR}]를 유지합니다.")
                    best_sector = ACTIVE_SECTOR
                
                conn.close()
                return best_sector
                
    except Exception as e:
        logger.warning(f"⚠️ LightGBM 스캔 실패, 위험 조정 모멘텀으로 대체합니다: {e}")

    # 2. 2순위: LightGBM 데이터가 없을 경우 (위험 조정 모멘텀 복원)
    stock_table = 'korea_stocks' if market == 'korea' else 'usa_stocks'
    try:
        df = pd.read_sql(f"""
            SELECT date, ticker, sector, Close 
            FROM {stock_table} 
            WHERE date >= date((SELECT MAX(date) FROM {stock_table}), '-30 days')
            AND sector IS NOT NULL
        """, conn)
    except Exception as e:
        logger.error(f"❌ 스캐너 데이터 로드 실패: {e}")
        conn.close()
        return None
    conn.close()

    if df.empty:
        return None

    momentum_list = []
    for sector, group in df.groupby('sector'):
        if sector in EXCLUDE_SECTORS:
            continue
            
        sector_scores = []
        for ticker, t_group in group.groupby('ticker'):
            t_group = t_group.sort_values('date')
            if len(t_group) >= 5:
                t_group['daily_ret'] = t_group['Close'].pct_change()
                total_ret = (t_group.iloc[-1]['Close'] - t_group.iloc[0]['Close']) / t_group.iloc[0]['Close']
                volatility = t_group['daily_ret'].std() * np.sqrt(252)
                
                if volatility > 0 and not np.isnan(volatility):
                    # 샤프 지수 복원 (단순 수익률이 아닌 위험 대비 수익률)
                    sector_scores.append(total_ret / volatility)
        
        if len(sector_scores) >= 5:
            momentum_list.append({
                'sector': sector, 
                'score': sum(sector_scores) / len(sector_scores)
            })

    if not momentum_list:
        return None

    momentum_df = pd.DataFrame(momentum_list).sort_values('score', ascending=False)
    
    new_best = momentum_df.iloc[0]['sector']
    new_score = momentum_df.iloc[0]['score']
    
    curr_row = momentum_df[momentum_df['sector'] == ACTIVE_SECTOR]
    curr_score = curr_row.iloc[0]['score'] if not curr_row.empty else -999

    logger.info("📊 [모멘텀(위험조정) 기준 TOP 3]")
    for _, row in momentum_df.head(3).iterrows():
        marker = " (현재)" if row['sector'] == ACTIVE_SECTOR else ""
        logger.info(f"  {row['sector']} (Score: {row['score']:.4f}){marker}")

    if new_best == ACTIVE_SECTOR:
        return ACTIVE_SECTOR
    elif new_score > (curr_score * MOMENTUM_SWITCH_THRESHOLD) or curr_score < 0:
        logger.info(f"🔄 모멘텀 주도 섹터 교체: [{ACTIVE_SECTOR}] -> [{new_best}]")
        return new_best
    else:
        logger.info(f"🛡️ 차이가 크지 않아 기존 [{ACTIVE_SECTOR}]를 유지합니다.")
        return ACTIVE_SECTOR

if __name__ == "__main__":
    import sys
    # --market korea|usa 인수 지원 (usa_scan_job 에서 usa 전달)
    _market = 'korea'
    for i, arg in enumerate(sys.argv[1:]):
        if arg == '--market' and i + 2 < len(sys.argv):
            _market = sys.argv[i + 2]
        elif arg in ('korea', 'usa'):
            _market = arg

    best = scan_best_sector(market=_market)
    if best and best != ACTIVE_SECTOR:
        update_active_sector(best)
        logger.info(f"⚙️ config.yaml 업데이트 완료 → {best} (market={_market})")
    else:
        logger.info(f"⚙️ 현재 섹터 유지 (config 업데이트 없음, market={_market})")