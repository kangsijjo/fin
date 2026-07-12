"""
macro_data/daily 증분 갱신 — 마지막 수집일 다음 날부터 오늘까지.

run_paper.bat 이 live_signal.py 직전에 호출한다.
(이전에는 스케줄러 어디에서도 macro_data 를 갱신하지 않아
 live_signal 이 옛 날짜 데이터로 신호를 재검사하는 문제가 있었음)

pykrx_collector.collect_macro_data 재사용 — 이미 수집된 날짜는 자동 스킵.

주의: 15:50 실행 시 당일 OHLCV 는 확정치, 투자자 수급은 잠정치일 수 있음.
[2026-07-10 정정] 과거 "수급은 전략 신호에 미사용(AI 전용)" 전제는 KIS 안D 도입으로
무효 — h52w_for3d_mkt/for_high20_mkt/gc_for3d 가 '외국인 3일 연속 순매수'를 필수로
쓴다. '있으면 스킵' 정책 탓에 당일 잠정치가 영구 동결되던 것을, 매 실행 시 직전
거래일 1개를 확정치로 재수집(main 의 refresh 블록)해 보정한다. 당일치는 여전히
잠정 — 18:30 신호는 그 시점 최선의 데이터로 판단(실거래와 동일한 정보 제약).

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


def _refresh_last_trading_day(last):
    """직전 거래일 확정치 재수집 — 임시폴더 수집 → 검증 → 원자 교체(2026-07-12 재설계).

    [기존 구현의 critical 결함] 원본 CSV 를 os.remove 한 뒤 재수집하던 초기 버전은
    ① KRX 스로틀이 빈 응답을 1회만 줘도 pykrx_collector 가 .holiday 마커를 써서
      '실제 거래일'이 가짜 휴장으로 영구 스킵되고(모든 백필 경로가 마커를 존중),
    ② 삭제~재작성 사이 크래시/재부팅이면 그날 18:30 신호가 직전 거래일이 통째로
      빠진 데이터로 생성됐다(외국인 3일 연속·RSI·신고가 전부 하루 시프트).
    이제 원본은 '검증된 새 파일'로 교체되기 전까지 절대 삭제되지 않는다:
    임시폴더에 수집 → 행수 검증(기존의 90% 이상) → os.replace 원자 교체.
    실패 시 기존(잠정) 파일 유지 + 텔레그램 경고 — 다음날 재시도가 자연 커버."""
    import pykrx_collector as _pkc
    real_csv = f"{DATA_DIR}/{last}.csv"
    if not os.path.exists(real_csv):
        return
    try:
        with open(real_csv, encoding="utf-8-sig") as f:
            n_old = max(0, sum(1 for _ in f) - 1)
    except Exception:
        n_old = 0
    tmp_dir = os.path.join(DATA_DIR, "_refresh_tmp")
    try:
        os.makedirs(tmp_dir, exist_ok=True)
        for f in glob.glob(os.path.join(tmp_dir, "*")):
            os.remove(f)
        print(f"[update_macro] 직전 거래일 {last} 수급 확정치 재수집(임시폴더 검증 후 교체)")
        _orig = _pkc.DATA_DIR
        try:
            _pkc.DATA_DIR = tmp_dir       # collect 가 임시폴더에 쓰도록 전환
            collect_macro_data(last, last)
        finally:
            _pkc.DATA_DIR = _orig
        tmp_csv = os.path.join(tmp_dir, f"{last}.csv")
        if not os.path.exists(tmp_csv):
            raise RuntimeError("재수집 결과 없음(빈 응답/스로틀 의심) — 기존 확정 파일 유지")
        with open(tmp_csv, encoding="utf-8-sig") as f:
            n_new = max(0, sum(1 for _ in f) - 1)
        if n_old > 0 and n_new < n_old * 0.9:
            raise RuntimeError(f"재수집 행수 급감({n_old}→{n_new}) — 기존 파일 유지")
        os.replace(tmp_csv, real_csv)
        print(f"[update_macro] {last} 확정치 교체 완료({n_old}→{n_new}행)")
    except Exception as e:
        print(f"[update_macro][WARN] {last} 확정치 재수집 보류: {e}")
        try:
            import notifier
            notifier.safe_send(f"⚠ [update_macro] {last} 확정치 재수집 실패 — 기존(잠정) 파일 유지: {str(e)[:80]}")
        except Exception:
            pass
    finally:
        for f in glob.glob(os.path.join(tmp_dir, "*")):
            try:
                os.remove(f)
            except OSError:
                pass


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

    # [2026-07-10] 직전 거래일 확정치 재수집 — 15:50 당일 수집분의 수급(외국인/기관)은
    # 잠정치인데 '있으면 스킵' 정책이라 영구 동결됐었음(KIS 3전략의 외국인 조건 입력 오염
    # + 확정치로 백필된 과거 데이터와의 비대칭 → 백테스트 vs 라이브 조건 불일치).
    # 직전 파일 1개만 지우고 즉시 재수집(비용 ~수초). 실패해도 아래 결측 스캔이 같은
    # 구멍을 즉시 재시도하고, 그래도 안 되면 주말 갭백필이 메운다.
    # ※ '오늘보다 이전' 중 최신 파일을 고른다 — run_paper.bat 은 pykrx_collector(오늘
    #   파일 생성)를 먼저 돌리고 이 스크립트를 돌리므로 files[-1]은 항상 '오늘'이라
    #   files[-1] 기준이면 refresh 가 영원히 미발동(2026-07-10 초기구현 결함 수정).
    today_str = today.strftime("%Y%m%d")
    prev_days = [os.path.basename(f).rsplit(".", 1)[0] for f in files]
    prev_days = [d for d in prev_days if d.isdigit() and len(d) == 8 and d < today_str]
    if prev_days:
        last = max(prev_days)
        last_dt = datetime.strptime(last, "%Y%m%d")
        if (today - last_dt).days <= 7:
            _refresh_last_trading_day(last)
        files = sorted(glob.glob(f"{DATA_DIR}/*.csv"))

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
