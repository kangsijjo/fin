# -*- coding: utf-8 -*-
"""
export_db.py — stock.db 의 아무 테이블이나 **기간을 지정해 CSV 로** 뽑는다. (2026-09-14)

이 파일과 `export_for_calculator.py` 의 차이
  · export_db.py            : **범용**. 테이블 하나(또는 여럿)를 있는 그대로 뽑는다. 컬럼·종목·기간 지정.
  · export_for_calculator.py: **검산기 전용**. 가격+수급을 조인하고 단위를 백만원으로 맞춰 한 파일로 만든다.

날짜 형식이 테이블마다 다르다(`2015-01-02` / `20150102`). 이 스크립트는 날짜 컬럼을 자동으로 찾아
**형식과 무관하게** 비교하므로 항상 `YYYYMMDD` 로 기간을 주면 된다.

사용 예
  python export_db.py --list                                   # 테이블 목록과 기간 보기
  python export_db.py --table korea_stocks --start 20240101    # 한 테이블
  python export_db.py --table supply_demand --start 20240101 --end 20260911
  python export_db.py --table program_trading --codes 005930,000660
  python export_db.py --table korea_indicators --cols date,ticker,rsi,macd_hist --start 20250101
  python export_db.py --tables korea_stocks,supply_demand --start 20250101   # 여러 개를 각각 파일로
  python export_db.py --all --start 20260101                   # 주요 테이블 일괄

출력: outputs/db_export/<테이블>_<시작>_<끝>.csv (utf-8-sig — 엑셀에서 한글 안 깨짐)
"""
import os
import sys
import sqlite3
import argparse
from datetime import datetime

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --all 로 뽑을 주요 테이블(운영 부산물·초소형 테이블 제외)
MAIN = ["korea_stocks", "supply_demand", "korea_indicators", "foreign_ratio",
        "credit_balance", "program_trading", "stock_lending", "macro_indicators"]
DATE_CANDS = ("date", "pubDate", "dt", "일자")


def find_db():
    cands = [os.getenv("STOCK_DB", "")]
    try:
        from fin_paths import STOCK_DB
        cands.append(str(STOCK_DB))
    except Exception:
        pass
    cands.append(os.path.join(os.path.dirname(HERE), "Stock_AI_Project", "data", "stock.db"))
    p = next((x for x in cands if x and os.path.exists(x)), None)
    if not p:
        sys.exit("stock.db 를 찾지 못했습니다 — STOCK_DB 환경변수로 경로를 지정하세요.")
    return p


def open_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_info(con, t):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info([{t}])")]
    dc = next((c for c in cols if c.lower() in [d.lower() for d in DATE_CANDS]), None)
    return cols, dc


def list_tables(con):
    ts = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print(f"{'테이블':26s}{'행수':>12s}  {'기간':24s} 날짜컬럼")
    print("-" * 78)
    for t in ts:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            cols, dc = table_info(con, t)
            rng = ""
            if dc and n:
                a, b = con.execute(f"SELECT MIN([{dc}]), MAX([{dc}]) FROM [{t}]").fetchone()
                rng = f"{str(a)[:10]} ~ {str(b)[:10]}"
            print(f"{t:26s}{n:12,}  {rng:24s} {dc or '-'}")
        except Exception as e:
            print(f"{t:26s}{'?':>12s}  {str(e)[:40]}")
    print("\n컬럼을 보려면: --table <이름> --peek")


def export(con, t, a):
    cols, dc = table_info(con, t)
    if not cols:
        print(f"  [{t}] 테이블이 없습니다 — --list 로 이름을 확인하세요.")
        return None
    if a.peek:
        print(f"\n[{t}] 컬럼 {len(cols)}개\n  {cols}")
        d = pd.read_sql(f"SELECT * FROM [{t}] LIMIT 3", con)
        print(d.to_string()[:1200])
        return None

    sel = "*"
    if a.cols:
        want = [c.strip() for c in a.cols.split(",") if c.strip()]
        miss = [c for c in want if c not in cols]
        if miss:
            print(f"  [{t}] 없는 컬럼: {miss} — 건너뜁니다. (가능: {cols})")
            return None
        sel = ", ".join(f"[{c}]" for c in want)

    where, params = [], []
    if dc and (a.start or a.end):
        # 날짜 형식(대시 유무)에 무관하게 비교
        if a.start:
            where.append(f"replace([{dc}],'-','') >= ?")
            params.append(a.start)
        if a.end:
            where.append(f"replace([{dc}],'-','') <= ?")
            params.append(a.end)
    elif (a.start or a.end) and not dc:
        print(f"  [{t}] 날짜 컬럼이 없어 기간 조건을 무시합니다.")

    if a.codes:
        codecol = next((c for c in cols if c.lower() in ("ticker", "code", "종목코드")), None)
        if codecol:
            cl = [c.strip().zfill(6) for c in a.codes.split(",") if c.strip()]
            where.append(f"[{codecol}] IN ({','.join('?' * len(cl))})")
            params += cl
        else:
            print(f"  [{t}] 종목 컬럼이 없어 --codes 를 무시합니다.")

    sql = f"SELECT {sel} FROM [{t}]" + (" WHERE " + " AND ".join(where) if where else "")
    if dc:
        sql += f" ORDER BY [{dc}]"
    if a.limit:
        sql += f" LIMIT {int(a.limit)}"

    print(f"  [{t}] 조회 중…", flush=True)
    df = pd.read_sql(sql, con, params=params)
    if df.empty:
        print(f"  [{t}] 조건에 맞는 행이 없습니다.")
        return None

    os.makedirs(a.outdir, exist_ok=True)
    tag = f"_{a.start or 'all'}_{a.end or 'all'}"
    path = os.path.join(a.outdir, f"{t}{tag}.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    mb = os.path.getsize(path) / 1e6
    rng = ""
    if dc:
        rng = f" | {str(df[dc].min())[:10]} ~ {str(df[dc].max())[:10]}"
    print(f"  [{t}] {len(df):,}행 · {mb:.1f} MB{rng}\n        → {path}")
    return path


# ── 통합 추출: 종목-일 한 행에 모든 테이블을 옆으로 붙인다 ──────────────────
# 기준은 korea_stocks(전 종목 가격). 나머지는 (종목,날짜)로 LEFT JOIN 하므로
# 자료가 없는 구간은 빈칸으로 남는다(프로그램매매 2022~, 대차 2023~ 처럼 시작일이 다름).
JOINS = [
    ("supply_demand", "ticker", "date",
     {"foreign_net_value": "foreign_net", "institution_net_value": "inst_net",
      "individual_net_value": "individual_net", "short_balance_ratio": "short_bal_rt"}),
    ("korea_indicators", "ticker", "date",
     {"rsi": "rsi", "macd": "macd", "macd_signal": "macd_signal", "macd_hist": "macd_hist",
      "bb_upper": "bb_upper", "bb_mid": "bb_mid", "bb_lower": "bb_lower",
      "vol_ma20": "vol_ma20", "vol_spike": "vol_spike", "short_ratio": "short_ratio",
      "credit_ratio": "credit_ratio", "lending_chg_5d": "lending_chg_5d"}),
    ("foreign_ratio", "ticker", "date",
     {"holding_ratio": "for_hold_ratio", "held_shares": "for_held_shares", "listed_shares": "listed_shares"}),
    ("credit_balance", "ticker", "date",
     {"credit_remain": "credit_remain", "credit_remain_rt": "credit_remain_rt"}),
    ("program_trading", "ticker", "date",
     {"prm_net_amt": "prm_net_amt", "prm_buy_amt": "prm_buy_amt", "prm_sell_amt": "prm_sell_amt",
      "prm_net_qty": "prm_net_qty"}),
    ("stock_lending", "ticker", "date",
     {"lending_balance": "lending_balance", "lending_amount": "lending_amount"}),
]


def export_join(con, a):
    """종목-일 기준으로 모든 테이블을 한 파일에 합친다."""
    start, end = a.start or "20000101", a.end
    print(f"[통합] 기준 테이블 korea_stocks — {start} ~ {end}")
    base = pd.read_sql(
        "SELECT replace(date,'-','') AS date, ticker AS code, Open AS open, High AS high, "
        "Low AS low, Close AS close, Volume AS volume, Change AS change_pct, "
        "ma5, ma20, ma60 "
        "FROM korea_stocks WHERE replace(date,'-','') BETWEEN ? AND ? AND Close > 0",
        con, params=[start, end])
    if base.empty:
        sys.exit("해당 기간에 가격 자료가 없습니다.")
    base["code"] = base["code"].astype(str).str.zfill(6)
    # 거래대금은 이 DB 에 없다 — 거래량×종가로 근사(실측이 필요하면 export_for_calculator.py 사용)
    base["trading_value_est"] = (base["volume"] * base["close"] / 1e6).round(0)
    print(f"  가격 {len(base):,}행 · 종목 {base['code'].nunique():,}")
    if a.min_tv:
        before = len(base)
        base = base[base["trading_value_est"] >= a.min_tv]
        print(f"  거래대금(근사) {a.min_tv:,}백만 이상만 → {len(base):,}행 (원본의 {len(base)/max(1,before)*100:.0f}%)")
    if a.codes:
        cl = [c.strip().zfill(6) for c in a.codes.split(",") if c.strip()]
        base = base[base["code"].isin(cl)]
        print(f"  종목 지정 {len(cl)}개 → {len(base):,}행")
    if base.empty:
        sys.exit("필터 후 남은 행이 없습니다 — --min-tv 를 낮춰보세요.")

    for tbl, kcol, dcol, colmap in JOINS:
        try:
            sel = ", ".join(f"[{s}] AS [{d}]" for s, d in colmap.items())
            df = pd.read_sql(
                f"SELECT replace([{dcol}],'-','') AS date, [{kcol}] AS code, {sel} "
                f"FROM [{tbl}] WHERE replace([{dcol}],'-','') BETWEEN ? AND ?",
                con, params=[start, end])
        except Exception as e:
            print(f"  [{tbl}] 건너뜀: {str(e)[:70]}")
            continue
        if df.empty:
            print(f"  [{tbl}] 해당 기간 자료 없음")
            continue
        df["code"] = df["code"].astype(str).str.zfill(6)
        df = df.drop_duplicates(["code", "date"])
        base = base.merge(df, on=["code", "date"], how="left")
        hit = base[list(colmap.values())[0]].notna().mean() * 100
        print(f"  [{tbl}] {len(df):,}행 조인 · 채움률 {hit:.1f}%")

    # 거시지표는 일자 단위 — 피벗해서 모든 종목 행에 같은 값으로 붙인다
    try:
        mi = pd.read_sql(
            "SELECT replace(date,'-','') AS date, indicator, close FROM macro_indicators "
            "WHERE replace(date,'-','') BETWEEN ? AND ?", con, params=[start, end])
        if not mi.empty:
            piv = mi.drop_duplicates(["date", "indicator"]).pivot(index="date", columns="indicator", values="close")
            piv.columns = ["mkt_" + str(c).lower() for c in piv.columns]
            base = base.merge(piv.reset_index(), on="date", how="left")
            print(f"  [macro_indicators] {piv.shape[1]}개 지표 브로드캐스트 ({', '.join(piv.columns[:6])})")
    except Exception as e:
        print(f"  [macro_indicators] 건너뜀: {str(e)[:70]}")

    base = base.sort_values(["code", "date"])
    os.makedirs(a.outdir, exist_ok=True)
    path = os.path.join(a.outdir, f"all_{start}_{end}.csv")
    base.to_csv(path, index=False, encoding="utf-8-sig")
    mb = os.path.getsize(path) / 1e6
    print(f"\n통합 완료 → {path}")
    print(f"  {len(base):,}행 × {len(base.columns)}컬럼 · 종목 {base['code'].nunique():,} · "
          f"거래일 {base['date'].nunique():,} · {mb:.1f} MB")
    if mb > 200:
        print("  ⚠ 파일이 매우 큽니다. 기간을 줄이거나 --min-tv 를 올리세요.")
    print(f"\n  컬럼: {list(base.columns)}")
    return path


def main():
    ap = argparse.ArgumentParser(description="stock.db → CSV 추출 (기간 지정)")
    ap.add_argument("--join", action="store_true",
                    help="종목-일 한 행에 모든 테이블을 합쳐 **하나의 CSV** 로 (가격+수급+지표+지분율+신용+프로그램+대차+거시)")
    ap.add_argument("--min-tv", type=float, default=None,
                    help="--join 전용. 거래대금(거래량×종가, 백만원) 하한. 크기를 줄이는 가장 효과적인 수단")
    ap.add_argument("--list", action="store_true", help="테이블 목록과 기간 보기")
    ap.add_argument("--table", help="뽑을 테이블 하나")
    ap.add_argument("--tables", help="여러 테이블(쉼표)")
    ap.add_argument("--all", action="store_true", help=f"주요 테이블 일괄: {', '.join(MAIN)}")
    ap.add_argument("--start", help="시작일 YYYYMMDD")
    ap.add_argument("--end", default=datetime.now().strftime("%Y%m%d"), help="종료일 YYYYMMDD")
    ap.add_argument("--cols", help="가져올 컬럼(쉼표). 생략하면 전부")
    ap.add_argument("--codes", help="종목코드(쉼표). 예: 005930,000660")
    ap.add_argument("--limit", type=int, help="행 수 상한(맛보기용)")
    ap.add_argument("--peek", action="store_true", help="컬럼과 표본 3행만 보기")
    ap.add_argument("--outdir", default=os.path.join(HERE, "db_export"))
    a = ap.parse_args()

    path = find_db()
    print(f"DB: {path}  ({os.path.getsize(path)/1e9:.1f} GB)\n")
    con = open_ro(path)
    try:
        if a.join:
            export_join(con, a)
            return
        if a.list or not (a.table or a.tables or a.all):
            list_tables(con)
            if not (a.table or a.tables or a.all):
                print("\n예) python export_db.py --table korea_stocks --start 20240101")
                print("    python export_db.py --join --start 20240101 --min-tv 1000   ← 전부 한 파일로")
            return
        targets = ([a.table] if a.table else []) + \
                  ([t.strip() for t in a.tables.split(",")] if a.tables else []) + \
                  (MAIN if a.all else [])
        seen, made = set(), []
        for t in targets:
            if t in seen:
                continue
            seen.add(t)
            p = export(con, t, a)
            if p:
                made.append(p)
        if made:
            tot = sum(os.path.getsize(p) for p in made) / 1e6
            print(f"\n완료: 파일 {len(made)}개 · 합계 {tot:.1f} MB · 폴더 {a.outdir}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
