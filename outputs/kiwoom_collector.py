"""
키움 수급 데이터 수집기 — 종목별 신용매매동향 + 프로그램매매 (조회 전용, 주문 없음).

KIS 로 확보 불가하던 두 데이터를 키움 REST 로 수집:
  - ka10013 신용매매동향 (종목별 신용 융자 추이)  → db/credit/YYYY-MM/YYYYMMDD.csv
  - ka90013 종목일별 프로그램매매                → db/program/YYYY-MM/YYYYMMDD.csv

대상 종목: credit_collector.target_codes() (최근 신호 + 랭킹 상위) — 호출량 절약.

키 선택: 조회 전용이므로 실전(KIWOOM_PROD_*) 키가 있으면 우선 사용
  (모의서버는 일부 시세 API 미지원/호출 제한이 낮음). 주문 코드는 이 파일에 없음.

실행: python kiwoom_collector.py          # 둘 다 수집 (멱등)
      python kiwoom_collector.py credit   # 신용만
      python kiwoom_collector.py program  # 프로그램만
매일 20:00 recollect_guard 가 자동 실행.
"""

import os
import sys
import time
from datetime import datetime

import pandas as pd

import config

CREDIT_DIR = "./db/credit"
PROGRAM_DIR = "./db/program"
TODAY = datetime.today().strftime("%Y%m%d")
MONTH = datetime.today().strftime("%Y-%m")


def _clients():
    """조회용 StockInfo/MarketCondition — 실전 키 우선 (조회 전용)."""
    key = os.getenv("KIWOOM_PROD_APP_KEY", "").strip() or config.KIWOOM_APP_KEY
    sec = os.getenv("KIWOOM_PROD_APP_SECRET", "").strip() or config.KIWOOM_APP_SECRET
    use_prod = bool(os.getenv("KIWOOM_PROD_APP_KEY", "").strip())
    if not key or not sec:
        print("[ERROR] 키움 앱키 없음 (.env KIWOOM_*_APP_KEY)")
        sys.exit(1)
    os.environ["KIWOOM_API_KEY"] = key
    os.environ["KIWOOM_API_SECRET"] = sec
    os.environ["KIWOOM_USE_SANDBOX"] = "false" if use_prod else "true"
    try:
        from kiwoom_rest_api.config import get_base_url
        from kiwoom_rest_api.auth.token import TokenManager
        from kiwoom_rest_api.koreanstock.stockinfo import StockInfo
        from kiwoom_rest_api.koreanstock.market_condition import MarketCondition
    except ImportError:
        print("[ERROR] kiwoom-rest-api 미설치 → pip install kiwoom-rest-api")
        sys.exit(1)
    base = get_base_url()
    tm = TokenManager()
    tm.get_token()
    print(f"[kiwoom] 토큰 OK ({'실전(조회전용)' if use_prod else '모의'} {base})")
    return StockInfo(base_url=base, token_manager=tm), \
           MarketCondition(base_url=base, token_manager=tm)


def _targets():
    from credit_collector import target_codes
    return target_codes()


def _save(rows, out_dir, label):
    if not rows:
        print(f"[{label}] 수집 0건 — 실패")
        return False
    os.makedirs(f"{out_dir}/{MONTH}", exist_ok=True)
    path = f"{out_dir}/{MONTH}/{TODAY}.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[{label}] {len(rows)}종목 저장: {path}")
    return True


def collect_credit(si, codes):
    """ka10013 — 종목별 신용 융자 동향 (최신일 1행씩)."""
    path = f"{CREDIT_DIR}/{MONTH}/{TODAY}.csv"
    if os.path.exists(path):
        print(f"[credit] {path} 이미 존재 (멱등)")
        return True
    rows, fails = [], 0
    for code in codes:
        try:
            r = si.credit_trading_trend_request_ka10013(
                stock_code=code, date=TODAY, query_type="1")  # 1=융자
            lst = r.get("crd_trde_trend") or []
            if lst:
                row = dict(lst[0])
                row["code"] = code
                rows.append(row)
        except Exception as e:
            fails += 1
            if fails <= 3:
                print(f"  [warn] credit {code}: {str(e)[:120]}")
        time.sleep(0.12)
    if fails > 3:
        print(f"  [warn] credit 실패 총 {fails}건")
    return _save(rows, CREDIT_DIR, "credit")


def collect_program(mc, codes):
    """ka90013 — 종목일별 프로그램매매 (최신일 1행씩)."""
    path = f"{PROGRAM_DIR}/{MONTH}/{TODAY}.csv"
    if os.path.exists(path):
        print(f"[program] {path} 이미 존재 (멱등)")
        return True
    rows, fails = [], 0
    list_keys = None
    for code in codes:
        try:
            r = mc.stockwise_program_trading_by_day_request_ka90013(
                stock_code=code, amount_quantity_type="1", date=TODAY)
            if list_keys is None and isinstance(r, dict):
                list_keys = [k for k, v in r.items() if isinstance(v, list)]
            lst = None
            for k in (list_keys or []):
                if r.get(k):
                    lst = r[k]
                    break
            if lst:
                row = dict(lst[0])
                row["code"] = code
                rows.append(row)
        except Exception as e:
            fails += 1
            if fails <= 3:
                print(f"  [warn] program {code}: {str(e)[:120]}")
        time.sleep(0.12)
    if fails > 3:
        print(f"  [warn] program 실패 총 {fails}건")
    return _save(rows, PROGRAM_DIR, "program")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    codes = _targets()
    if not codes:
        print("[kiwoom_collector] 대상 종목 없음")
        return
    print(f"[kiwoom_collector] 대상 {len(codes)}종목 ({cmd})")
    si, mc = _clients()
    ok = True
    if cmd in ("all", "credit"):
        ok = collect_credit(si, codes) and ok
    if cmd in ("all", "program"):
        ok = collect_program(mc, codes) and ok
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
