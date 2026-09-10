# -*- coding: utf-8 -*-
"""
backfill_investor_breakdown.py — 투자자 주체별 일별 순매수 수집 (pykrx, KRX 공개 데이터). (2026-09-09)

왜: 시스템의 '기관' 순매수는 금융투자(프로그램/차익 — 기계적)·투신(재량)·연기금(리밸런싱 — 역추세·기계적)·
    사모(메자닌·이벤트)·보험·은행의 **합계**라 정보가 서로 상쇄될 수 있다. 종목단위 외국인/기관 합계 IC≈0(2026-09-08)
    인데 프로그램매매만 IC +0.14 였던 것과 같은 논리 — 어느 주체의 흐름/크로스에 정보가 있는지 분해해 본다.
    기타법인은 자사주·관계사 매수 대리.

무엇: 거래일마다 주체 5종(연기금·투신·금융투자·사모·기타법인)의 종목별 순매수거래대금(원)·순매수거래량
      → macro_data/investor_{market}/YYYYMMDD.csv (utf-8-sig, 일봉 CSV 와 같은 규약). 거래일 목록은
      macro_data/daily(KOSDAQ) 의 기존 파일명에서 얻어 휴장 탐침 없음.

운영: KRX 웹(pykrx) 경로 — KIS/키움 키와 무관(장중 실행이 틱 웹소켓에 영향 없음). 호출 간 0.5~1.5초,
      방화벽 감지 시 15초 대기·3회 재시도, 원자적 저장, 이미 있는 날은 건너뜀(재개 가능).
      **시작 시 주체명 5종을 1일치로 사전 검증** — 하나라도 실패하면 즉시 중단(조용한 부분수집 방지).

사용: python backfill_investor_breakdown.py --market KOSDAQ --start 20210101 --end 20260908
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

import config  # noqa: F401  — .env(KRX_ID/PW) 로드가 pykrx import 보다 먼저여야 함 (backfill_kospi_daily 와 동일)
from pykrx import stock  # noqa: E402

INVESTORS = {"연기금": "pension", "투신": "trust", "금융투자": "fininv", "사모": "private", "기타법인": "corp"}
HERE = os.path.dirname(os.path.abspath(__file__))


def trading_days(start, end):
    d = os.path.join(HERE, "macro_data", "daily")
    days = sorted(f[:8] for f in os.listdir(d) if f.endswith(".csv") and f[:8].isdigit())
    return [x for x in days if start <= x <= end]


def fetch_investor(date_str, market, inv):
    """한 주체·하루치. 반환 DataFrame(code, {tag}_net, {tag}_qty) / 실패 None (방화벽은 재시도)."""
    tag = INVESTORS[inv]
    for attempt in range(3):
        try:
            time.sleep(random.uniform(0.5, 1.5))
            df = stock.get_market_net_purchases_of_equities_by_ticker(date_str, date_str, market, inv)
            if df is None or df.empty:
                return pd.DataFrame(columns=["code", f"{tag}_net", f"{tag}_qty"])
            df = df.reset_index()[["티커", "순매수거래대금", "순매수거래량"]]
            df.columns = ["code", f"{tag}_net", f"{tag}_qty"]
            return df
        except Exception as e:
            msg = str(e)
            if "Expecting value" in msg or "None of" in msg:
                print(f"  [{date_str} {inv}] KRX 방화벽 감지 — 15초 대기 ({attempt + 1}/3)", flush=True)
                time.sleep(15)
            else:
                print(f"  [{date_str} {inv}] 오류: {msg[:120]}", flush=True)
                return None
    return None


def fetch_day(date_str, market):
    out = None
    for inv in INVESTORS:
        part = fetch_investor(date_str, market, inv)
        if part is None:
            return None
        out = part if out is None else out.merge(part, on="code", how="outer")
    out["date"] = date_str
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="KOSDAQ")
    ap.add_argument("--start", default="20210101")
    ap.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    a = ap.parse_args()
    out_dir = os.path.join(HERE, "macro_data", f"investor_{a.market.lower()}")
    os.makedirs(out_dir, exist_ok=True)

    days = trading_days(a.start, a.end)
    todo = [d for d in days if not os.path.exists(os.path.join(out_dir, f"{d}.csv"))]
    print(f"[{a.market}] 거래일 {len(days):,} | 미수집 {len(todo):,} | 주체 {list(INVESTORS)} | 출력 {out_dir}", flush=True)
    if not todo:
        return

    # 사전 검증: 마지막 거래일 1일치로 주체명 5종 모두 유효한지 — 실패 시 즉시 중단(조용한 부분수집 방지)
    probe = fetch_day(days[-1], a.market)
    if probe is None or probe.empty:
        print("사전 검증 실패 — 주체명/시장/로그인 확인 필요. 중단.", flush=True)
        sys.exit(1)
    nz = {c: int((probe[c].fillna(0) != 0).sum()) for c in probe.columns if c.endswith("_net")}
    print(f"사전 검증 OK ({days[-1]}): 종목 {len(probe):,} | 주체별 비0 종목수 {nz}", flush=True)

    t0, ok, fail = time.time(), 0, 0
    for i, d in enumerate(todo, 1):
        df = fetch_day(d, a.market)
        if df is None:
            fail += 1
            print(f"  [{d}] 실패 — 건너뜀(재실행 시 재시도)", flush=True)
            if fail >= 10 and fail / i > 0.3:
                print("실패율 30% 초과 — KRX 차단 의심, 중단", flush=True)
                sys.exit(1)
            continue
        tmp = os.path.join(out_dir, f"{d}.csv.tmp")
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, os.path.join(out_dir, f"{d}.csv"))
        ok += 1
        if i % 20 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"  진행 {i}/{len(todo)} | 성공 {ok} 실패 {fail} | 경과 {el / 60:.0f}분 | ETA {el / i * (len(todo) - i) / 60:.0f}분", flush=True)
    print(f"완료: 성공 {ok} / 실패 {fail} (총 {(time.time() - t0) / 60:.0f}분)", flush=True)
    sys.exit(0 if fail == 0 else 2)


if __name__ == "__main__":
    main()
