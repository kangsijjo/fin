"""
종목 코드 → 종목명 캐시 빌더 (FinanceDataReader 기반).

pykrx 는 KRX 서버 통신 불안정 → FinanceDataReader 의 StockListing 사용 (Naver Finance).

출력: ./name_cache.csv
컬럼: code,name,market
"""

import os
import csv
import sys

try:
    import FinanceDataReader as fdr
except ImportError:
    print("[ERROR] FinanceDataReader 미설치.")
    print("  설치: pip install finance-datareader")
    sys.exit(1)

OUT = "./name_cache.csv"


def build():
    mapping = {}

    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = fdr.StockListing(market)
            # 컬럼명은 fdr 버전에 따라 'Code'/'Symbol', 'Name' 등 다를 수 있음
            code_col = next((c for c in ("Code", "Symbol") if c in df.columns), None)
            name_col = next((c for c in ("Name",) if c in df.columns), None)
            if not code_col or not name_col:
                print(f"  [warn] {market}: 컬럼 못 찾음. columns={list(df.columns)[:8]}")
                continue
            print(f"[{market}] tickers: {len(df)}")
            for _, r in df.iterrows():
                code = str(r[code_col]).zfill(6)
                name = str(r[name_col]).strip()
                if name and name != "nan":
                    mapping[code] = (name, market)
        except Exception as e:
            print(f"  [warn] {market} 실패: {e}")

    if not mapping:
        # fallback: KRX 전체
        try:
            df = fdr.StockListing("KRX")
            code_col = next((c for c in ("Code", "Symbol") if c in df.columns), None)
            name_col = "Name" if "Name" in df.columns else None
            mkt_col = "Market" if "Market" in df.columns else None
            if code_col and name_col:
                print(f"[KRX fallback] tickers: {len(df)}")
                for _, r in df.iterrows():
                    code = str(r[code_col]).zfill(6)
                    name = str(r[name_col]).strip()
                    mkt = str(r[mkt_col]) if mkt_col else ""
                    if name and name != "nan":
                        mapping[code] = (name, mkt)
        except Exception as e:
            print(f"  [warn] KRX fallback 실패: {e}")

    print(f"[total] {len(mapping)} 종목명 수집")

    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "market"])
        for code, (name, mkt) in sorted(mapping.items()):
            w.writerow([code, name, mkt])

    print(f"[saved] {OUT}")


def load_name_cache(path=OUT):
    """code → name dict 반환. 없으면 {}."""
    if not os.path.exists(path):
        return {}
    import pandas as pd
    df = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
    df["code"] = df["code"].astype(str).str.zfill(6)
    return dict(zip(df["code"], df["name"]))


if __name__ == "__main__":
    build()
