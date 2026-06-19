import os
import pandas as pd
import numpy as np
import lightgbm as lgb
from src.config_db import get_connection
from src.logger import get_logger

logger = get_logger('walk_forward')

def run_walk_forward_backtest(sector=None, market=None, train_weeks=104, test_weeks=1):
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

    conn = get_connection()
    table = 'korea_indicators' if market == 'korea' else 'usa_indicators'
    finrl_table = 'finrl_dataset_kr' if market == 'korea' else 'finrl_dataset_us'

    logger.info(f"=== [{sector}] 주간 Walk-Forward (증분 업데이트 모드) 시작 ===")

    # 1. 기존 DB 확인 (내일부터는 여기서 건너뛰기가 작동함)
    # 단, BACKTEST_REBUILD 환경변수가 설정되어 있으면 해당 섹터의 기존 결과를
    # 일괄 삭제하고 처음부터 다시 계산한다 (purge gap 적용 후 재시뮬용).
    last_saved_date = None
    if os.environ.get('BACKTEST_REBUILD'):
        try:
            conn.execute(f"DELETE FROM {finrl_table} WHERE sector=?", (sector,))
            conn.commit()
            logger.info(f"🧹 [{sector}] 기존 백테스트 결과 삭제 - 처음부터 재계산")
        except Exception as e:
            logger.warning(f"기존 결과 삭제 실패(스킵): {e}")
    else:
        try:
            res = pd.read_sql(f"SELECT MAX(date) as max_date FROM {finrl_table} WHERE sector=?", conn, params=[sector])
            if not res.empty and res['max_date'].iloc[0] is not None:
                last_saved_date = pd.to_datetime(res['max_date'].iloc[0])
                logger.info(f"📍 DB 확인 완료: {last_saved_date.date()} 까지 이미 학습/저장되어 있습니다.")
        except:
            pass

    # 2. 지표 데이터 로드
    try:
        df = pd.read_sql(f"SELECT * FROM {table} WHERE sector=? ORDER BY date, ticker", conn, params=[sector])
    except Exception as e:
        logger.error(f"데이터 로드 실패: {e}")
        conn.close()
        return

    if df.empty:
        conn.close()
        return

    df['date'] = pd.to_datetime(df['date'])

    # ───────────────────────────────────────────────────────────────────────
    # [현실화] 시그널은 T 종가 정보, 체결은 T+1 시가, 청산은 T+홀딩기간 종가.
    # 학습 타깃과 정합하도록 HOLDING_DAYS=5 로 사용한다.
    # entry_price : T+1 시가 (Open.shift(-1))
    # exit_price  : T+HOLDING 종가 (Close.shift(-HOLDING))
    # ───────────────────────────────────────────────────────────────────────
    HOLDING_DAYS = 5

    g = df.sort_values(['ticker', 'date']).groupby('ticker', sort=False)
    df['entry_price'] = g['Open'].shift(-1)
    df['exit_price']  = g['Close'].shift(-HOLDING_DAYS)

    # 데이터 위생 가드:
    # 1) entry_price <= 0 (가격 데이터 오류 / 액면분할 직후 등) → 거래 불가
    # 2) trade_return_gross 극단치(±50% 초과)는 수정주가 누락 가능성 → 제외
    # 두 경우 모두 NaN 처리해서 이후 dropna 에서 자동 제거되도록 함.
    bad_entry = ~(df['entry_price'] > 0)
    df.loc[bad_entry, ['entry_price', 'exit_price']] = np.nan

    df['trade_return_gross'] = df['exit_price'] / df['entry_price'] - 1
    # 5일 변동 ±15% 초과는 액면분할/유상증자/거래정지 등 비정상 이벤트로 간주.
    # 한국 시장 5일 변동성 분포의 상위 1% 수준이므로 정상 거래는 거의 영향 없음.
    extreme = df['trade_return_gross'].abs() > 0.15
    df.loc[extreme, 'trade_return_gross'] = np.nan

    # inf 도 안전망으로 NaN 변환 (0 나눗셈 등이 남았을 경우)
    df['trade_return_gross'] = df['trade_return_gross'].replace(
        [np.inf, -np.inf], np.nan
    )
    # 호환을 위해 next_return 이름은 유지하되, 의미를 'HOLDING_DAYS 누적 수익'으로 재정의
    df['next_return'] = df['trade_return_gross']

    # 타깃: HOLDING_DAYS 누적 수익 기준 (학습과 동일 horizon)
    df['target'] = 0
    df.loc[df['trade_return_gross'] >=  0.02, 'target'] = 1
    df.loc[df['trade_return_gross'] <= -0.02, 'target'] = 2
    df = df.dropna(subset=['entry_price', 'exit_price', 'next_return', 'target'])

    # 피처 컬럼: FEATURE_COLS 를 우선 사용하여 train.py 와 완전히 일치시킨다.
    # DB에 lgbm_prob, lgbm_pred 등 이전 실행 컬럼이 남아있어도 학습 피처로 오염되지 않음.
    from src.processor.indicators import FEATURE_COLS as _FEAT
    feature_cols = [c for c in _FEAT if c in df.columns]
    # FEATURE_COLS 에 없는 신규 컬럼이 DB 에 추가됐을 경우를 대비한 경고
    missing_feat = [c for c in _FEAT if c not in df.columns]
    if missing_feat:
        logger.warning(f"[{sector}] 백테스트 피처 누락(0 처리): {missing_feat}")
        for c in missing_feat:
            df[c] = 0.0
        feature_cols = list(_FEAT)
    df = df.dropna(subset=feature_cols)

    if df.empty:
        logger.warning(f"[{sector}] 유효한 지표 데이터가 부족하여 스킵합니다.")
        return pd.DataFrame()

    min_date = df['date'].min()
    max_date = df['date'].max()

    if pd.isna(min_date) or pd.isna(max_date):
        return pd.DataFrame()

    total_days = (max_date - min_date).days
    total_windows = max(1, (total_days // 7) - train_weeks)

    finrl_data_list = []
    current_train_start = min_date

    # 3. Walk-Forward 핵심 루프 (주 단위) — 줄바꿈 로그 방식
    window_num = 0
    log_step = max(1, total_windows // 20)   # 5% 단위로 로그 (최대 20줄)
    logger.info(f"Walk-Forward 시작: 총 {total_windows}주 윈도우")

    while True:
        current_train_end = current_train_start + pd.DateOffset(weeks=train_weeks) - pd.Timedelta(days=1)
        current_test_end = current_train_end + pd.DateOffset(weeks=test_weeks)

        if current_train_end >= max_date:
            break

        window_num += 1

        # 💡 증분 업데이트: 기존에 했던 기간이면 고속 스킵!
        if last_saved_date is not None and current_test_end <= last_saved_date:
            current_train_start += pd.DateOffset(weeks=test_weeks)
            if window_num % log_step == 0 or window_num == total_windows:
                logger.info(f"  Walk-Forward [{window_num}/{total_windows}] {window_num/total_windows*100:.0f}% — 기존 데이터 스킵")
            continue

        # ── PURGE GAP (lookahead bias 차단) ──
        HOLDING_DAYS = 5
        purge_cutoff = current_train_end - pd.tseries.offsets.BDay(HOLDING_DAYS)
        train_df = df[(df['date'] >= current_train_start) & (df['date'] <= purge_cutoff)]
        test_df = df[(df['date'] > current_train_end) & (df['date'] <= current_test_end)]

        if last_saved_date is not None:
            test_df = test_df[test_df['date'] > last_saved_date]

        if len(train_df) > 100 and len(test_df) > 0:
            X_train, y_train = train_df[feature_cols], train_df['target']
            X_test = test_df[feature_cols]

            # config.yaml model 섹션 파라미터 사용 (train.py 와 동일하게 통일)
            try:
                from src.collector.config import MODEL_CONFIG
                _mc = MODEL_CONFIG or {}
            except Exception:
                _mc = {}
            _lgbm_params = dict(
                n_estimators=int(_mc.get('n_estimators', 100)),
                learning_rate=float(_mc.get('learning_rate', 0.05)),
                max_depth=int(_mc.get('max_depth', 6)),
                num_leaves=int(_mc.get('num_leaves', 31)),
                random_state=42,
                verbose=-1,
            )
            model = lgb.LGBMClassifier(**_lgbm_params)
            model.fit(X_train, y_train)

            test_df_copy = test_df.copy()
            class_1_idx = list(model.classes_).index(1) if 1 in model.classes_ else 0
            test_df_copy['lgbm_prob'] = model.predict_proba(X_test)[:, class_1_idx]
            test_df_copy['lgbm_pred'] = model.predict(X_test)

            finrl_data_list.append(test_df_copy)

        current_train_start += pd.DateOffset(weeks=test_weeks)

        if window_num % log_step == 0 or window_num == total_windows:
            logger.info(
                f"  Walk-Forward [{window_num}/{total_windows}] {window_num/total_windows*100:.0f}% "
                f"— 학습 {len(train_df)}행 / 테스트 {len(test_df)}행"
            )

    logger.info(f"Walk-Forward 완료: {window_num}/{total_windows}주 처리")

    # 4. 신규 데이터 이어 붙이기 (Append)
    if finrl_data_list:
        final_df = pd.concat(finrl_data_list, ignore_index=True)
        finrl_df = final_df.copy()
        finrl_df = finrl_df.rename(columns={'ticker': 'tic', 'Close': 'close', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Volume': 'volume'})
        finrl_df['date'] = finrl_df['date'].dt.strftime('%Y-%m-%d')
        # 백테스트 내부 파생 컬럼은 저장 대상 아님 — 기존 finrl_dataset_* 스키마에 없음.
        finrl_df = finrl_df.drop(
            columns=['entry_price', 'exit_price', 'trade_return_gross'],
            errors='ignore'
        )
        finrl_df.to_sql(finrl_table, conn, if_exists='append', index=False)
        conn.commit()
        logger.info(f"✨ 신규 데이터 {len(finrl_df)}행 DB 추가 완료!")
    else:
        logger.info("✔️ 이미 모든 백테스트 데이터가 최신 상태입니다. (신규 학습 스킵)")

    # 5. 누적 성과 계산 — 수수료/세금/슬리피지를 매수·매도 양방향으로 분리 적용
    #   - 매수: 수수료 BUY_FEE
    #   - 매도: 수수료 SELL_FEE + 거래세 TAX (KR 기준 0.18% 가정)
    #   - 슬리피지: 진입/청산 각각 SLIPPAGE
    #   - 보유기간: HOLDING_DAYS 영업일 동안 같은 종목을 들고 있으므로,
    #     동일 종목이 연속 선택되어도 만기 시점에 청산-재진입이 발생한다고 본다.
    BUY_FEE   = 0.00015          # 매수 수수료 0.015%
    SELL_FEE  = 0.00015          # 매도 수수료 0.015%
    TAX       = 0.0018           # 매도 시 거래세 0.18% (KR 가정, US 는 0 으로 설정 가능)
    SLIPPAGE  = 0.0010           # 진입/청산 각각 0.1%
    if market == 'usa':
        TAX = 0.0

    cost_one_trip = BUY_FEE + SELL_FEE + TAX + 2 * SLIPPAGE  # 매수+매도 1세트
    logger.info(
        f"=== 누적 성과 (T+1 시가 진입, {HOLDING_DAYS}영업일 보유, "
        f"round-trip 비용 {cost_one_trip*100:.3f}%) ==="
    )

    full_df = pd.read_sql(f"SELECT * FROM {finrl_table} WHERE sector=?", conn, params=[sector])
    conn.close()

    if full_df.empty:
        return pd.DataFrame()

    full_df['date'] = pd.to_datetime(full_df['date'])

    # ── 비중복 거래로 재구성 ──
    # 매일 새로운 진입을 가정하면 동시에 HOLDING_DAYS 개의 거래가 평행으로 돌게 되어
    # 일별 단순 합산이 부풀려진다. 진입 후에는 만기까지 신규 진입을 금지하여
    # 한 시점에 1개 포지션만 유지하는 가장 보수적인 시뮬레이션으로 통일.
    daily_portfolio = []
    cooldown_until = None
    all_dates = sorted(full_df['date'].unique())

    for date in all_dates:
        if cooldown_until is not None and date <= cooldown_until:
            daily_portfolio.append({'date': date, 'daily_return': 0.0})
            continue

        group = full_df[full_df['date'] == date]
        top_stock = group.nlargest(1, 'lgbm_prob')
        # config 의 prob_threshold 와 동일한 기준 사용 (0.55).
        # 모델 확신이 강한 거래만 잡아 노이즈/leakage 영향 축소.
        if not top_stock.empty and float(top_stock.iloc[0]['lgbm_prob']) >= 0.55:
            gross = float(top_stock.iloc[0]['next_return'])  # HOLDING_DAYS 누적
            net = gross - cost_one_trip
            # 진입일은 0%, 만기일에 누적수익 한 번에 인식 (또는 평균 분배).
            # 여기서는 평균 분배가 그래프 부드럽게 나옴.
            per_day = (1 + net) ** (1 / HOLDING_DAYS) - 1
            for k in range(HOLDING_DAYS):
                d = pd.Timestamp(date) + pd.tseries.offsets.BDay(k)
                daily_portfolio.append({'date': d, 'daily_return': per_day})
            cooldown_until = pd.Timestamp(date) + pd.tseries.offsets.BDay(HOLDING_DAYS - 1)
        else:
            daily_portfolio.append({'date': date, 'daily_return': 0.0})

    portfolio = pd.DataFrame(daily_portfolio)
    # 중복 날짜 안전 처리(쿨다운 행과 만기 분배 행이 겹칠 수 있음).
    portfolio = (
        portfolio.groupby('date', as_index=False)['daily_return']
                 .sum()
                 .sort_values('date')
                 .reset_index(drop=True)
    )
    # inf/NaN 안전망: 이상치가 남아 들어와도 누적이 발산하지 않도록 차단.
    portfolio['daily_return'] = (
        portfolio['daily_return']
        .replace([np.inf, -np.inf], 0.0)
        .fillna(0.0)
        .clip(lower=-0.5, upper=0.5)
    )
    portfolio['cum_return'] = (1 + portfolio['daily_return']).cumprod()

    total_return = (portfolio['cum_return'].iloc[-1] - 1) * 100
    mdd = (portfolio['cum_return'] / portfolio['cum_return'].cummax() - 1).min() * 100
    std = portfolio['daily_return'].std()
    sharpe = (portfolio['daily_return'].mean() / std * np.sqrt(252)) if std > 0 else 0

    print(f"\n=== [{sector}] 실전 검증 완료 ===")
    print(f"총 수익률: {total_return:.2f}% | MDD: {mdd:.2f}% | 샤프비율: {sharpe:.2f}")

    return portfolio

if __name__ == "__main__":
    import sys
    from src.collector.config import ACTIVE_SECTOR, KOREA_SECTORS
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'all':
            logger.info("🚀 전 섹터 주간 Walk-Forward 백테스트 가동")
            for s in KOREA_SECTORS:
                run_walk_forward_backtest(sector=s)
        else:
            run_walk_forward_backtest(sector=cmd)
    else:
        run_walk_forward_backtest(sector=ACTIVE_SECTOR)