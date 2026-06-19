# check_result.py 로 저장 후 실행
from src.config_db import get_connection
import pandas as pd
from src.processor.indicators import FEATURE_COLS

conn = get_connection()

print('=== 섹터별 종목수 ===')
df = pd.read_sql('SELECT sector, COUNT(DISTINCT ticker) as cnt FROM korea_stocks GROUP BY sector ORDER BY cnt DESC LIMIT 15', conn)
print(df.to_string())

print('\n=== 피처 NaN 비율 ===')
df2 = pd.read_sql('SELECT * FROM korea_indicators LIMIT 100000', conn)
null_pct = df2[FEATURE_COLS].isnull().mean().sort_values(ascending=False)
print(null_pct[null_pct > 0.01])

conn.close()