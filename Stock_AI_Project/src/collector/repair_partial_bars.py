# -*- coding: utf-8 -*-
"""
repair_partial_bars.py — 장중에 저장된 '미완성 일봉'(korea_stocks)을 다시 받아 바로잡는다. (2026-11-02, 설명서 §92)

왜: 주가 수집(main_collector)이 09:00 를 넘겨 돌면 그날 장중 값을 일봉으로 저장하고 다시 안 고쳤다
    (10-08 코스닥 1,708종목 · 10-29 643종목 거래량이 KRX 확정치의 90% 미만 — 실측). 기술지표가 이 값으로 계산된다.
    수집기는 §92 에서 고쳐 앞으로는 안 생기고(확정일까지만 + 마지막 날 덮어쓰기), 이 스크립트는 지난 것을 한 번 고친다.

방법
  ① 의심 날짜: outputs/macro_data/daily/{일자}.csv(KRX 확정 거래량, 코스닥)와 비교해 거래량 90% 미만 종목 수가
     그 무렵 보통 수준보다 50 이상 튄 날(또는 --dates 로 직접 지정)
  ② 그 날짜에 행이 있는 한국 종목 전부(코스피 포함 — 코스피는 비교 기준이 없어 다시 받은 값으로 판단)를 FDR 로 다시 받아
     **미완성으로 보이는 행만**(저장된 거래량 < 다시 받은 거래량 × 0.9) 가격 칸(Open·High·Low·Close·Volume·Change)을 덮어씀.
     종가가 31% 넘게 다르면(액면분할 등 수정주가 의심) 건너뜀 — 과거 행에 수정주가가 섞이지 않게.
  ③ 기본은 점검만(무엇이 바뀔지 출력). --apply 면 바꾸기 전 원래 행을 C:\\fin\\_archive\\YYYYMMDD\\ 에 CSV 로 백업(MANIFEST.csv) 후 덮어씀.
  ④ 끝나면 대시보드 데이터+로그 탭 [기술지표] 버튼(45~90분)으로 지표를 다시 계산.

실행(C:\\fin\\Stock_AI_Project 에서):
  venv\\Scripts\\python -m src.collector.repair_partial_bars            # 점검만
  venv\\Scripts\\python -m src.collector.repair_partial_bars --apply    # 백업 후 덮어쓰기(약 20~30분)
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime

import pandas as pd

from src.config_db import ROOT_DIR, get_connection
from src.collector.db import PRICE_COLS

FIN = os.path.dirname(ROOT_DIR)
MACRO_DAILY = os.path.join(FIN, "outputs", "macro_data", "daily")
VOL_RATIO = 0.9          # DB 거래량 < KRX 확정 거래량 × 0.9 → 미완성 의심
MIN_BAD = 20             # 그런 종목이 이 수 이상이고
EXCESS = 50              # 그 무렵 보통 수준(앞뒤 21일 중앙값)보다 이만큼 많은 날만 의심 날짜
MAX_CLOSE_DIFF = 0.31    # 다시 받은 종가가 이보다 많이 다르면(가격제한폭 30% 초과) 수정주가 의심 → 건너뜀


def suspect_dates(con, since, macro_dir=MACRO_DAILY, min_bad=MIN_BAD, excess=EXCESS):
    """[(날짜 YYYY-MM-DD, 비교 종목 수, 미완성 의심 수)] — KRX 확정 거래량(macro_data)과 비교.
    평소에도 출처 차이로 몇십 종목은 거래량이 다르다(2024년 하루 ~100 → 2026-04 이후 ~5). 그래서 '그 무렵 보통 수준(앞뒤 21일 중앙값)
    보다 excess 이상 튄 날'만 의심 날짜로 본다 — 실측 튄 날: 05-13·05-27·06-30(1,600~1,700) · 07-14(744) · 10-08(1,708) · 10-29(643)."""
    ks = pd.read_sql("SELECT date, ticker, Volume FROM korea_stocks WHERE date >= ?", con, params=[since])
    ks["d"] = ks["date"].astype(str).str.replace("-", "").str[:8]
    rows = []
    for d, g in ks.groupby("d"):
        p = os.path.join(macro_dir, f"{d}.csv")
        if not os.path.exists(p):
            continue
        md = pd.read_csv(p, dtype={"code": str}, usecols=["code", "volume"])
        md["code"] = md["code"].str.zfill(6)
        m = g.merge(md, left_on="ticker", right_on="code")
        v = pd.to_numeric(m["volume"], errors="coerce")
        bad = int(((pd.to_numeric(m["Volume"], errors="coerce") < v * VOL_RATIO) & (v > 0)).sum())
        rows.append((f"{d[:4]}-{d[4:6]}-{d[6:]}", len(m), bad))
    return spikes(rows, min_bad, excess)


def spikes(rows, min_bad=MIN_BAD, excess=EXCESS):
    """[(날짜, 비교 수, 의심 수)] 중 그 무렵 보통 수준(앞뒤 21일 중앙값)보다 excess 이상 튄 날만."""
    if not rows:
        return []
    base = pd.Series([r[2] for r in rows]).rolling(21, center=True, min_periods=1).median()
    return [r for r, b in zip(rows, base) if r[2] >= min_bad and r[2] >= b + excess]


def plan_updates(db_rows, fetched):
    """db_rows: DataFrame(date, ticker, PRICE_COLS…) / fetched: 같은 모양(다시 받은 값)
    → (바꿀 행 DataFrame[원래 값 + *_new 칸], 건너뛴 행 수(수정주가 의심)). 바꿀 행 = 값이 다르고 · 미완성(거래량 90% 미만)이고 · 종가 31% 이내."""
    if db_rows.empty or fetched.empty:
        return db_rows.iloc[0:0], 0
    a = db_rows.copy()
    b = fetched.drop(columns=["date"]).assign(_d=fetched["date"].astype(str).str[:10])
    a["_d"] = a["date"].astype(str).str[:10]          # 비교는 날짜 10자리로, 덮어쓸 때는 DB 의 원래 date 값 그대로
    m = a.merge(b, on=["ticker", "_d"], suffixes=("", "_new")).drop(columns=["_d"])
    cols = [c for c in PRICE_COLS if c in a.columns and c + "_new" in m.columns]

    def _diff(c):
        x = pd.to_numeric(m[c], errors="coerce")
        y = pd.to_numeric(m[c + "_new"], errors="coerce")
        return ~((x - y).abs() <= 1e-9) & y.notna()
    changed = pd.concat([_diff(c) for c in cols], axis=1).any(axis=1) if cols else pd.Series(False, index=m.index)
    c_old = pd.to_numeric(m["Close"], errors="coerce")
    c_new = pd.to_numeric(m["Close_new"], errors="coerce")
    weird = ((c_new / c_old - 1).abs() > MAX_CLOSE_DIFF) & c_old.gt(0)
    v_old = pd.to_numeric(m["Volume"], errors="coerce").fillna(0)
    v_new = pd.to_numeric(m["Volume_new"], errors="coerce")
    partial = v_old < v_new * VOL_RATIO                 # 장중에 저장된 일봉 = 거래량이 확정치보다 적음
    return m[changed & partial & ~weird].reset_index(drop=True), int((changed & partial & weird).sum())


def _fetch(tickers, d0, d1, log=print, sleep=0.0):
    """FDR 로 [d0, d1] 다시 받기 → DataFrame(date, ticker, PRICE_COLS)."""
    import FinanceDataReader as fdr
    frames, fail = [], 0
    for k, t in enumerate(tickers, 1):
        for attempt in range(3):
            try:
                df = fdr.DataReader(t, d0, d1)
                if df is not None and len(df):
                    df = df.reset_index().rename(columns={df.index.name or "index": "date", "Date": "date"})
                    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                    df["ticker"] = t
                    frames.append(df[["date", "ticker"] + [c for c in PRICE_COLS if c in df.columns]])
                break
            except Exception as e:
                if attempt == 2:
                    fail += 1
                    log(f"  다시 받기 실패 [{t}]: {e}")
                else:
                    time.sleep(2)
        if sleep:
            time.sleep(sleep)
        if k % 250 == 0:
            log(f"  진행 {k}/{len(tickers)}")
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "ticker"])
    return out, fail


def backup(rows, dates, now=None):
    """바꿀 행의 원래 값을 _archive 에 CSV 로(삭제 아님) + MANIFEST.csv 한 줄. → 백업 파일 경로."""
    now = now or datetime.now()
    day_dir = os.path.join(FIN, "_archive", now.strftime("%Y%m%d"))
    sub = os.path.join(day_dir, "korea_stocks_partial_bars")
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, f"rows_{now.strftime('%H%M%S')}.csv")
    rows.to_csv(path, index=False, encoding="utf-8-sig")
    man = os.path.join(day_dir, "MANIFEST.csv")
    new = not os.path.exists(man)
    with open(man, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["set", "original_relpath", "bytes", "moved_at"])
        w.writerow(["korea_stocks_partial_bars",
                    f"Stock_AI_Project/data/stock.db#korea_stocks({','.join(dates)} · {len(rows)}행 원래 값)",
                    os.path.getsize(path), now.strftime("%Y-%m-%d %H:%M:%S")])
    return path


def apply_updates(con, upd):
    cols = [c for c in PRICE_COLS if c + "_new" in upd.columns]
    sql = ("UPDATE korea_stocks SET " + ",".join(f'"{c}"=?' for c in cols) + " WHERE ticker=? AND date=?")
    vals = [tuple(None if pd.isna(r[c + "_new"]) else float(r[c + "_new"]) for c in cols) + (r["ticker"], r["date"])
            for _, r in upd.iterrows()]
    cur = con.executemany(sql, vals)
    con.commit()
    return cur.rowcount


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default="", help="고칠 날짜(쉼표, YYYY-MM-DD) — 비우면 KRX 거래량 비교로 자동 탐지")
    ap.add_argument("--since", default="2018-01-01", help="자동 탐지 시작일(기본 전체 — KRX 비교 파일이 있는 2018-06 부터)")
    ap.add_argument("--apply", action="store_true", help="백업 후 덮어쓰기(없으면 점검만)")
    a = ap.parse_args(argv)
    con = get_connection()
    try:
        if a.dates:
            dates = sorted({x.strip()[:10] for x in a.dates.split(",") if x.strip()})
            print(f"[repair] 지정 날짜: {', '.join(dates)}")
        else:
            sd = suspect_dates(con, a.since)
            for d, n, bad in sd:
                print(f"[repair] 의심 {d}: 코스닥 {n}종목 중 {bad}종목이 KRX 확정 거래량의 90% 미만")
            dates = [d for d, _, _ in sd]
        if not dates:
            print("[repair] 고칠 날짜 없음 — 끝")
            return 0
        q = ",".join("?" * len(dates))
        db_rows = pd.read_sql(f"SELECT date, ticker, {', '.join(chr(34) + c + chr(34) for c in PRICE_COLS)} "
                              f"FROM korea_stocks WHERE substr(date,1,10) IN ({q})", con, params=dates)
        tickers = sorted(db_rows["ticker"].unique())
        print(f"[repair] {len(tickers):,}종목 다시 받기({dates[0]} ~ {dates[-1]}) — 20~30분")
        fetched, fail = _fetch(tickers, dates[0], dates[-1])
        fetched = fetched[fetched["date"].isin(dates)]
        upd, weird = plan_updates(db_rows, fetched)
        by = upd.groupby("date").size().to_dict() if len(upd) else {}
        print(f"[repair] 바꿀 행 {len(upd):,} ({', '.join(f'{d} {n}' for d, n in sorted(by.items())) or '-'}) · "
              f"수정주가 의심 건너뜀 {weird} · 받기 실패 {fail}종목")
        if len(upd):
            ex = upd.head(5)
            for _, r in ex.iterrows():
                print(f"   예) {r['ticker']} {r['date']}: 종가 {r['Close']}→{r['Close_new']} · 거래량 {r['Volume']}→{r['Volume_new']}")
        if not a.apply:
            print("[repair] 점검만 했습니다 — 실제로 고치려면 --apply")
            return 0
        if not len(upd):
            return 0
        bpath = backup(upd[["date", "ticker"] + [c for c in PRICE_COLS if c in upd.columns]], dates)
        n = apply_updates(con, upd)
        print(f"[repair] 덮어씀 {n:,}행 · 원래 값 백업: {bpath}")
        print("[repair] 다음: 대시보드 데이터+로그 탭 [기술지표] 버튼(45~90분)으로 지표 다시 계산")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
