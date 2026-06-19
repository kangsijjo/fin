import pandas as pd
import numpy as np
from src.config_db import get_connection
from src.logger import get_logger

logger = get_logger('rule_trader')

# 🎯 전략 파라미터
BUY_THRESHOLD = 0.55  # 매수 기준 확률 (55% 이상)
FEE_RATE = 0.0035     # 수수료 0.35%
TEST_START_DATE = '2024-01-01' # 🚨 검증 시작일 (데이터 누수 방지)

def run_lgbm_trader(sector=None, market='korea'):
    from src.collector.config import ACTIVE_SECTOR
    if sector is None:
        sector = ACTIVE_SECTOR

    conn = get_connection()
    table = 'finrl_dataset_kr' if market == 'korea' else 'finrl_dataset_us'

    # 1. 데이터 로드
    df = pd.read_sql(f"SELECT * FROM {table} WHERE sector=? ORDER BY date", conn, params=[sector])
    
    if df.empty:
        logger.error(f"[{sector}] 데이터가 없습니다.")
        conn.close()
        return

    df['date'] = pd.to_datetime(df['date'])
    
    # 2. 매매 시뮬레이션
    daily_portfolio = []
    current_holding_ticker = None

    for date, group in df.groupby('date'):
        top_stock = group.nlargest(1, 'lgbm_prob')
        
        if not top_stock.empty and float(top_stock.iloc[0]['lgbm_prob']) >= BUY_THRESHOLD:
            best_ticker = top_stock.iloc[0]['tic']
            day_return = float(top_stock.iloc[0]['next_return'])
            
            if current_holding_ticker != best_ticker:
                day_return -= FEE_RATE
                current_holding_ticker = best_ticker
        else:
            day_return = 0.0 
            current_holding_ticker = None
                
        daily_portfolio.append({'date': date, 'daily_return': day_return, 'sector': sector})

    # 3. 2024년 이후 데이터만 필터링 (검증 기간 분리)
    portfolio = pd.DataFrame(daily_portfolio)
    portfolio = portfolio[portfolio['date'] >= TEST_START_DATE].copy()
    
    if portfolio.empty:
        logger.warning(f"[{sector}] {TEST_START_DATE} 이후 데이터가 부족합니다.")
        conn.close()
        return

    # 누적 수익률 계산
    portfolio['cum_return'] = (1 + portfolio['daily_return']).cumprod()

    # 4. 승률(Win Rate) 계산
    # 실제로 주식을 보유했던 날(거래일) 중 수익이 난 날의 비율
    trade_days = portfolio[portfolio['daily_return'] != 0]
    win_rate = (len(trade_days[trade_days['daily_return'] > 0]) / len(trade_days)) * 100 if len(trade_days) > 0 else 0

    # 5. 주요 지표 계산
    total_return = (portfolio['cum_return'].iloc[-1] - 1) * 100
    mdd = (portfolio['cum_return'] / portfolio['cum_return'].cummax() - 1).min() * 100
    sharpe = (portfolio['daily_return'].mean() / portfolio['daily_return'].std() * np.sqrt(252)) if portfolio['daily_return'].std() > 0 else 0

    # 6. 결과 DB 저장 (대시보드 연동용)
    # 기존 해당 섹터 결과가 있다면 삭제 후 저장 (테이블이 없으면 패스)
    try:
        conn.execute("DELETE FROM backtest_results WHERE sector=?", (sector,))
    except Exception:
        pass 
        
    portfolio.to_sql('backtest_results', conn, if_exists='append', index=False)
    conn.commit()
    conn.close()

    print(f"\n✅ [{sector}] 검증 완료 ({TEST_START_DATE} ~ )")
    print(f"--------------------------------------------------")
    print(f"총 수익률: {total_return:.2f}% | MDD: {mdd:.2f}%")
    print(f"승률: {win_rate:.2f}% | 샤프비율: {sharpe:.2f}")
    print(f"--------------------------------------------------")

if __name__ == "__main__":
    import sys
    from src.collector.config import KOREA_SECTORS
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if cmd == 'all':
        for s in KOREA_SECTORS: run_lgbm_trader(sector=s)
    else:
        run_lgbm_trader(sector=cmd if cmd != 'run' else None)