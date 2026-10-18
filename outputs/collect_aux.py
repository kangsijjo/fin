# -*- coding: utf-8 -*-
"""
collect_aux.py — 기간 백테스트 테스트기의 '데이터 부족'을 **사용자가 직접 수집**하는 실행기. (2026-10-18)

대시보드 [수집] 버튼 → 이 스크립트(별도 프로세스) → 기존 수집기 하나를 실행.
새 수집 로직은 없다 — 이미 검증된 수집기를 기간 인자만 넣어 부른다.

  kind        수집기                                               기간 인자
  individual  backfill_individual.py --backfill LO HI               LO~HI (supply_demand 의 빈 날만)
  short       backfill_shorting_fundamental.py --market KOSDAQ       --start LO --end HI (이미 있는 날 건너뜀)
  credit      Stock_AI: python -m src.collector.kiwoom_extra         --backfill --since LO --force
                                                                    (LO 이후 빈 곳을 종목별로 모두 채움, HI 무시)
  flow        update_macro_daily.py (결측 스캔 → 오늘까지)           기간 무시 — 가격·외국인·기관 최신화

끝나면
  · 상태 파일 db/period_bt/collect_{kind}.json (대시보드가 3초마다 읽음)
  · 데이터 구간 캐시(coverage.json) 무효화 + 개인 순매수면 일봉 로더 캐시도 무효화(로더 서명이
    stock.db 변경을 모름 — 무효화 안 하면 다음 백테스트가 옛 개인 데이터를 그대로 씀)
  · 새 데이터 구간을 다시 계산해 상태 파일·텔레그램에 표시

사용: python collect_aux.py --kind individual --start 20180614 --end 20201230
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STOCK_AI = os.path.join(os.path.dirname(HERE), "Stock_AI_Project")
STATE_DIR = os.path.join(HERE, "db", "period_bt")
LOG_DIR = os.path.join(HERE, "logs")
LOADER_DIR = os.path.join(HERE, "macro_data", "daily")


def _py_outputs():
    p = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    return p if os.path.exists(p) else sys.executable


def _py_stockai():
    p = os.path.join(STOCK_AI, "venv", "Scripts", "python.exe")
    return p if os.path.exists(p) else _py_outputs()


def _iso(d8):
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"


# kind → 설명 + 명령 생성기. 대시보드도 이 표를 읽어 화면에 쓴다(라벨·주의문 단일 출처).
KINDS = {
    "individual": {"label": "개인 순매수", "aux": ["individual"], "range": True,
                   "src": "KRX(pykrx·로그인)", "eta": "하루치 약 3~5초 — 1년 ≈ 15~20분",
                   "cmd": lambda s, e: ([_py_outputs(), "-u", os.path.join(HERE, "backfill_individual.py"),
                                         "--backfill", s, e], HERE)},
    "short": {"label": "공매도 거래비중", "aux": ["short"], "range": True,
              "src": "KRX(pykrx)", "eta": "하루치 약 3~5초 — 1년 ≈ 15~20분 · 2020-03~2021-05 공매도 금지기간은 값이 0",
              "cmd": lambda s, e: ([_py_outputs(), "-u", os.path.join(HERE, "backfill_shorting_fundamental.py"),
                                    "--market", "KOSDAQ", "--start", s, "--end", e], HERE)},
    "credit": {"label": "신용잔고", "aux": ["credit"], "range": False,
               "src": "키움 REST", "eta": "종목 수천 개 × 수 초 — 수 시간. 장중(09:00~15:30) 피하기 · "
                                        "시작일 이후 빈 곳을 모두 채움(종료일 무시) · 2022년 이전 제공 여부는 키움 미확인",
               "cmd": lambda s, e: ([_py_stockai(), "-m", "src.collector.kiwoom_extra", "--backfill",
                                     "--since", _iso(s), "--force"], STOCK_AI)},
    "flow": {"label": "가격·외국인·기관", "aux": ["foreign", "inst"], "range": False,
             "src": "KRX(pykrx)", "eta": "빠진 날만 — 보통 1~2분 · 기간 무시(마지막 날 ~ 오늘)",
             "cmd": lambda s, e: ([_py_outputs(), "-u", os.path.join(HERE, "update_macro_daily.py")], HERE)},
}
AUX_TO_KIND = {a: k for k, v in KINDS.items() for a in v["aux"]}


def state_path(kind):
    return os.path.join(STATE_DIR, f"collect_{kind}.json")


def _write_state(kind, st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = state_path(kind) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, state_path(kind))


def _tail(path, n=6):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip() for l in fh.readlines()[-200:] if l.strip()]
        # tqdm 진행줄(\r) 정리
        return [l.split("\r")[-1][:200] for l in lines[-n:]]
    except Exception:
        return []


def invalidate_caches(kind):
    """수집 결과가 다음 점검·백테스트에 바로 반영되게 캐시를 '틀린 서명'으로 덮는다(삭제 아님)."""
    import period_backtest as P
    try:
        if os.path.exists(P.COVERAGE_JSON):
            with open(P.COVERAGE_JSON, "w", encoding="utf-8") as fh:
                json.dump({"data_max": "", "invalidated": kind}, fh)
    except Exception:
        pass
    if kind == "individual":            # backfill_individual 도 성공 시 직접 무효화한다(이중 안전)
        try:
            from strategies.daily_loader import invalidate_cache
            invalidate_cache(kind, LOADER_DIR)
        except Exception:
            pass


def _notify(text):
    try:
        from notifier import safe_send
        safe_send(text)
    except Exception:
        pass


def run(kind, start, end):
    spec = KINDS[kind]
    cmd, cwd = spec["cmd"](start, end)
    os.makedirs(LOG_DIR, exist_ok=True)
    log = os.path.join(LOG_DIR, f"collect_{kind}.log")
    st = {"kind": kind, "label": spec["label"], "start": start, "end": end, "status": "running",
          "pid": os.getpid(), "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tail": []}
    _write_state(kind, st)
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    with open(log, "w", encoding="utf-8", errors="replace") as lf:
        lf.write(f"[collect_aux] {kind} {start}~{end}\n$ {' '.join(cmd)}\n")
        lf.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while proc.poll() is None:
            time.sleep(5)
            st.update(tail=_tail(log), elapsed_sec=int(time.time() - t0))
            _write_state(kind, st)
    rc = proc.returncode
    invalidate_caches(kind)
    cov = None
    try:                                     # 새 구간 계산(개인 순매수는 로더 캐시 재생성 ~1분)
        import period_backtest as P
        cal = P.trading_calendar()
        full = P.aux_coverage(cal, force=True)
        cov = {a: full.get(a) for a in spec["aux"]}
    except Exception as e:
        st["cov_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    st.update(status="done" if rc == 0 else "error", rc=rc, tail=_tail(log, 10),
              elapsed_sec=int(time.time() - t0), coverage=cov,
              finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _write_state(kind, st)
    rng = " · ".join(f"{a} {c[0]}~{c[1]}" if c else f"{a} 없음" for a, c in (cov or {}).items())
    mins = st["elapsed_sec"] // 60
    if rc == 0:
        _notify(f"✅ [데이터 수집] {spec['label']} 완료 ({start}~{end}, {mins}분)\n지금 구간: {rng or '-'}")
    else:
        _notify(f"🚨 [데이터 수집] {spec['label']} 실패 rc={rc} ({mins}분) — logs/collect_{kind}.log 확인")
    return rc


def main():
    os.chdir(HERE)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=sorted(KINDS))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    a = ap.parse_args()
    if not (re.fullmatch(r"\d{8}", a.start) and re.fullmatch(r"\d{8}", a.end)) or a.start > a.end:
        sys.exit("날짜는 YYYYMMDD, 시작 ≤ 종료")
    try:
        rc = run(a.kind, a.start, a.end)
    except Exception as e:
        _write_state(a.kind, {"kind": a.kind, "status": "error", "start": a.start, "end": a.end,
                              "error": f"{type(e).__name__}: {str(e)[:200]}",
                              "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        _notify(f"🚨 [데이터 수집] {a.kind} 실행 실패 — {type(e).__name__}: {str(e)[:120]}")
        raise
    sys.exit(rc)


if __name__ == "__main__":
    main()
