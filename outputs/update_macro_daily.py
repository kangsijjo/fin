"""
macro_data/daily 증분 갱신 — 마지막 수집일 다음 날부터 오늘까지.

run_paper.bat 이 live_signal.py 직전에 호출한다.
(이전에는 스케줄러 어디에서도 macro_data 를 갱신하지 않아
 live_signal 이 옛 날짜 데이터로 신호를 재검사하는 문제가 있었음)

pykrx_collector.collect_macro_data 재사용 — 이미 수집된 날짜는 자동 스킵.

주의: 15:50 실행 시 당일 OHLCV 는 확정치, 투자자 수급은 잠정치일 수 있음.
수급(foreign_net/inst_net)은 전략 신호에 사용되지 않고 AI feature 전용이라 허용.

사용법:
  python update_macro_daily.py                          # 결측 스캔 + 오늘까지 채움
  python update_macro_daily.py --force 20260610         # 특정 날짜 강제 재수집
  python update_macro_daily.py --force 20260608 20260610  # 날짜 범위 강제 재수집
"""

import os
import glob
import sys
from datetime import datetime, timedelta

from pykrx_collector import collect_macro_data, DATA_DIR


def _force_delete(date_str):
    """특정 날짜 파일/마커 삭제 → 재수집 가능 상태로 초기화."""
    csv_path = f"{DATA_DIR}/{date_str}.csv"
    hol_path = f"{DATA_DIR}/{date_str}.csv.holiday"
    deleted = []
    for p in (csv_path, hol_path):
        if os.path.exists(p):
            os.remove(p)
            deleted.append(os.path.basename(p))
    if deleted:
        print(f"[update_macro][force] 삭제: {', '.join(deleted)}")
    else:
        print(f"[update_macro][force] {date_str} — 기존 파일 없음 (신규 수집)")


def main():
    args = sys.argv[1:]

    # --force 모드: 특정 날짜(또는 범위) 강제 재수집
    if args and args[0] == "--force":
        dates = args[1:]
        if not dates:
            print("사용법: python update_macro_daily.py --force YYYYMMDD [YYYYMMDD_END]")
            sys.exit(1)
        if len(dates) == 1:
            target_dates = [dates[0]]
        else:
            import pandas as pd
            dr = pd.bdate_range(dates[0], dates[1])
            target_dates = [d.strftime("%Y%m%d") for d in dr]

        print(f"[update_macro][force] {len(target_dates)}일 강제 재수집: {target_dates}")
        for d in target_dates:
            _force_delete(d)
        collect_macro_data(target_dates[0], target_dates[-1])
        print("[update_macro][force] 완료.")
        return

    # 일반 모드: 결측 우선 정책
    files = sorted(glob.glob(f"{DATA_DIR}/*.csv"))
    today = datetime.today()

    # [2026-06-13] 결측 우선 정책: 마지막 파일 이후만이 아니라 보유 구간 전체를
    # 스캔해 중간 구멍부터 채움. 기존 파일/휴장 마커는 즉시 스킵이라 비용 미미.
    if files:
        first = os.path.basename(files[0]).rsplit(".", 1)[0]
        start_dt = datetime.strptime(first, "%Y%m%d")
    else:
        start_dt = today - timedelta(days=365 * 3)

    print(f"[update_macro] 결측 스캔 {start_dt:%Y%m%d} ~ {today:%Y%m%d} "
          f"(기존 {len(files)}일 스킵, 구멍만 수집)")
    collect_macro_data(start_dt.strftime("%Y%m%d"), today.strftime("%Y%m%d"))

    files_after = sorted(glob.glob(f"{DATA_DIR}/*.csv"))
    latest = os.path.basename(files_after[-1]) if files_after else "없음"
    print(f"[update_macro] 완료. 최신 파일: {latest}")

    # 오늘이 평일인데 오늘자 파일이 없으면 경고 (휴장일이면 정상)
    if today.weekday() < 5:
        today_file = f"{DATA_DIR}/{today:%Y%m%d}.csv"
        if not os.path.exists(today_file):
            print(f"[update_macro][warn] 오늘({today:%Y%m%d}) 데이터 없음 — "
                  f"휴장일이거나 KRX 집계 전일 수 있음. live_signal 은 최신 가용일 기준으로 동작.")
            sys.exit(0)


if __name__ == "__main__":
    main()
