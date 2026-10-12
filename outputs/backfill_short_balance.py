# -*- coding: utf-8 -*-
"""
backfill_short_balance.py — 코스닥 '공매도 잔고 비중' 과거치 수집(잔고 전용, 폴백 없음). (2026-10-12)

왜 따로 만드나
  · krx_collector.py 의 공매도 수집은 ① pykrx 기본값 **코스피**만 받고(코스닥 유니버스와 0종목 겹침)
    ② '잔고' 실패 시 '거래 비중'으로 **조용히 폴백**해 같은 '비중' 컬럼에 다른 지표를 섞는다
    (db/short 95일 중 90일이 폴백 — 06-02 부터 잔고 엔드포인트 실패).
  · 그래서 이 스크립트는 **코스닥 + 잔고 엔드포인트만** 쓴다. 실패하면 그날은 비워 둔다(섞지 않는다).
  · 결과 출력은 macro_data/shortbal_kosdaq/YYYYMMDD.csv — 현재 어떤 전략도 읽지 않는다.
    잔고 과거치가 실제로 받아지는지 확인된 뒤 별도 전략으로 붙일지 결정한다(§46).

사용(PC 에서 — KRX 웹은 이 분석 환경에서 접속 불가):
  python backfill_short_balance.py --probe             # 4개 날짜만 시험 → 받아지는지 판정 (1분)
  python backfill_short_balance.py --start 20220101     # (probe OK 일 때) 전체 수집, 재개 가능 (~1시간)
"""
import os
import sys
import time
import random
import argparse
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import config  # noqa: F401  — .env(KRX_ID/PW) 로드가 pykrx import 보다 먼저
from pykrx import stock  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "macro_data", "shortbal_kosdaq")
MARKET = "KOSDAQ"
_COLS = {"공매도잔고": "short_balance", "상장주식수": "listed_shares", "공매도금액": "short_amount",
         "시가총액": "mktcap", "비중": "short_bal_ratio"}


def trading_days(start, end):
    d = os.path.join(HERE, "macro_data", "daily")
    days = sorted(f[:8] for f in os.listdir(d) if f.endswith(".csv") and f[:8].isdigit())
    return [x for x in days if start <= x <= end]


def fetch_balance(date_str):
    """잔고 엔드포인트만 호출. 성공 → DataFrame(code, short_balance, …, short_bal_ratio, date), 실패 → (None, 사유)."""
    last = ""
    for attempt in range(3):
        try:
            time.sleep(random.uniform(0.6, 1.6))
            df = stock.get_shorting_balance_by_ticker(date_str, market=MARKET)
            if df is None or df.empty:
                return None, "빈 응답"
            df = df.reset_index()
            df = df.rename(columns={df.columns[0]: "code", **_COLS})
            if "short_bal_ratio" not in df.columns:
                return None, f"비중 컬럼 없음(컬럼={list(df.columns)[:6]})"
            df["code"] = df["code"].astype(str).str.zfill(6)
            df["date"] = date_str
            keep = ["code"] + [c for c in _COLS.values() if c in df.columns] + ["date"]
            return df[keep], ""
        except Exception as e:
            last = str(e).split("\n")[0][:120]
            if "Expecting value" in last or "None of" in last:
                time.sleep(15)            # KRX 방화벽/응답변경 — 잠시 대기 후 재시도
                continue
            return None, last
    return None, last or "재시도 초과"


def probe():
    days = trading_days("20220101", datetime.now().strftime("%Y%m%d"))
    if not days:
        print("[probe] macro_data/daily 거래일 목록이 비어 있음")
        return 2
    picks = []
    for want in ("20220103", "20240102", "20250602", days[-3]):
        c = [d for d in days if d >= want]
        if c and c[0] not in picks:
            picks.append(c[0])
    ok = 0
    for d in picks:
        df, why = fetch_balance(d)
        if df is None:
            print(f"[probe] {d}: 실패 — {why}")
            continue
        nz = int(pd.to_numeric(df["short_bal_ratio"], errors="coerce").fillna(0).ne(0).sum())
        print(f"[probe] {d}: OK {len(df):,}종목 · 비중≠0 {nz:,}종목 · 예시 {df.head(2).to_dict('records')}")
        ok += 1
    verdict = ("전 구간 수집 가능 — 다음: python backfill_short_balance.py --start 20220101" if ok == len(picks)
               else "일부 가능 — 성공한 시기만 수집됨(실패일은 비워 둠)" if ok
               else "수집 불가 — 잔고 과거치는 pykrx 로 받을 수 없음(거래 비중만 사용)")
    print(f"[probe] 결과 {ok}/{len(picks)} — {verdict}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="4개 날짜만 시험")
    ap.add_argument("--start", default="20220101")
    ap.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    a = ap.parse_args()
    if a.probe:
        sys.exit(probe())

    os.makedirs(OUT_DIR, exist_ok=True)
    days = trading_days(a.start, a.end)
    todo = [d for d in days if not os.path.exists(os.path.join(OUT_DIR, f"{d}.csv"))]
    print(f"[{MARKET} 잔고] 대상 {len(days):,}일 | 미수집 {len(todo):,} | 출력 {OUT_DIR}", flush=True)
    t0, ok, fail, streak = time.time(), 0, 0, 0
    for i, d in enumerate(todo, 1):
        df, why = fetch_balance(d)
        if df is None:
            fail += 1
            streak += 1
            print(f"  {d} 실패: {why}", flush=True)
            if streak >= 15:
                print("연속 15일 실패 — 중단(엔드포인트 불가 추정). 받은 날짜는 보존됨, 재실행 시 이어서.", flush=True)
                sys.exit(1)
            continue
        streak = 0
        tmp = os.path.join(OUT_DIR, f"{d}.csv.tmp")
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, os.path.join(OUT_DIR, f"{d}.csv"))
        ok += 1
        if i % 25 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"  진행 {i}/{len(todo)} | 성공 {ok} 실패 {fail} | 경과 {el / 60:.0f}분 "
                  f"| ETA {el / i * (len(todo) - i) / 60:.0f}분", flush=True)
    print(f"완료: 성공 {ok} / 실패 {fail} (총 {(time.time() - t0) / 60:.0f}분)", flush=True)


if __name__ == "__main__":
    main()
