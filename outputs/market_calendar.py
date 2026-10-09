# -*- coding: utf-8 -*-
"""
market_calendar.py — 일봉 유니버스(macro_data/daily)의 '신선도' 판정 + KRX 휴장일 달력.
(2026-08-20 신설 / 2026-10-09 휴장일 목록·신선도 대기 추가)

배경
  두 트레이더의 todays_signals() 는 `latest_macro_date()`(= daily 폴더의 마지막
  CSV 파일명)를 '직전 영업일'로 간주하고 그 날짜의 신호만 매수한다. 즉 수집이
  며칠 밀리면 **트레이더는 조용히 며칠 묵은 신호로 매수**한다 — 로그에는
  "[buy] 신호 기준일: 20260814" 한 줄만 찍히므로 사람이 알아채기 어렵다.
  실제로 8월 중순 유니버스 파일이 2~4일씩 늦게 생성된 흔적이 있다
  (20260814.csv 가 08-18 20:00 에 생성 등).

  '직전 영업일 신호를 다음날 시가에 산다'가 백테스트 계약이므로, 하루만 밀려도
  진입가 가정이 한 세션 어긋난다. 반대로 주말·공휴일을 stale 로 오판하면
  멀쩡한 매매를 막게 된다 — 평일인데 `{date}.csv` 도 `{date}.csv.holiday` 마커도
  없는 날만 '빠진 거래일'로 센다(휴장일 마커는 수집기가 남긴다 — gap_scan.mark_holiday).

[2026-10-09] 휴장일 목록(krx_holidays.txt) 추가 — 마커만으로는 부족했다
  `.holiday` 마커는 수집기가 **그날을 수집하려다 데이터가 없을 때 사후에** 만든다.
  그래서 PC 가 휴장일 오후에 꺼져 있으면 마커가 없어 다음 거래일 아침에 휴장일을
  '빠진 거래일'로 오판했다(10-06 09:00 "10-05 누락" 경고 — 10-05 는 개천절 대체공휴일,
  마커는 10-08 11:21 에야 생성). 워치독의 영업일 나이·매매 미실행 판정도 공휴일을
  몰라 추석 연휴(09-24·25)마다 '7~8영업일 미갱신' 오경보를 냈다.
  → KRX 가 매년 공고하는 휴장일을 krx_holidays.txt 에 **사전에** 적어 두고
    마커와 OR 로 쓴다. 목록 누락 시엔 종전(마커만) 동작으로 자연 폴백.

[2026-10-09] 신선도 대기(wait_until_fresh) 추가 — 부팅 직후 경합
  PC 가 꺼져 있다 켜지면 StartWhenAvailable 로 밀린 작업이 한꺼번에 돈다.
  10-08 11:20 신호 작업이 유니버스 수집(KIS_Paper, 11:21)보다 먼저 실행돼
  10-02 데이터로 '신규 0건' → 사용자가 수동 재실행해야 했다.
  신호 bat 이 실행 전에 `python market_calendar.py --wait-fresh 30` 으로
  기대 날짜 파일이 생길 때까지 최대 30분 기다린다(이미 신선하면 즉시 통과).

사용
    from market_calendar import universe_gap, is_trading_day
    n, missing = universe_gap("20260814")     # (빠진 거래일 수, 날짜 리스트)
    python market_calendar.py --wait-fresh 30 [--label 키움]   # exit 0=신선, 3=시간초과
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "./macro_data/daily"
HOLIDAY_FILE = os.path.join(_HERE, "krx_holidays.txt")

# 유니버스가 이 일수 이상 밀리면 매수를 막는다(그 미만은 경고만).
#   1일 지연 = 신호가 이틀 묵음 → 진입가 가정이 한 세션 어긋나지만 방향성은 유효 → 경고
#   2일 이상 = 수집 장애가 확실 → 차단(놓친 기회보다 잘못된 가격의 매수가 더 나쁘다)
STALE_WARN_DAYS = 1
STALE_BLOCK_DAYS = 2

# 당일 유니버스 파일이 생겼어야 하는 시각(HHMM). KIS_Paper(update_macro_daily)가
# 15:50 전후에 당일 파일을 만든다 → 16:00 이후의 신호 작업은 '오늘' 파일을 기대.
FRESH_CUTOFF_HHMM = "1600"


# ── 휴장일 달력 ───────────────────────────────────────────────────────────────
def load_holidays(path: str | None = None) -> set:
    """krx_holidays.txt → {'YYYYMMDD', ...}. 줄 앞 8자리가 숫자인 줄만 읽는다
    ('#' 주석·설명 허용). 파일이 없거나 읽기 실패면 빈 집합(=종전 동작)."""
    out = set()
    try:
        with open(path or HOLIDAY_FILE, encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                if len(s) >= 8 and s[:8].isdigit():
                    out.add(s[:8])
    except Exception:
        pass
    return out


def _as_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d).replace("-", "")[:8], "%Y%m%d").date()


def is_trading_day(d=None, holidays: set | None = None) -> bool:
    """KRX 거래일인가 — 주말이 아니고 휴장일 목록에 없으면 True."""
    d = _as_date(d or date.today())
    hol = load_holidays() if holidays is None else holidays
    return d.weekday() < 5 and d.strftime("%Y%m%d") not in hol


def prev_trading_day(d=None, holidays: set | None = None) -> date:
    """d 직전(당일 제외) 거래일."""
    d = _as_date(d or date.today())
    hol = load_holidays() if holidays is None else holidays
    cur = d - timedelta(days=1)
    for _ in range(30):                    # 최장 연휴도 30일은 안 넘는다(무한루프 방지)
        if is_trading_day(cur, hol):
            return cur
        cur -= timedelta(days=1)
    return cur


# ── 유니버스 신선도 (매수 게이트) ─────────────────────────────────────────────
def universe_gap(latest_date: str, today: str | None = None,
                 data_dir: str | None = None, holidays: set | None = None):
    """latest_date 이후 ~ today 직전 사이에 '있어야 하는데 없는 거래일' 수를 센다.

    latest_date : 'YYYYMMDD' (보통 latest_macro_date() 결과)
    today       : 'YYYYMMDD' (기본 오늘)
    holidays    : 휴장일 집합(기본 krx_holidays.txt). 테스트에서 set() 로 격리 가능.
    반환        : (개수, ['YYYYMMDD', ...])

    주말·휴장일 목록·`.holiday` 마커가 있는 날은 세지 않는다. 판정 불가(형식 오류 등)면
    (0, []) — 달력 판정 실패가 매매를 막는 일은 없어야 한다(fail-open).
    """
    # 기본값을 인자 기본식이 아니라 호출 시점에 읽는다 — 인자 기본값은 import 시각에
    # 고정돼 테스트/재설정에서 DATA_DIR 변경이 반영되지 않는다.
    data_dir = data_dir or DATA_DIR
    hol = load_holidays() if holidays is None else holidays
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
            if (ds not in hol and not os.path.exists(csv)
                    and not os.path.exists(csv + ".holiday")):
                missing.append(ds)
        cur += timedelta(days=1)
    return len(missing), missing


def check_signal_freshness(latest_date: str, account_label: str,
                           today: str | None = None,
                           data_dir: str | None = None,
                           holidays: set | None = None):
    """매수 직전 신선도 판정. 반환: (buy_allowed: bool, message: str|None).

    message 가 있으면 호출측이 로그 + 텔레그램으로 알린다.
    """
    n, missing = universe_gap(latest_date, today, data_dir, holidays)
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


# ── 신선도 대기 (신호 작업 전, 부팅 직후 경합 방지) ───────────────────────────
def latest_universe_date(data_dir: str | None = None) -> str | None:
    """daily 폴더의 마지막 YYYYMMDD.csv (트레이더 latest_macro_date 와 같은 기준)."""
    d = data_dir or DATA_DIR
    try:
        names = [f[:8] for f in os.listdir(d)
                 if len(f) == 12 and f.endswith(".csv") and f[:8].isdigit()]
    except Exception:
        return None
    return max(names) if names else None


def expected_universe_date(now: datetime | None = None,
                           holidays: set | None = None) -> str:
    """지금 시점에 있어야 할 최신 유니버스 날짜.
    거래일 16:00 이후 → 오늘 / 그 외(장중·장전·휴일) → 직전 거래일."""
    now = now or datetime.now()
    hol = load_holidays() if holidays is None else holidays
    if is_trading_day(now, hol) and now.strftime("%H%M") >= FRESH_CUTOFF_HHMM:
        return now.strftime("%Y%m%d")
    return prev_trading_day(now, hol).strftime("%Y%m%d")


def universe_is_fresh(now: datetime | None = None, data_dir: str | None = None,
                      holidays: set | None = None):
    """→ (fresh: bool, latest: str|None, expected: str)"""
    latest = latest_universe_date(data_dir)
    exp = expected_universe_date(now, holidays)
    return (latest is not None and latest >= exp), latest, exp


def wait_until_fresh(max_minutes: float = 30, poll_sec: float = 60, label: str = "",
                     data_dir: str | None = None, notify: bool = True,
                     _now=None, _sleep=time.sleep) -> int:
    """유니버스가 기대 날짜까지 채워질 때까지 기다린다. 반환 0=신선 / 3=시간초과.

    시간초과여도 호출측(bat)은 신호 작업을 그대로 진행한다 — 여기서 막으면 수집기
    장애 하나가 신호 기록(사후검증 데이터)까지 끊는다. 대신 텔레그램으로 알린다.
    _now/_sleep 는 테스트 주입용.
    """
    clock = _now or datetime.now
    hol = load_holidays()
    tag = f"[fresh-wait{(' ' + label) if label else ''}]"
    waited = 0.0
    announced = False
    while True:
        ok, latest, exp = universe_is_fresh(clock(), data_dir, hol)
        if ok:
            if announced:
                print(f"{tag} 유니버스 {latest} 확인 — {waited / 60:.1f}분 대기 후 진행", flush=True)
            return 0
        if not announced:
            print(f"{tag} 유니버스 최신 {latest} < 기대 {exp} — 수집 완료 대기"
                  f"(최대 {max_minutes:g}분, {poll_sec:g}초 간격)", flush=True)
            announced = True
        if waited >= max_minutes * 60:
            msg = (f"⚠ {tag} {max_minutes:g}분 기다렸지만 유니버스가 {latest} 에 머물러 있음"
                   f"(기대 {exp}). 신호는 이 데이터로 그대로 생성합니다 — "
                   f"수집(KIS_Paper / update_macro_daily) 실패 여부를 확인하세요.")
            print(msg, flush=True)
            if notify:
                try:
                    import notifier
                    notifier.safe_send(msg)
                except Exception:
                    pass
            return 3
        _sleep(poll_sec)
        waited += poll_sec


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = sys.argv[1:]
    if "--wait-fresh" in args:
        i = args.index("--wait-fresh")
        try:
            mins = float(args[i + 1])
        except Exception:
            mins = 30.0
        lbl = args[args.index("--label") + 1] if "--label" in args and \
            args.index("--label") + 1 < len(args) else ""
        try:
            sys.exit(wait_until_fresh(mins, label=lbl))
        except SystemExit:
            raise
        except Exception as e:        # 대기 로직 오류가 신호 작업을 막지 않게(fail-open)
            print(f"[fresh-wait] 판정 오류(무시하고 진행): {e}")
            sys.exit(0)
    elif "--is-trading-day" in args:
        print("1" if is_trading_day() else "0")
    else:
        print(__doc__)
