"""
매크로 지표 연속 수집기 — AI v4 피처(vix, sox_ret_5d, usdkrw_chg_5d, kospi_ret_20d) 유지용.

배경:
  stock.db(Stock_AI_Project)의 macro_indicators 는 2026-05 에서 수집이 끊겼다.
  AI 학습 피처가 거기 의존하므로, 같은 지표를 yfinance 로 이어서 수집한다.
  make_trades_history_v3 는 stock.db + 이 파일을 병합해 사용 (이 파일 우선).

수집: VIX(^VIX), SOX(^SOX), KRW_USD(KRW=X), KOSPI(^KS11), SP500(^GSPC), NASDAQ(^IXIC)
저장: ./macro_data/indicators.csv (date, indicator, close — stock.db 와 동일 스키마, 누적)

실행: python macro_collector.py            # 최근 30일 갱신 (멱등)
      python macro_collector.py 365        # 최근 N일 백필
매일 20:00 recollect_guard 가 자동 실행.
"""

import os
import sys
from datetime import datetime, timedelta

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("[ERROR] yfinance 미설치: pip install yfinance")
    sys.exit(1)

import config  # noqa: F401

OUT = "./macro_data/indicators.csv"
SYMBOLS = {
    "VIX": "^VIX",
    "SOX": "^SOX",
    "KRW_USD": "KRW=X",
    "KOSPI": "^KS11",
    "SP500": "^GSPC",
    "NASDAQ": "^IXIC",
}


def main():
    days = 30
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = []
    for name, sym in SYMBOLS.items():
        try:
            h = yf.download(sym, start=start, progress=False, auto_adjust=True)
            if h is None or h.empty:
                print(f"  [warn] {name}({sym}) 데이터 없음")
                continue
            close = h["Close"]
            if hasattr(close, "columns"):   # yfinance 멀티컬럼 대응
                close = close.iloc[:, 0]
            for dt, c in close.items():
                if pd.isna(c):
                    continue
                rows.append({"date": pd.Timestamp(dt).strftime("%Y-%m-%d"),
                             "indicator": name, "close": float(c)})
            print(f"  [ok] {name}: {len(close)}일")
        except Exception as e:
            print(f"  [warn] {name}({sym}) 실패: {e}")

    if not rows:
        print("[macro] 수집 0건 — 실패")
        sys.exit(1)

    new = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if os.path.exists(OUT):
        old = pd.read_csv(OUT)
        merged = pd.concat([old, new], ignore_index=True)
    else:
        merged = new
    merged = merged.drop_duplicates(subset=["date", "indicator"], keep="last")
    merged = merged.sort_values(["indicator", "date"])
    merged.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"[macro] {len(new)}행 갱신 → 누적 {len(merged):,}행: {OUT}")


if __name__ == "__main__":
    main()
