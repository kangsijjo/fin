"""
KRX 정보데이터시스템 → 공매도 데이터 일자별 수집.

pykrx 1.2.8 기준:
  - 신용잔고: pykrx 가 지원 안 함 (별건으로 추후 구현 필요)
  - 공매도 잔고 (get_shorting_balance_by_ticker): KRX 컬럼 변경으로 내부 버그
    → 대체 함수 fallback (volume_by_ticker → value_by_ticker → balance_top50)

KRX 일부 endpoint 는 KRX 정보데이터시스템 로그인 필요. .env 에 KRX_ID/KRX_PW 추가.
호출 시점: 매일 평일 08:30 권장 (KIS_KRX 작업).

사용법:
  python krx_collector.py short                            # 직전 영업일 공매도
  python krx_collector.py short 20260603                   # 특정 날짜
  python krx_collector.py both                             # short 만 실행 (credit 은 미지원)
  python krx_collector.py short --force 20260603           # 특정 날짜 강제 재수집
  python krx_collector.py short --force 20260601 20260610  # 날짜 범위 강제 재수집
"""

import sys
import os
from datetime import datetime, timedelta

import config

try:
    from pykrx import stock as krx_stock
except ImportError:
    print("[ERROR] pykrx 미설치. 다음 명령으로 설치하세요:")
    print("  pip install pykrx")
    sys.exit(1)


def _last_business_day():
    """직전 영업일 (주말만 고려). 공휴일은 빈 응답 시 호출자가 처리."""
    d = datetime.now() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _yesterday():
    return _last_business_day()


def _month_dir(base_dir, date_str):
    yyyy_mm = f"{date_str[:4]}-{date_str[4:6]}"
    path = f"{base_dir}/{yyyy_mm}"
    os.makedirs(path, exist_ok=True)
    return path


def _force_delete_short(date_str):
    """특정 날짜 공매도 파일 삭제 → 재수집 가능 상태로 초기화."""
    out_dir = _month_dir(config.DB_SHORT_DIR, date_str)
    out_path = f"{out_dir}/{date_str}.csv"
    if os.path.exists(out_path):
        os.remove(out_path)
        print(f"[short][force] 삭제: {out_path}")
    else:
        print(f"[short][force] {date_str} — 기존 파일 없음 (신규 수집)")


def save_credit_balance(date_str=None):
    """신용잔고 — pykrx 1.2.8 미지원. 안내 메시지만 출력."""
    print("[credit] 신용잔고는 현재 pykrx 1.2.8 에서 지원하지 않습니다. (skip)")


def save_short_balance(date_str=None):
    """공매도 데이터 (종목별, 일자 기준) → db/short/YYYY-MM/YYYYMMDD.csv.

    pykrx get_shorting_balance_by_ticker 가 KRX 컬럼 변경으로 깨진 상태 (1.2.8).
    대체 함수 순서대로 시도:
      1. get_shorting_balance_by_ticker
      2. get_shorting_volume_by_ticker
      3. get_shorting_value_by_ticker
      4. get_shorting_balance_top50
    """
    if date_str is None:
        date_str = _yesterday()

    out_dir = _month_dir(config.DB_SHORT_DIR, date_str)
    out_path = f"{out_dir}/{date_str}.csv"
    if os.path.exists(out_path):
        print(f"[short] 이미 있음, 스킵: {out_path}")
        return

    print(f"[short] {date_str} 공매도 수집 중 (fallback 체인)...")

    candidates = [
        ("balance_by_ticker", "get_shorting_balance_by_ticker"),
        ("volume_by_ticker",  "get_shorting_volume_by_ticker"),
        ("value_by_ticker",   "get_shorting_value_by_ticker"),
        ("balance_top50",     "get_shorting_balance_top50"),
    ]

    df = None
    used_source = None
    for tag, fn_name in candidates:
        fn = getattr(krx_stock, fn_name, None)
        if fn is None:
            print(f"  [skip] {fn_name} 함수 없음")
            continue
        try:
            df = fn(date_str)
            if df is not None and not df.empty:
                used_source = tag
                print(f"  [ok] {fn_name} 성공 ({len(df)}행)")
                break
            else:
                print(f"  [warn] {fn_name} 빈 결과")
        except Exception as e:
            msg = str(e).split("\n")[0][:120]
            print(f"  [fail] {fn_name}: {msg}")

    if df is None or df.empty:
        print(f"  [warn] {date_str} 모든 fallback 실패 (휴장 또는 KRX 변경)")
        return

    df = df.reset_index()
    df["__source"] = used_source
    df.to_csv(out_path, encoding="utf-8-sig", index=False)
    print(f"[short] {len(df)}행 저장 (source={used_source}): {out_path}")


def backfill_missing(lookback_bdays=10):
    """[2026-06-13] 결측 우선: 최근 N영업일 중 빠진 공매도 파일부터 수집."""
    from gap_scan import recent_missing
    missing = recent_missing(config.DB_SHORT_DIR, lookback_bdays=lookback_bdays,
                             include_today=False)
    if missing:
        print(f"[short] 결측 {len(missing)}일 우선 수집: {missing}")
        for d in missing:
            save_short_balance(d)
    return missing


def main():
    cmd = sys.argv[1] if len(sys.argv) >= 2 else "both"
    rest = sys.argv[2:]

    # --force 모드: python krx_collector.py short --force YYYYMMDD [YYYYMMDD_END]
    force_mode = "--force" in rest
    if force_mode:
        fi = rest.index("--force")
        force_dates = rest[fi + 1:]
        if not force_dates:
            print("사용법: python krx_collector.py short --force YYYYMMDD [YYYYMMDD_END]")
            sys.exit(1)
        if len(force_dates) == 1:
            target_dates = [force_dates[0]]
        else:
            import pandas as pd
            dr = pd.bdate_range(force_dates[0], force_dates[1])
            target_dates = [d.strftime("%Y%m%d") for d in dr]

        print(f"[short][force] {len(target_dates)}일 강제 재수집: {target_dates}")
        for d in target_dates:
            _force_delete_short(d)
            save_short_balance(d)
        print("[short][force] 완료.")
        return

    # 일반 날짜 인자
    date_str = rest[0] if rest else None

    if cmd in ("both", "short") and date_str is None:
        backfill_missing()

    if cmd == "credit":
        save_credit_balance(date_str)
    elif cmd == "short":
        save_short_balance(date_str)
    elif cmd == "both":
        save_credit_balance(date_str)
        save_short_balance(date_str)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
