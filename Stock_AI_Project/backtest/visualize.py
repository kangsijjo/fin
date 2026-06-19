import matplotlib
matplotlib.rcParams['font.family'] = 'Malgun Gothic'  # 윈도우 한글 폰트
matplotlib.rcParams['axes.unicode_minus'] = False
import backtrader as bt
import pandas as pd
import sqlite3
import os
import pickle
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from src.config_db import get_db_path
DB_PATH = get_db_path()

from src.config_db import get_connection

def load_ticker_data(ticker, market='korea'):
    conn = get_connection()
    table = 'korea_stocks' if market == 'korea' else 'usa_stocks'
    df = pd.read_sql(
        f"SELECT * FROM {table} WHERE ticker=? ORDER BY date",
        conn, params=[ticker]
    )
    conn.close()
    return df

class MLStrategy(bt.Strategy):
    params = (
        ('model', None),
        ('prob_threshold', 0.6),
        ('printlog', False),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None
        self.bar_count = 0
        self.buy_signals = []
        self.sell_signals = []
        self.portfolio_values = []
        self.dates = []

    def make_features(self):
        """현재 시점 피처 생성"""
        closes = [self.dataclose[-i] for i in range(61)]
        closes = list(reversed(closes))
        
        if len(closes) < 61:
            return None
        
        close_series = pd.Series(closes)
        
        return_1d  = (closes[-1] - closes[-2]) / closes[-2]
        return_5d  = (closes[-1] - closes[-6]) / closes[-6]
        return_20d = (closes[-1] - closes[-21]) / closes[-21]
        
        ma5  = close_series[-5:].mean()
        ma20 = close_series[-20:].mean()
        ma60 = close_series[-60:].mean()
        
        ma5_ratio  = closes[-1] / ma5
        ma20_ratio = closes[-1] / ma20
        ma60_ratio = closes[-1] / ma60
        
        delta = close_series.diff()
        gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
        loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
        rsi  = 100 - (100 / (1 + gain / loss)) if loss != 0 else 50
        
        volumes = [self.datas[0].volume[-i] for i in range(21)]
        volumes = list(reversed(volumes))
        vol_ratio  = volumes[-1] / np.mean(volumes[-20:]) if np.mean(volumes[-20:]) > 0 else 1
        
        returns    = close_series.pct_change()
        volatility = returns[-20:].std()
        
        # 거시지표
        from src.collector.macro import load_macro_features
        current_date = str(self.datas[0].datetime.date(0))
        macro = load_macro_features(current_date)
        
        nasdaq_chg  = macro.get('NASDAQ', 0.0)
        sox_chg     = macro.get('SOX', 0.0)
        krw_usd_chg = macro.get('KRW_USD', 0.0)
        vix_chg     = macro.get('VIX', 0.0)
        kospi_chg   = macro.get('KOSPI', 0.0)
        
        return [[return_1d, return_5d, return_20d,
                ma5_ratio, ma20_ratio, ma60_ratio,
                rsi, vol_ratio, volatility,
                nasdaq_chg, sox_chg, krw_usd_chg,
                vix_chg, kospi_chg, 0.0]]  # 마지막 0.0은 news_sentiment
    def next(self):
        self.bar_count += 1
        self.portfolio_values.append(self.broker.getvalue())
        self.dates.append(self.datas[0].datetime.date(0))

        if self.bar_count < 61:
            return
        if self.order:
            return

        features = self.make_features()
        if features is None:
            return
        probs     = self.params.model.predict_proba(features)[0]
        buy_prob  = probs[1]
        sell_prob = probs[2]

        if not self.position:
            if buy_prob >= 0.5:
                self.order = self.buy()
                if self.params.printlog:
                    print(f'매수: {self.datas[0].datetime.date(0)} 가격: {self.dataclose[0]:.0f} 매수확률: {buy_prob:.2f}')
        else:
            if sell_prob >= 0.5:
                self.order = self.sell()
                if self.params.printlog:
                    print(f'매도: {self.datas[0].datetime.date(0)} 가격: {self.dataclose[0]:.0f} 매도확률: {sell_prob:.2f}')

    def notify_order(self, order):
        if order.status in [order.Completed]:
            self.order = None

def run_visualization(ticker, sector, market='korea', cash=10000000):
    print(f"\n=== {ticker} 백테스트 시각화 ===")

    # 모델 로드
    model_path = f'src/models/saved/{sector}_model.pkl'
    if not os.path.exists(model_path):
        print(f"모델 없음: {model_path}")
        return

    with open(model_path, 'rb') as f:
        obj = pickle.load(f)
    model = obj['model'] if isinstance(obj, dict) and 'model' in obj else obj

    # 데이터 로드
    df = load_ticker_data(ticker, market)
    if len(df) == 0:
        print("데이터 없음")
        return

    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date')
    df = df.rename(columns={
        'Open': 'open', 'High': 'high',
        'Low': 'low', 'Close': 'close', 'Volume': 'volume'
    })
    df = df[['open', 'high', 'low', 'close', 'volume']].dropna()

    data = bt.feeds.PandasData(dataname=df)

    cerebro = bt.Cerebro()
    cerebro.adddata(data)
    cerebro.addstrategy(MLStrategy, model=model, prob_threshold=0.6)
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=0.0015)
    cerebro.addsizer(bt.sizers.PercentSizer, percents=95)

    results = cerebro.run()
    strat = results[0]

    # 시각화
    fig, axes = plt.subplots(3, 1, figsize=(14, 10))
    fig.suptitle(f'{ticker} ({sector}) 백테스트 결과', fontsize=14)

    dates = strat.dates
    portfolio_values = strat.portfolio_values

    # 1. 포트폴리오 수익률
    ax1 = axes[0]
    portfolio_returns = [(v - cash) / cash * 100 for v in portfolio_values]
    ax1.plot(dates, portfolio_returns, 'b-', linewidth=1.5, label='AI 전략')

    # 벤치마크 (Buy & Hold)
    bh_returns = [(df['close'].iloc[i] / df['close'].iloc[0] - 1) * 100
                  for i in range(len(df['close']))]
    ax1.plot(df.index[:len(dates)], bh_returns[:len(dates)], 'gray',
             linewidth=1, linestyle='--', label='Buy & Hold')
    ax1.set_ylabel('수익률 (%)')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax1.axhline(y=0, color='black', linewidth=0.5)

    # 2. 주가 + 매수/매도 시점
    ax2 = axes[1]
    ax2.plot(df.index[:len(dates)], df['close'].values[:len(dates)],
             'k-', linewidth=1, label='주가')

    if strat.buy_signals:
        buy_dates, buy_prices = zip(*strat.buy_signals)
        ax2.scatter(buy_dates, buy_prices, color='red', marker='^',
                    s=50, zorder=5, label='매수')

    if strat.sell_signals:
        sell_dates, sell_prices = zip(*strat.sell_signals)
        ax2.scatter(sell_dates, sell_prices, color='blue', marker='v',
                    s=50, zorder=5, label='매도')

    ax2.set_ylabel('주가')
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    # 3. MDD
    ax3 = axes[2]
    portfolio_series = pd.Series(portfolio_values, index=dates)
    rolling_max = portfolio_series.expanding().max()
    drawdown = (portfolio_series - rolling_max) / rolling_max * 100
    ax3.fill_between(dates, drawdown, 0, color='red', alpha=0.3, label='MDD')
    ax3.set_ylabel('낙폭 (%)')
    ax3.legend()
    ax3.grid(True, alpha=0.3)

    plt.tight_layout()

    # 저장
    os.makedirs('backtest/charts', exist_ok=True)
    chart_path = f'backtest/charts/{ticker}_{sector}.png'
    plt.savefig(chart_path, dpi=100, bbox_inches='tight')
    print(f"차트 저장: {chart_path}")
    plt.show()

    # 결과 출력
    final_value = cerebro.broker.getvalue()
    total_return = (final_value - cash) / cash * 100
    print(f"\n최종 자금: {final_value:,.0f}원")
    print(f"총 수익률: {total_return:.2f}%")
    print(f"매수 횟수: {len(strat.buy_signals)}회")
    print(f"매도 횟수: {len(strat.sell_signals)}회")

if __name__ == "__main__":
    from src.collector.config import ACTIVE_SECTOR
    usa_sectors = ['Industrials', 'Financials', 'Information Technology',
                   'Health Care', 'Consumer Discretionary', 'Consumer Staples',
                   'Utilities', 'Real Estate', 'Materials',
                   'Communication Services', 'Energy']
    market = 'usa' if ACTIVE_SECTOR in usa_sectors else 'korea'

    # 기본 종목으로 시각화
    if market == 'korea':
        ticker = '005930'  # 삼성전자
    else:
        ticker = 'XOM'  # 에너지 섹터 대표

    run_visualization(ticker, ACTIVE_SECTOR, market)