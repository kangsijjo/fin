# check_nan.py
from src.config_db import get_connection
import pandas as pd
from src.processor.indicators import FEATURE_COLS

conn = get_connection()

# 반도체 섹터만 확인 (데이터가 확실히 있는 섹터)
df = pd.read_sql(
    "SELECT * FROM korea_indicators WHERE sector='반도체' LIMIT 10000", conn)
print(f"반도체 행수: {len(df)}")
null_pct = df[FEATURE_COLS].isnull().mean().sort_values(ascending=False)
print(null_pct[null_pct > 0])

# supply_demand 직접 확인 (반도체 종목 1개)
ticker = df['ticker'].iloc[0] if not df.empty else None
if ticker:
    sd = pd.read_sql(
        f"SELECT COUNT(*) as cnt FROM supply_demand WHERE ticker='{ticker}'", conn)
    print(f"\n{ticker} supply_demand 행수: {sd['cnt'].iloc[0]}")

conn.close()