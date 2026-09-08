# -*- coding: utf-8 -*-
"""
backfill_kospi_daily.py — KOSPI 일봉 + 수급(외국인/기관 순매수거래대금) 별도 수집. (2026-09-08)

목적
  수급선 역전 전략(SupplyCrossoverStrategy)을 'KOSPI 전용'으로 백테스트하기 위한 데이터.
  기존 macro_data/daily(KOSDAQ 1,822종목)와 완전히 분리해 daily_kospi/ 에 저장한다.

왜 별도 폴더인가 (중요)
  · pykrx_collector.collect_macro_data 는 파일명이 날짜뿐(YYYYMMDD.csv)이라 market 을 KOSPI 로
    바꿔도 같은 macro_data/daily 에 섞여 들어가 KOSDAQ 백테스트를 조용히 오염시킨다.
  · 그래서 여기서는 macro_data/daily_kospi/ 에만 쓴다. stock.db 에는 절대 쓰지 않는다
    (supply_demand.foreign_net_value 는 KIS 소스=다른 단위. 덮으면 단위 혼재로 파손).

단위 정합성 (KOSDAQ 결과와 사과 대 사과)
  · foreign_net/inst_net = pykrx get_market_net_purchases_of_equities_by_ticker 의 '순매수거래대금'(원).
    KOSDAQ CSV 와 동일 함수·동일 단위. 개인(individual_net)은 이미 supply_demand 에 같은 단위로 백필됨.
    → 세 수급선(개인/외국인/기관)이 전부 pykrx 원 단위 → 크로스오버 비교가 유효.

안전
  · KRX 로그인은 config(.env: KRX_ID/KRX_PW) 로 처리 — pykrx_collector 와 동일.
  · 스텔스 스로틀·재시도·휴장마커·원자적 쓰기(부분 CSV 방지)를 그대로 계승.
  · 멱등: 이미 있는 날짜/휴장마커는 건너뜀 → 중단 후 재실행하면 이어서 수집.
  · 장중 대량 REST 는 틱수집을 방해(2026-09-04 교훈) → 마감 후(>=15:40) 실행 권장.

사용
  python backfill_kospi_daily.py                 # 기본 2021-01-01 ~ 오늘
  python backfill_kospi_daily.py --start 20210101 --end 20260908
"""
import config  # noqa: F401  — .env(KRX_ID/PW) 로드가 pykrx import 보다 먼저여야 함

import os
import sys
import time
import random
import argparse
from datetime import datetime

import pandas as pd
from pykrx import stock

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "macro_data", "daily_kospi")
os.makedirs(DATA_DIR, exist_ok=True)

MARKET = "KOSPI"


def _fetch_one_day(date_str):
    """하루치 KOSPI OHLCV + 외국인/기관 순매수 병합. 성공 시 DataFrame, 휴장이면 'HOLIDAY', 실패 None."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(1.5, 3.5))
            df_ohlcv = stock.get_market_ohlcv(date_str, MARKET)

            if df_ohlcv is None or df_ohlcv.empty or (df_ohlcv["종가"] == 0).all():
                # 빈/전종목0 = 진짜 휴장일 수도, KRX 스로틀 오응답일 수도. 마지막 시도에서만 휴장 확정.
                if attempt < max_retries - 1:
                    print(f"  [{date_str}] 빈 응답 — 휴장 유보, 재시도 {attempt + 2}/{max_retries}")
                    time.sleep(10)
                    continue
                return "HOLIDAY"

            df_ohlcv = df_ohlcv.reset_index()
            df_ohlcv.rename(columns={
                "티커": "code", "시가": "open", "고가": "high",
                "저가": "low", "종가": "close", "거래량": "volume",
                "거래대금": "trading_value", "등락률": "change_pct",
            }, inplace=True)

            time.sleep(random.uniform(0.5, 1.5))
            df_for = stock.get_market_net_purchases_of_equities_by_ticker(
                date_str, date_str, MARKET, "외국인")
            if df_for is not None and not df_for.empty:
                df_for = df_for.reset_index()[["티커", "순매수거래대금"]]
                df_for.rename(columns={"티커": "code", "순매수거래대금": "foreign_net"}, inplace=True)
            else:
                df_for = pd.DataFrame(columns=["code", "foreign_net"])

            time.sleep(random.uniform(0.5, 1.5))
            df_ins = stock.get_market_net_purchases_of_equities_by_ticker(
                date_str, date_str, MARKET, "기관합계")
            if df_ins is not None and not df_ins.empty:
                df_ins = df_ins.reset_index()[["티커", "순매수거래대금"]]
                df_ins.rename(columns={"티커": "code", "순매수거래대금": "inst_net"}, inplace=True)
            else:
                df_ins = pd.DataFrame(columns=["code", "inst_net"])

            df = pd.merge(df_ohlcv, df_for, on="code", how="left")
            df = pd.merge(df, df_ins, on="code", how="left")
            df["date"] = date_str
            return df

        except Exception as e:
            msg = str(e)
            if "Expecting value" in msg or "None of" in msg:
                print(f"  [{date_str}] KRX 방화벽 감지 — 15초 대기 ({attempt + 1}/{max_retries})")
                time.sleep(15)
            else:
                print(f"  [{date_str}] 오류: {msg[:120]}")
                return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20210101")
    ap.add_argument("--end", default=datetime.today().strftime("%Y%m%d"))
    a = ap.parse_args()

    today_str = datetime.today().strftime("%Y%m%d")
    now_hm = datetime.now().strftime("%H%M")

    date_range = pd.bdate_range(start=a.start, end=a.end)
    total = len(date_range)
    print(f"[KOSPI] {a.start} ~ {a.end}  영업일 {total}일  → {DATA_DIR}")

    t0 = time.time()
    n_new = n_skip = n_hol = n_fail = 0
    for i, dt in enumerate(date_range):
        date_str = dt.strftime("%Y%m%d")
        save_path = os.path.join(DATA_DIR, f"{date_str}.csv")

        if os.path.exists(save_path) or os.path.exists(save_path + ".holiday"):
            n_skip += 1
            continue

        # 오늘·미래 + 장마감 전이면 보류 (당일 데이터는 15:40 이후에만 확정)
        if date_str >= today_str and now_hm < "1540":
            print(f"  [{date_str}] 장마감 전 — 수집 보류")
            continue

        res = _fetch_one_day(date_str)
        if res is None:
            n_fail += 1
        elif isinstance(res, str) and res == "HOLIDAY":
            if date_str < today_str:
                open(save_path + ".holiday", "w").close()
            n_hol += 1
        else:
            # 원자적 쓰기 — 쓰다 죽은 부분 CSV 가 '존재=완료' 스킵에 걸려 잔존하는 것 방지
            tmp = save_path + ".tmp"
            res.to_csv(tmp, index=False, encoding="utf-8-sig")
            os.replace(tmp, save_path)
            n_new += 1

        if (i + 1) % 10 == 0 or i + 1 == total:
            el = time.time() - t0
            done = n_new + n_hol + n_fail
            rate = el / max(done, 1)
            eta = rate * (total - (i + 1)) / 60.0
            print(f"  {i + 1:4d}/{total}  신규 {n_new} | 스킵 {n_skip} | 휴장 {n_hol} | 실패 {n_fail} "
                  f"| 경과 {el / 60:.1f}분 | ETA {eta:.0f}분", flush=True)

    print(f"\n[완료] 신규 {n_new} | 스킵 {n_skip} | 휴장 {n_hol} | 실패 {n_fail}  "
          f"(총 {(time.time() - t0) / 60:.1f}분)")
    if n_fail:
        print(f"[주의] 실패 {n_fail}일 — 재실행하면 실패한 날짜만 다시 시도합니다(멱등).")


if __name__ == "__main__":
    main()
