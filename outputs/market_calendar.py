# -*- coding: utf-8 -*-
"""
market_calendar.py — 일봉 유니버스(macro_data/daily)의 '신선도' 판정.  (2026-08-20 신설)

배경
  두 트레이더의 todays_signals() 는 `latest_macro_date()`(= daily 폴더의 마지막
  CSV 파일명)를 '직전 영업일'로 간주하고 그 날짜의 신호만 매수한다. 즉 수집이
  며칠 밀리면 **트레이더는 조용히 며칠 묵은 신호로 매수**한다 — 로그에는
  "[buy] 신호 기준일: 20260814" 한 줄만 찍히므로 사람이 알아채기 어렵다.
  실제로 8월 중순 유니버스 파일이 2~4일씩 늦게 생성된 흔적이 있다
  (20260814.csv 가 08-18 20:00 에 생성 등).

  '직전 영업일 신호를 다음날 시가에 산다'가 백테스트 계약이므로, 하루만 밀려도
  진입가 가정이 한 세션 어긋난다. 반대로 주말·공휴일을 stale 로 오판하면
  멀쩡한 매매를 막게 된다 — 그래서 달력을 하드코딩하지 않고 **폴더 자체**로
  판정한다: 평일인데 `{date}.csv` 도 `{date}.csv.holiday` 마커도 없는 날만
  '빠진 거래일'로 센다(휴장일 마커는 수집기가 남긴다 — gap_scan.mark_holiday).

사용
    from market_calendar import universe_gap
    n, missing = universe_gap("20260814")     # (빠진 거래일 수, 날짜 리스트)
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

DATA_DIR = "./macro_data/daily"

# 유니버스가 이 일수 이상 밀리면 매수를 막는다(그 미만은 경고만).
#   1일 지연 = 신호가 이틀 묵음 → 진입가 가정이 한 세션 어긋나지만 방향성은 유효 → 경고
#   2일 이상 = 수집 장애가 확실 → 차단(놓친 기회보다 잘못된 가격의 매수가 더 나쁘다)
STALE_WARN_DAYS = 1
STALE_BLOCK_DAYS = 2


def universe_gap(latest_date: str, today: str | None = None,
                 data_dir: str | None = None):
    """latest_date 이후 ~ today 직전 사이에 '있어야 하는데 없는 거래일' 수를 센다.

    latest_date : 'YYYYMMDD' (보통 latest_macro_date() 결과)
    today       : 'YYYYMMDD' (기본 오늘)
    반환        : (개수, ['YYYYMMDD', ...])

    주말과 `.holiday` 마커가 있는 날은 세지 않는다. 판정 불가(형식 오류 등)면
    (0, []) — 달력 판정 실패가 매매를 막는 일은 없어야 한다(fail-open).
    """
    # 기본값을 인자 기본식이 아니라 호출 시점에 읽는다 — 인자 기본값은 import 시각에
    # 고정돼 테스트/재설정에서 DATA_DIR 변경이 반영되지 않는다.
    data_dir = data_dir or DATA_DIR
    try:
        d0 = datetime.strptime(str(latest_date).replace("-", "")[:8], "%Y%m%d")
        d1 = datetime.strptime((today or datetime.today().strftime("%Y%m%d"))
                               .replace("-", "")[:8], "%Y%m%d")
    except Exception:
        return 0, []

    missing = []
    cur = d0 + timedelta(days=1)
    while cur < d1:
        if cur.weekday() < 5:                      # 0=월 … 4=금
            ds = cur.strftime("%Y%m%d")
            csv = os.path.join(data_dir, f"{ds}.csv")
            if not os.path.exists(csv) and not os.path.exists(csv + ".holiday"):
                missing.append(ds)
        cur += timedelta(days=1)
    return len(missing), missing


def check_signal_freshness(latest_date: str, account_label: str,
                           today: str | None = None,
                           data_dir: str | None = None):
    """매수 직전 신선도 판정. 반환: (buy_allowed: bool, message: str|None).

    message 가 있으면 호출측이 로그 + 텔레그램으로 알린다.
    """
    n, missing = universe_gap(latest_date, today, data_dir)
    if n < STALE_WARN_DAYS:
        return True, None

    shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
    if n >= STALE_BLOCK_DAYS:
        return False, (
            f"🚨 [{account_label}] 유니버스가 {n}거래일 밀렸습니다 — 당일 매수 차단\n"
            f"  최신 일봉: {latest_date} / 빠진 거래일: {shown}\n"
            f"  이 상태로 매수하면 {n}일 묵은 신호를 오늘 시가에 사게 됩니다.\n"
            f"  수집(run_collector / update_macro_daily) 실패 여부를 확인해 주세요.")
    return True, (
        f"⚠ [{account_label}] 유니버스가 {n}거래일 밀렸습니다 — 매수는 진행하되 확인 요망\n"
        f"  최신 일봉: {latest_date} / 빠진 거래일: {shown}")
