# -*- coding: utf-8 -*-
"""
backfill_shorting_fundamental.py — 공매도 비중 + 재무지표(PER/PBR/배당) 수집 (pykrx). (2026-09-13)

왜: korea_indicators 에 `short_ratio` 컬럼이 있으나 **값이 전부 0**(고유값 1개)이다 — 컬럼만 만들어두고
    한 번도 채우지 않았다. 그리고 재무지표(밸류 팩터)는 시스템에 아예 없다.
    2026-09-13 미사용 컬럼 전수 스캔 결과, 이미 저장된 컬럼 중 새로 쓸 만한 것은 없었다
    (return_*·ma*_ratio·macd 계열은 bb_pct_db/rsi_db/macd_hist_db 와 |r| 0.67~0.87 로 중복).
    → 남은 선택지는 '아직 없는 데이터'이고, 그중 pykrx 로 즉시 받을 수 있는 두 가지가 이것이다.

수집 내용 (거래일별 1행/종목)
  · 공매도: `get_shorting_volume_by_ticker` → 공매도 거래량, 매수 거래량, **비중(%)**
  · 재무:   `get_market_fundamental`        → BPS, PER, PBR, EPS, DIV, DPS
  → macro_data/shortfund_{market}/YYYYMMDD.csv (utf-8-sig, 기존 일봉 CSV 규약과 동일)

운영: KRX 웹(pykrx) 경로라 KIS/키움 키와 무관(장중에도 안전). 호출 간 0.5~1.5초, 방화벽 감지 시 15초
      대기·3회 재시도, 원자적 저장, 이미 있는 날은 건너뜀(재개 가능). 거래일 목록은 macro_data/daily 파일명 기준.
      ⚠ 한국은 공매도 금지 기간이 있었다(2020-03~2021-05 등) — 그 구간은 값이 0/결측이 정상이다.

사용: python backfill_shorting_fundamental.py --market KOSDAQ --days 250     # 최근 250거래일(파일럿)
      python backfill_shorting_fundamental.py --market KOSDAQ --start 20210101
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


def trading_days(start, end):
    d = os.path.join(HERE, "macro_data", "daily")
    days = sorted(f[:8] for f in os.listdir(d) if f.endswith(".csv") and f[:8].isdigit())
    return [x for x in days if start <= x <= end]


def _retry(fn, label, date_str):
    for attempt in range(3):
        try:
            time.sleep(random.uniform(0.5, 1.5))
            return fn()
        except Exception as e:
            msg = str(e)
            if "Expecting value" in msg or "None of" in msg:
                print(f"  [{date_str} {label}] KRX 방화벽/응답변경 — 15초 대기 ({attempt + 1}/3)", flush=True)
                time.sleep(15)
            else:
                print(f"  [{date_str} {label}] 오류: {msg[:110]}", flush=True)
                return None
    return None


def fetch_day(date_str, market):
    sh = _retry(lambda: stock.get_shorting_volume_by_ticker(date_str, market), "공매도", date_str)
    fu = _retry(lambda: stock.get_market_fundamental(date_str, market=market), "재무", date_str)
    if sh is None and fu is None:
        return None
    frames = []
    if sh is not None and not sh.empty:
        s = sh.reset_index()
        s.columns = ["code"] + [{"공매도": "short_vol", "매수": "buy_vol", "비중": "short_ratio"}.get(c, c)
                                for c in sh.columns]
        frames.append(s[["code"] + [c for c in ("short_vol", "buy_vol", "short_ratio") if c in s.columns]])
    if fu is not None and not fu.empty:
        f = fu.reset_index()
        f = f.rename(columns={f.columns[0]: "code"})
        keep = ["code"] + [c for c in ("BPS", "PER", "PBR", "EPS", "DIV", "DPS") if c in f.columns]
        frames.append(f[keep])
    if not frames:
        return None
    out = frames[0]
    for g in frames[1:]:
        out = out.merge(g, on="code", how="outer")
    out["date"] = date_str
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="KOSDAQ")
    ap.add_argument("--start", default="20210101")
    ap.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--days", type=int, default=None, help="최근 N 거래일만(파일럿)")
    a = ap.parse_args()
    out_dir = os.path.join(HERE, "macro_data", f"shortfund_{a.market.lower()}")
    os.makedirs(out_dir, exist_ok=True)

    days = trading_days(a.start, a.end)
    if a.days:
        days = days[-a.days:]
    todo = [d for d in days if not os.path.exists(os.path.join(out_dir, f"{d}.csv"))]
    print(f"[{a.market}] 대상 거래일 {len(days):,} | 미수집 {len(todo):,} | 출력 {out_dir}", flush=True)
    if not todo:
        return

    probe = fetch_day(days[-1], a.market)
    if probe is None or probe.empty:
        print("사전 검증 실패 — 중단", flush=True)
        sys.exit(1)
    nz = {c: int(pd.to_numeric(probe[c], errors="coerce").fillna(0).ne(0).sum())
          for c in probe.columns if c not in ("code", "date")}
    print(f"사전 검증 OK ({days[-1]}): 종목 {len(probe):,} | 컬럼별 비0 종목수 {nz}", flush=True)

    t0, ok, fail = time.time(), 0, 0
    for i, d in enumerate(todo, 1):
        df = fetch_day(d, a.market)
        if df is None:
            fail += 1
            if fail >= 10 and fail / i > 0.3:
                print("실패율 30% 초과 — 중단", flush=True)
                sys.exit(1)
            continue
        tmp = os.path.join(out_dir, f"{d}.csv.tmp")
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, os.path.join(out_dir, f"{d}.csv"))
        ok += 1
        if i % 25 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"  진행 {i}/{len(todo)} | 성공 {ok} 실패 {fail} | 경과 {el / 60:.0f}분 "
                  f"| ETA {el / i * (len(todo) - i) / 60:.0f}분", flush=True)
    print(f"완료: 성공 {ok} / 실패 {fail} (총 {(time.time() - t0) / 60:.0f}분)", flush=True)


if __name__ == "__main__":
    main()
