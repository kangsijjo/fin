"""
DART (전자공시시스템) → 일자별 공시 목록 수집.

API 키 발급 필수:
  1. https://opendart.fss.or.kr/ 가입
  2. "오픈API 신청" → 인증키 받기
  3. .env 에 추가: DART_API_KEY=발급받은_키

호출 시점: 매일 평일 19:00 권장 (당일 공시 마감 후, KIS_DART 작업).

사용법:
  python dart_collector.py today                      # 오늘 공시
  python dart_collector.py date 20260604              # 특정 날짜
  python dart_collector.py corp 005930                # 특정 종목 최근 공시
  python dart_collector.py date 20260604 --force      # 특정 날짜 강제 재수집
  python dart_collector.py today --force              # 오늘 강제 재수집

저장: db/dart/YYYY-MM/YYYYMMDD.csv
스키마: code, corp_code, corp_name, report_nm, rcept_dt, rcept_no, flr_nm
"""

import sys
import os
import csv
from datetime import datetime

import requests

import config


DART_BASE = "https://opendart.fss.or.kr/api"
DART_API_KEY = os.getenv("DART_API_KEY", "").strip()


def _month_dir(base_dir, date_str):
    yyyy_mm = f"{date_str[:4]}-{date_str[4:6]}"
    path = f"{base_dir}/{yyyy_mm}"
    os.makedirs(path, exist_ok=True)
    return path


def _check_api_key():
    if not DART_API_KEY:
        print("[ERROR] DART_API_KEY 환경변수가 없습니다.")
        print("        .env 파일에 DART_API_KEY=발급받은_키 추가 후 재시도.")
        sys.exit(1)


def fetch_disclosures(begin_date, end_date=None, corp_code=None,
                      page_count=100, max_pages=10):
    """DART 공시검색 API 호출. 페이징 처리하여 모든 공시 반환."""
    _check_api_key()
    if end_date is None:
        end_date = begin_date

    results = []
    for page in range(1, max_pages + 1):
        params = {
            "crtfc_key": DART_API_KEY,
            "bgn_de": begin_date,
            "end_de": end_date,
            "page_no": page,
            "page_count": page_count,
        }
        if corp_code:
            params["corp_code"] = corp_code

        try:
            r = requests.get(f"{DART_BASE}/list.json", params=params, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"  [error] DART API 호출 실패 page={page}: {e}")
            break

        status = data.get("status", "")
        if status == "013":   # 조회된 데이터 없음
            break
        if status != "000":
            print(f"  [warn] DART status={status} msg={data.get('message')}")
            break

        items = data.get("list", [])
        results.extend(items)
        total_page = data.get("total_page", 1)
        if page >= total_page:
            break

    return results


def _force_delete_dart(date_str):
    """특정 날짜 공시 파일 삭제 → 재수집 가능 상태로 초기화."""
    out_dir = _month_dir(config.DB_DART_DIR, date_str)
    out_path = f"{out_dir}/{date_str}.csv"
    if os.path.exists(out_path):
        os.remove(out_path)
        print(f"[dart][force] 삭제: {out_path}")
    else:
        print(f"[dart][force] {date_str} — 기존 파일 없음 (신규 수집)")


def save_dart_for_date(date_str=None, force=False):
    """특정 날짜 전체 공시 목록 저장 → db/dart/YYYY-MM/YYYYMMDD.csv"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    out_dir = _month_dir(config.DB_DART_DIR, date_str)
    out_path = f"{out_dir}/{date_str}.csv"

    if force:
        _force_delete_dart(date_str)
    elif os.path.exists(out_path):
        print(f"[dart] 이미 있음, 스킵: {out_path}")
        return

    print(f"[dart] {date_str} 공시 수집 중...")
    items = fetch_disclosures(date_str)
    if not items:
        print(f"  {date_str} 공시 없음 (휴장 또는 미공시)")
        open(out_path, "w", encoding="utf-8-sig").close()
        return

    fieldnames = list(items[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for it in items:
            writer.writerow(it)

    print(f"[dart] {len(items)}건 저장: {out_path}")


def save_dart_for_corp(corp_code, date_from, date_to=None):
    """특정 종목 공시 (corp_code 는 DART 고유번호 8자리)."""
    if date_to is None:
        date_to = datetime.now().strftime("%Y%m%d")
    items = fetch_disclosures(date_from, date_to, corp_code=corp_code)
    print(f"[dart_corp] {corp_code}: {date_from}~{date_to}: {len(items)}건")
    for it in items[:20]:
        print(f"  {it.get('rcept_dt')} {it.get('report_nm')}")
    return items


def backfill_missing(lookback_bdays=10):
    """[2026-06-13] 결측 우선: 최근 N영업일 중 빠진 공시 파일부터 수집."""
    from gap_scan import recent_missing
    missing = recent_missing(config.DB_DART_DIR, lookback_bdays=lookback_bdays,
                             include_today=False)
    if missing:
        print(f"[dart] 결측 {len(missing)}일 우선 수집: {missing}")
        for d in missing:
            save_dart_for_date(d)


def main():
    # 인자 없으면 today 기본 동작 (가드의 재수집 호출 호환)
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    cmd = args[0] if args else "today"

    if cmd == "today":
        backfill_missing()
        save_dart_for_date(force=force)
    elif cmd == "date":
        if len(args) < 2:
            print("사용법: python dart_collector.py date YYYYMMDD [--force]")
            sys.exit(1)
        save_dart_for_date(args[1], force=force)
    elif cmd == "corp":
        if len(args) < 2:
            print("사용법: python dart_collector.py corp CORP_CODE [DATE_FROM] [DATE_TO]")
            sys.exit(1)
        corp = args[1]
        df = args[2] if len(args) >= 3 else datetime.now().strftime("%Y%m%d")
        dt = args[3] if len(args) >= 4 else None
        save_dart_for_corp(corp, df, dt)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
