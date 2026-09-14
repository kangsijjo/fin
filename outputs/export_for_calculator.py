# -*- coding: utf-8 -*-
"""
export_for_calculator.py — 백테스트 검산기(backtest_calculator.html)에 넣을 CSV 를 만든다. (2026-09-13)

검산기는 브라우저에서 도는 단일 HTML 이라 SQLite(stock.db)를 직접 읽지 못한다.
이 스크립트가 DB 나 일봉 CSV 폴더에서 필요한 컬럼만 뽑아 **하나의 CSV** 로 저장한다.

두 가지 소스
  --source csv (기본, 권장) : outputs/macro_data/daily/*.csv — **거래대금이 실측값**이고 개인 순매수까지 들어 있다.
  --source db               : Stock_AI_Project/data/stock.db 의 korea_stocks + supply_demand 조인.
                              ⚠ 이 DB 에는 거래대금 컬럼이 없어 **거래량 × 종가로 근사**한다(실제와 다를 수 있음).

내보내는 컬럼(검산기가 자동 인식)
  code, date, open, high, low, close, trading_value(백만원), individual_net, foreign_net, inst_net

크기 조절 — 브라우저가 읽어야 하므로 무작정 크면 느리다.
  --min-tv  최소 거래대금(백만원, 기본 1000=10억). 유동성 없는 종목을 빼면 행이 1/4 로 준다.
  --start / --end   기간.
  권장: 최근 3~5년 + 최소 거래대금 10억 → 30~60MB, 브라우저에서 5~15초면 읽는다.

사용 예
  python export_for_calculator.py --start 20210101                      # 5년치(권장)
  python export_for_calculator.py --start 20240101 --min-tv 3000        # 가볍게
  python export_for_calculator.py --source db --start 20180101          # DB 에서(거래대금 근사)
"""
import os
import sys
import glob
import argparse
from datetime import datetime

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT_COLS = ["code", "date", "open", "high", "low", "close", "trading_value",
            "individual_net", "foreign_net", "inst_net"]


# 일봉 CSV 의 컬럼명은 2026-01-02 자로 한글 → 영문으로 바뀌었다.
#   ~2025-12-31 : code,open,high,low,close,volume,거래대금,등락률,시가총액,foreign_net,inst_net,date
#   2026-01-02~ : …,trading_value,change_pct,…
# 한쪽만 찾으면 7년치를 통째로 건너뛴다(2026-09-14 실측: 1,396개 중 1,225개 누락).
TV_ALIAS = ["trading_value", "거래대금"]


def from_csv(start, end, min_tv):
    files = sorted(glob.glob(os.path.join(HERE, "macro_data", "daily", "*.csv")))
    files = [f for f in files if start <= os.path.basename(f)[:8] <= end]
    if not files:
        sys.exit("해당 기간의 일봉 CSV 가 없습니다 — macro_data/daily 를 확인하세요.")
    print(f"[csv] 대상 파일 {len(files):,}개 ({os.path.basename(files[0])[:8]}~{os.path.basename(files[-1])[:8]})")
    parts, skipped = [], 0
    for i, f in enumerate(files, 1):
        try:
            d = pd.read_csv(f, dtype={"code": str})
        except Exception:
            skipped += 1
            continue
        tvcol = next((c for c in TV_ALIAS if c in d.columns), None)
        if tvcol is None:
            skipped += 1
            continue
        if tvcol != "trading_value":
            d = d.rename(columns={tvcol: "trading_value"})
        d = d[d["trading_value"] >= min_tv * 1e6]
        if d.empty:
            continue
        d["date"] = os.path.basename(f)[:8]
        parts.append(d[["code", "date", "open", "high", "low", "close", "trading_value"]
                       + [c for c in ("foreign_net", "inst_net", "individual_net") if c in d.columns]])
        if i % 200 == 0:
            print(f"  {i}/{len(files)} … 누적 {sum(len(p) for p in parts):,}행", flush=True)
    if not parts:
        sys.exit("조건을 만족하는 행이 없습니다 — --min-tv 를 낮춰보세요.")
    df = pd.concat(parts, ignore_index=True)
    if skipped:
        print(f"  ⚠ 건너뛴 파일 {skipped}개 (거래대금 컬럼을 못 찾음)")
    out = pd.DataFrame({
        "code": df["code"].astype(str).str.zfill(6),
        "date": df["date"],
        "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
        "trading_value": (df["trading_value"] / 1e6).round(0),
    })
    for c in ("foreign_net", "inst_net"):
        out[c] = (df[c] / 1e6).round(0) if c in df.columns else 0
    # 개인 순매수는 일봉 CSV 에 **없다** — DB(supply_demand) 에서 조인한다.
    out["individual_net"] = (df["individual_net"] / 1e6).round(0) if "individual_net" in df.columns else None
    if out["individual_net"].isna().all():
        out["individual_net"] = join_individual(out, start, end)
    return out


def join_individual(out, start, end):
    """supply_demand.individual_net_value 를 (code,date)로 조인. DB 가 없으면 0."""
    import sqlite3
    cands = [os.getenv("STOCK_DB", "")]
    try:
        from fin_paths import STOCK_DB
        cands.append(str(STOCK_DB))
    except Exception:
        pass
    cands.append(os.path.join(os.path.dirname(HERE), "Stock_AI_Project", "data", "stock.db"))
    path = next((p for p in cands if p and os.path.exists(p)), None)
    if not path:
        print("  [개인 순매수] stock.db 를 찾지 못해 0 으로 둡니다.")
        return 0
    print("  [개인 순매수] 일봉 CSV 에 없어 stock.db supply_demand 에서 조인합니다…", flush=True)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    sd = pd.read_sql(
        "SELECT ticker AS code, date, individual_net_value AS ind FROM supply_demand "
        "WHERE replace(date,'-','') BETWEEN ? AND ? AND individual_net_value IS NOT NULL",
        con, params=[start, end])
    con.close()
    if sd.empty:
        print("  [개인 순매수] DB 에 해당 기간 자료가 없어 0 으로 둡니다.")
        return 0
    sd["code"] = sd["code"].astype(str).str.zfill(6)
    sd["date"] = sd["date"].astype(str).str.replace("-", "").str[:8]
    sd["ind"] = (pd.to_numeric(sd["ind"], errors="coerce") / 1e6).round(0)
    m = out[["code", "date"]].merge(sd.drop_duplicates(["code", "date"]), on=["code", "date"], how="left")
    hit = m["ind"].notna().mean() * 100
    print(f"  [개인 순매수] {len(sd):,}행 중 매칭 {hit:.1f}%")
    return m["ind"].fillna(0).values


def from_db(start, end, min_tv):
    import sqlite3
    cands = [os.getenv("STOCK_DB", ""),
             os.path.join(os.path.dirname(HERE), "Stock_AI_Project", "data", "stock.db")]
    try:
        from fin_paths import STOCK_DB
        cands.insert(1, str(STOCK_DB))
    except Exception:
        pass
    path = next((p for p in cands if p and os.path.exists(p)), None)
    if not path:
        sys.exit("stock.db 를 찾지 못했습니다 — STOCK_DB 환경변수로 경로를 지정하세요.")
    print(f"[db] {path}")
    print("  ⚠ 이 DB 에는 거래대금 컬럼이 없어 **거래량 × 종가**로 근사합니다.")
    s, e = f"{start[:4]}-{start[4:6]}-{start[6:]}", f"{end[:4]}-{end[4:6]}-{end[6:]}"
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    df = pd.read_sql(
        "SELECT k.date, k.ticker AS code, k.Open AS open, k.High AS high, k.Low AS low, "
        "k.Close AS close, k.Volume AS volume, "
        "s.individual_net_value AS individual_net, s.foreign_net_value AS foreign_net, "
        "s.institution_net_value AS inst_net "
        "FROM korea_stocks k LEFT JOIN supply_demand s "
        "  ON k.ticker = s.ticker AND k.date = s.date "
        "WHERE k.date BETWEEN ? AND ? AND k.Close > 0 AND k.Volume > 0",
        con, params=[s, e])
    con.close()
    print(f"  원시 {len(df):,}행")
    df["trading_value"] = (df["volume"] * df["close"] / 1e6).round(0)
    df = df[df["trading_value"] >= min_tv]
    out = pd.DataFrame({
        "code": df["code"].astype(str).str.zfill(6),
        "date": df["date"].astype(str).str.replace("-", "").str[:8],
        "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
        "trading_value": df["trading_value"],
    })
    for c in ("individual_net", "foreign_net", "inst_net"):
        out[c] = (pd.to_numeric(df[c], errors="coerce").fillna(0) / 1e6).round(0)
    return out


def main():
    ap = argparse.ArgumentParser(description="백테스트 검산기용 CSV 추출")
    ap.add_argument("--source", default="csv", choices=["csv", "db"])
    ap.add_argument("--start", default="20210101")
    ap.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--min-tv", type=float, default=1000, help="최소 거래대금(백만원). 기본 1000 = 10억")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    out = from_csv(a.start, a.end, a.min_tv) if a.source == "csv" else from_db(a.start, a.end, a.min_tv)
    out = out.dropna(subset=["open", "high", "low", "close"])
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].round(0).astype("int64")
    out = out.sort_values(["code", "date"])

    path = a.out or os.path.join(HERE, f"calculator_data_{a.start}_{a.end}.csv")
    out.to_csv(path, index=False, encoding="utf-8-sig")
    mb = os.path.getsize(path) / 1e6
    print(f"\n저장 완료 → {path}")
    print(f"  {len(out):,}행 · 종목 {out['code'].nunique():,}개 · 거래일 {out['date'].nunique():,}일 · {mb:.1f} MB")
    if mb > 80:
        print("  ⚠ 파일이 큽니다. 브라우저가 느릴 수 있으니 --min-tv 를 올리거나 --start 를 늦춰 보세요.")
    print("\n이 파일을 backtest_calculator.html 의 '데이터' 영역에 끌어다 놓으면 됩니다.")


if __name__ == "__main__":
    main()
