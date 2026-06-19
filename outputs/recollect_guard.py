"""
20:00 수집 검증 + 재수집 가드.

매일 20:00 실행 (KIS_Recheck 작업):
  1. macro_data 증분 갱신 (update_macro_daily) — 누락일 자동 보충
  2. 오늘이 거래일인지 판정 (macro_data 에 오늘 파일 존재 여부)
  3. 거래일이면 당일 산출물 체크리스트 검사:
       macro_data/daily, db/daily, db/investor, db/index, db/short,
       db/dart, db/us_market, db/credit, ai_data feature
  4. 누락 항목의 수집기를 재실행 → 재검사
  5. macro 가 뒤늦게 채워졌으면 live_signal → paper_tracker → dashboard 재실행
  6. 결과 요약을 logs/recheck_YYYYMMDD.log 에 기록. 실패 잔존 시 exit 1.

수동 실행: python recollect_guard.py  (또는 run_recheck.bat)
"""

import os
import sys
import glob
import subprocess
from datetime import datetime, timedelta

TODAY = datetime.today().strftime("%Y%m%d")
MONTH = datetime.today().strftime("%Y-%m")
PY = sys.executable


def prev_bdate():
    """직전 영업일 (주말만 고려 — 공휴일은 KRX 파일 부재로 자연 처리)."""
    d = datetime.today() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


PREV = prev_bdate().strftime("%Y%m%d")
PREV_MONTH = prev_bdate().strftime("%Y-%m")


_RAN = {}   # 같은 수집기 세션 내 1회만 실행 (data_collector 3중 호출 방지)


def run(script, *args, timeout=1800):
    """수집기 실행 — (성공여부, 메시지)."""
    key = (script, args)
    if key in _RAN:
        print(f"  → (이번 세션에 이미 실행됨: {script})")
        return _RAN[key]
    cmd = [PY, script, *args]
    print(f"  → 실행: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        tail = (r.stdout or "")[-300:].strip()
        if r.returncode != 0:
            tail += ("\n" + (r.stderr or "")[-300:]).strip()
        _RAN[key] = (r.returncode == 0, tail)
    except subprocess.TimeoutExpired:
        _RAN[key] = (False, f"{script} 타임아웃({timeout}s)")
    except Exception as e:
        _RAN[key] = (False, f"{script} 실행 불가: {e}")
    return _RAN[key]


# (이름, 오늘자 파일 경로 패턴, 재수집 [script, args...], 필수 여부)
def checklist():
    return [
        ("macro_daily",  f"./macro_data/daily/{TODAY}.csv",
         ["update_macro_daily.py"], True),
        ("kis_daily",    f"./db/daily/{MONTH}/{TODAY}.csv",
         ["data_collector.py", "today"], True),
        ("kis_investor", f"./db/investor/{MONTH}/{TODAY}.csv",
         ["data_collector.py", "today"], False),   # data_collector 가 같이 수집
        ("kis_index",    f"./db/index/{MONTH}/{TODAY}.csv",
         ["data_collector.py", "today"], False),
        ("krx_short",    f"./db/short/{PREV_MONTH}/{PREV}.csv",
         ["krx_collector.py", "both"], False),  # 공매도는 T-1 데이터 → 전일자 파일 검사
        ("dart",         f"./db/dart/{MONTH}/{TODAY}.csv",
         ["dart_collector.py"], False),
        # us_market 은 macro_collector 가 indicators.csv 에 수집 → ALWAYS_RUN 의 macro_ind 로 커버
        # ("us_market", ...) 항목 제거 (us_market_collector.py 삭제됨)
        ("credit",       f"./db/credit/{MONTH}/{TODAY}.csv",
         ["kiwoom_collector.py", "credit"], False),   # 키움 ka10013 (KIS probe 대체)
        ("program",      f"./db/program/{MONTH}/{TODAY}.csv",
         ["kiwoom_collector.py", "program"], False),  # 키움 ka90013 프로그램매매
        ("macro_ind",    None,             # 항상 재실행 (멱등) — 아래 ALWAYS_RUN 처리
         ["macro_collector.py"], False),
        ("ai_features",  None,             # 항상 재실행 (멱등) — 아래 ALWAYS_RUN 처리
         ["historical_feature_builder.py"], False),
    ]


def exists(pattern):
    if pattern is None:
        return False
    return os.path.exists(pattern)


def main():
    print(f"=== 재수집 가드 ({TODAY} {datetime.now():%H:%M}) ===\n")

    # 1) macro 증분 갱신 (멱등 — 누락된 과거 일자까지 보충)
    macro_was_stale = not exists(f"./macro_data/daily/{TODAY}.csv")
    ok, msg = run("update_macro_daily.py")
    if not ok:
        print(f"  [warn] update_macro_daily 실패: {msg}")

    # 2) 거래일 판정
    if not exists(f"./macro_data/daily/{TODAY}.csv"):
        # 오늘 파일이 여전히 없으면 휴장일 가능성 — bdate 면 경고만
        wd = datetime.today().weekday()
        if wd >= 5:
            print("주말 — 검사 생략.")
            return
        print("[판정] 오늘자 KRX 데이터 없음 → 휴장일이거나 KRX 집계 지연.")
        print("       (집계 지연이면 내일 update_macro_daily 가 자동 보충)")
        return

    # 3) 체크리스트 검사 + 재수집 (같은 수집기는 세션당 1회만 실행됨)
    # pattern=None 항목은 "항상 실행" (멱등 수집기) — "누락" 이 아니라 "[갱신]" 로 출력
    ALWAYS_RUN = {"macro_ind", "ai_features"}
    pending = []
    for name, pattern, fix_cmd, required in checklist():
        if pattern is not None and exists(pattern):
            print(f"[OK] {name}")
            continue
        if name in ALWAYS_RUN:
            print(f"[갱신] {name} (멱등 재실행)")
        else:
            print(f"[누락] {name} → 재수집 시도")
        timeout = 3600 if fix_cmd[0] == "data_collector.py" else 1800
        ok, msg = run(*fix_cmd, timeout=timeout)
        good = exists(pattern) if pattern is not None else ok
        if good:
            print(f"  [OK] {name}")
        else:
            pending.append((name, pattern, required, msg))

    # 3-2) 최종 재검증 — 뒤 단계 수집기 실행으로 앞 항목이 뒤늦게 채워졌을 수 있음
    failures = []
    for name, pattern, required, msg in pending:
        if pattern is not None and exists(pattern):
            print(f"  [복구·후행] {name} (다른 단계 실행으로 충족)")
            continue
        level = "FAIL" if required else "warn"
        print(f"  [{level}] {name} 재수집 후에도 누락 — {msg[:200]}")
        if required:
            failures.append(name)

    # 4) macro 가 이번에 채워졌으면 신호/추적/대시보드 재생성
    if macro_was_stale:
        print("\n[후속] macro 늦은 갱신 감지 → 신호/추적/대시보드 재실행")
        for s in ["live_signal.py", "paper_tracker.py", "dashboard_generator.py"]:
            ok, msg = run(s)
            print(f"  {'[OK]' if ok else '[FAIL]'} {s}")
            if not ok:
                failures.append(s)

    # 5) 요약
    print("\n=== 결과 ===")
    if failures:
        print(f"실패 잔존: {', '.join(failures)}")
        sys.exit(1)
    print("모든 필수 항목 정상.")


if __name__ == "__main__":
    main()
