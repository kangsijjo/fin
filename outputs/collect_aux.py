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
    """상태 파일 기록 — 실패해도 수집을 멈추지 않는다(period_backtest.write_json_atomic 주석 참고)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    from period_backtest import write_json_atomic
    write_json_atomic(state_path(kind), st)


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


# ── 결과 검증: 로그가 아니라 **데이터 자체**를 보고 '빠진 거래일'을 센다 ─────────────
# 수집기 종료코드 0 이어도 0행일 수 있다(이 시스템의 반복 사고 유형) → 요청 구간 안에서
# 실제로 비어 있는 거래일을 직접 세어 완료/일부/실패를 정한다.
def _cal():
    import period_backtest as P
    return P.trading_calendar()


def _db_dates(sql, args):
    import sqlite3
    from fin_paths import STOCK_DB
    con = sqlite3.connect(f"file:{STOCK_DB}?mode=ro", uri=True, timeout=60)
    try:
        return {str(r[0]).replace("-", "")[:8] for r in con.execute(sql, args)}
    finally:
        con.close()


def have_days(kind, start, end):
    """kind 데이터가 실제로 있는 거래일 집합(YYYYMMDD)."""
    if kind == "short":
        d = os.path.join(HERE, "macro_data", "shortfund_kosdaq")
        return {f[:8] for f in os.listdir(d) if f.endswith(".csv")} if os.path.isdir(d) else set()
    if kind == "individual":
        return _db_dates("SELECT DISTINCT date FROM supply_demand WHERE date BETWEEN ? AND ? "
                         "AND individual_net_value IS NOT NULL", (_iso(start), _iso(end)))
    if kind == "credit":
        return _db_dates("SELECT DISTINCT date FROM credit_balance WHERE date BETWEEN ? AND ?",
                         (_iso(start), _iso(end)))
    if kind == "flow":
        return set(_cal())
    raise ValueError(kind)


def check_range(kind, start, end, cal=None):
    """요청 구간 안의 빠진 거래일 → (대상 거래일 수, 빠진 날 목록). flow 는 '기대 최신일'까지."""
    cal = cal or _cal()
    if kind == "flow":
        from market_calendar import expected_universe_date
        exp = expected_universe_date()
        return 1, ([] if cal and cal[-1] >= exp else [exp])
    days = [d for d in cal if start <= d <= end]
    have = have_days(kind, start, end)
    return len(days), [d for d in days if d not in have]


def fmt_ranges(missing, cal, limit=3):
    """빠진 날을 연속 구간으로 묶어 '20200316~20200320(5일)' 형태로."""
    if not missing:
        return ""
    idx = {d: i for i, d in enumerate(cal)}
    runs, cur = [], [missing[0]]
    for d in missing[1:]:
        if d in idx and cur[-1] in idx and idx[d] == idx[cur[-1]] + 1:
            cur.append(d)
        else:
            runs.append(cur)
            cur = [d]
    runs.append(cur)
    txt = [f"{r[0]}~{r[-1]}({len(r)}일)" if len(r) > 1 else r[0] for r in runs[:limit]]
    return ", ".join(txt) + (f" 외 {len(runs) - limit}구간" if len(runs) > limit else "")


SHORT_BAN = ("20200316", "20210502")   # 공매도 전면 금지기간 — 비어 있어도 정상


def _verdict(kind, start, end, rc, cal=None, note=""):
    """→ step dict {kind,label,status: done|partial|failed|skipped, missing_n, n_days, ranges, rc, note}
    상태는 종료코드가 아니라 '구간 안 빠진 거래일'로 정한다: 0일=완료 / 전부=실패 / 그 사이=일부."""
    spec = KINDS[kind]
    out = {"kind": kind, "label": spec["label"], "start": start, "end": end, "rc": rc, "note": note}
    try:
        cal = cal or _cal()
        n, miss = check_range(kind, start, end, cal)
        if kind == "short":
            ban = [d for d in miss if SHORT_BAN[0] <= d <= SHORT_BAN[1]]
            if ban:
                miss = [d for d in miss if d not in set(ban)]
                n -= len(ban)
                out["note"] = "; ".join(x for x in (note, f"공매도 금지기간 {len(ban)}일은 제외(정상)") if x)
        out.update(n_days=n, missing_n=len(miss), ranges=fmt_ranges(miss, cal))
        out["status"] = "done" if not miss else ("failed" if len(miss) >= n else "partial")
    except Exception as e:
        out.update(status="done" if rc == 0 else "failed", missing_n=None,
                   note="; ".join(x for x in (note, f"검증 실패({type(e).__name__}: {str(e)[:80]})") if x))
    return out


_ICON = {"done": "✅", "partial": "⚠", "failed": "❌", "skipped": "⏭"}


def step_line(v):
    """텔레그램 한 줄: '✅ 개인 순매수 — 20180614~20261015 빠진 날 0' / '⚠ … 중 12/2000거래일 미완료: …'"""
    m = v.get("missing_n")
    if v["status"] == "skipped" or m is None:
        body = ""
    elif v["kind"] == "flow":
        body = "최신" if not m else f"{v.get('ranges')} 데이터가 아직 없음"
    elif not m:
        body = f"{v['start']}~{v['end']} 빠진 날 0"
    else:
        body = f"{v['start']}~{v['end']} 중 {m}/{v['n_days']}거래일 미완료: {v['ranges']}"
    rc = f" (rc={v['rc']})" if v.get("rc") not in (0, None) else ""
    parts = [x for x in (body, v.get("note")) if x]
    return f"{_ICON.get(v['status'], '')} {v['label']}{rc} — " + " · ".join(parts)


# ── 실행 ─────────────────────────────────────────────────────────────────────
LOCK_PATH = os.path.join(STATE_DIR, "collect.lock")
LOCK_STALE_SEC = 120


def _acquire_lock():
    """대시보드 버튼·bat 이 동시에 돌지 않게. 실행 중엔 5초마다 갱신 — 2분 넘게 안 바뀌면 죽은 것."""
    os.makedirs(STATE_DIR, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK_PATH) > LOCK_STALE_SEC:
                    os.replace(LOCK_PATH, LOCK_PATH + ".stale")       # 죽은 실행기의 자물쇠 치우기(삭제 아님)
                    continue
            except OSError:
                continue
            return False
    return False


def _touch_lock():
    try:
        os.utime(LOCK_PATH, None)
    except OSError:
        pass


def _release_lock():
    try:
        os.replace(LOCK_PATH, LOCK_PATH + ".done")
    except OSError:
        pass


def _exec(kind, cmd, cwd, st, state_key, log_mode="w"):
    """수집기 하나를 실행하며 5초마다 상태·자물쇠 갱신. → 종료코드."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log = os.path.join(LOG_DIR, f"collect_{kind}.log")
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    with open(log, log_mode, encoding="utf-8", errors="replace") as lf:
        lf.write(f"[collect_aux] {kind} {datetime.now():%Y-%m-%d %H:%M:%S}\n$ {' '.join(cmd)}\n")
        lf.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while proc.poll() is None:
            time.sleep(5)
            try:                     # 상태 갱신이 실패해도 수집기를 고아로 두고 빠져나가지 않는다
                st.update(tail=_tail(log), elapsed_sec=int(time.time() - t0))
                _write_state(state_key, st)
                _touch_lock()
            except Exception:
                pass
    st["tail"] = _tail(log, 10)
    return proc.returncode


def _refresh_coverage(st):
    try:
        import period_backtest as P
        st["coverage"] = P.aux_coverage(P.trading_calendar(), force=True)
    except Exception as e:
        st["cov_error"] = f"{type(e).__name__}: {str(e)[:120]}"


def run(kind, start, end):
    """종류 하나 수집 → 데이터로 검증 → 상태 파일 + 텔레그램(완료/미완료 구간)."""
    spec = KINDS[kind]
    cmd, cwd = spec["cmd"](start, end)
    st = {"kind": kind, "label": spec["label"], "start": start, "end": end, "status": "running",
          "pid": os.getpid(), "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tail": []}
    _write_state(kind, st)
    t0 = time.time()
    rc = _exec(kind, cmd, cwd, st, kind)
    invalidate_caches(kind)
    _refresh_coverage(st)
    v = _verdict(kind, start, end, rc)
    st.update(status="done" if rc == 0 else "error", rc=rc, verdict=v,
              elapsed_sec=int(time.time() - t0), finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _write_state(kind, st)
    _notify(f"📥 [데이터 수집] {spec['label']} 끝 ({st['elapsed_sec'] // 60}분)\n{step_line(v)}"
            + (f"\n로그: logs/collect_{kind}.log" if v["status"] != "done" else ""))
    return rc


# ── 부족분 한 번에 (대시보드 [부족분 한 번에 수집] · collect_all.bat) ─────────────
CREDIT_SINCE = "20210101"        # 신용 백필 시작(사용자 결정 2026-10-18). 키움이 더 과거를 주면 당겨도 됨
CREDIT_PROBE_N = 20              # 본 수집 전 앞 20종목으로 '2022년 이전 제공 여부' 시험


def _market_hours(now=None):
    """키움 매매와 키를 다투는 시간(거래일 08:30~16:00) — 신용 단계는 이때 건너뛴다."""
    from market_calendar import is_trading_day
    now = now or datetime.now()
    return is_trading_day(now) and "0830" <= now.strftime("%H%M") < "1600"


def plan_all(cal, now=None):
    """[(kind, start, end)] — 최근 1~3거래일은 공표 지연이 정상이라 검증 대상에서 뺀다."""
    first = cal[0]
    prev = cal[-2] if len(cal) > 1 else cal[-1]          # 오늘/어제분은 다음 자동수집이 채움
    credit_from = next((d for d in cal if d >= CREDIT_SINCE), first)
    credit_to = cal[-3] if len(cal) > 3 else cal[-1]      # 신용은 결제일 기준이라 2일 늦게 나옴
    return [("flow", cal[-1], cal[-1]), ("individual", first, prev),
            ("short", first, prev), ("credit", credit_from, credit_to)]


def _credit_pre2022_rows():
    """2022년 이전 신용 행 수 — 0 이면 키움이 그 이전을 주는지 아직 확인 안 된 상태."""
    import sqlite3
    from fin_paths import STOCK_DB
    con = sqlite3.connect(f"file:{STOCK_DB}?mode=ro", uri=True, timeout=60)
    try:
        return con.execute("SELECT count(*) FROM credit_balance WHERE date < '2022-01-01'").fetchone()[0]
    finally:
        con.close()


def run_all(now=None):
    t_all = time.time()
    cal = _cal()
    plan = plan_all(cal, now)
    st = {"kind": "all", "label": "부족분 한 번에", "status": "running", "pid": os.getpid(),
          "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          "steps": [{"kind": k, "label": KINDS[k]["label"], "status": "pending", "start": s, "end": e}
                    for k, s, e in plan], "tail": []}
    _write_state("all", st)
    _notify("📥 [데이터 일괄 수집] 시작 — " + " → ".join(KINDS[k]["label"] for k, _, _ in plan)
            + "\n(빈 날만 받음 · 신용은 수 시간 걸릴 수 있음 · 끝나면 완료/미완료 보고)")
    results = []
    for i, (kind, s, e) in enumerate(plan):
        step = st["steps"][i]
        step["status"] = "running"
        st["current"] = KINDS[kind]["label"]
        _write_state("all", st)
        note, rc = "", 0
        try:
            if kind == "credit" and _market_hours(now):
                v = {"kind": kind, "label": KINDS[kind]["label"], "start": s, "end": e, "rc": None,
                     "status": "skipped", "missing_n": None,
                     "note": "장중(08:30~16:00)이라 건너뜀 — 키움 매매와 키를 다툼. 16시 이후 다시 실행"}
                results.append(v)
                step.update(v)
                _write_state("all", st)
                continue
            cmd, cwd = KINDS[kind]["cmd"](s, e)
            if kind == "credit":
                # 키움이 2022년 이전 신용을 주는지 아직 모르면(그 이전 행 0건) 앞 20종목으로 먼저 시험
                if s < "20220101" and _credit_pre2022_rows() == 0:
                    rc = _exec(kind, cmd + ["--limit", str(CREDIT_PROBE_N)], cwd, st, "all")
                    if rc != 0 or _credit_pre2022_rows() == 0:
                        v = _verdict(kind, s, e, rc)
                        v["note"] = (f"키움 시험 수집 실패(rc={rc}) — logs/collect_credit.log 확인" if rc != 0 else
                                     f"키움 ka10013 시험({CREDIT_PROBE_N}종목)에서 2022년 이전 행 0건 — 키움이 그 이전을 "
                                     "주지 않는 것으로 보고 본 수집 생략. 대안: KIS '국내주식 신용잔고 일별추이'(FHPST04760000)")
                        results.append(v)
                        step.update(v)
                        _write_state("all", st)
                        continue
                    note = "키움 2022년 이전 제공 확인(시험 통과)"
                rc = _exec(kind, cmd, cwd, st, "all", log_mode="a")
            else:
                rc = _exec(kind, cmd, cwd, st, "all")
            invalidate_caches(kind)
            v = _verdict(kind, s, e, rc, note=note)
        except Exception as ex:
            v = {"kind": kind, "label": KINDS[kind]["label"], "start": s, "end": e, "rc": None,
                 "status": "failed", "missing_n": None, "note": f"{type(ex).__name__}: {str(ex)[:120]}"}
        results.append(v)
        step.update(v)
        _write_state("all", st)
    _refresh_coverage(st)
    cnt = {k: sum(1 for v in results if v["status"] == k) for k in _ICON}
    mins = int(time.time() - t_all) // 60
    head = (f"📥 [데이터 일괄 수집] 끝 ({mins}분) — 완료 {cnt['done']} · 일부 미완료 {cnt['partial']}"
            f" · 실패 {cnt['failed']}" + (f" · 건너뜀 {cnt['skipped']}" if cnt["skipped"] else ""))
    _notify(head + "\n" + "\n".join(step_line(v) for v in results)
            + ("\n로그: logs/collect_*.log · 대시보드 백테스트 탭 [데이터 현황]"
               if cnt["partial"] or cnt["failed"] else ""))
    st.update(status="done", current="", summary=head, elapsed_sec=int(time.time() - t_all),
              finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _write_state("all", st)
    return 0 if not cnt["failed"] else 1


def main():
    os.chdir(HERE)
    sys.path.insert(0, HERE)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=sorted(KINDS) + ["all"])
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    a = ap.parse_args()
    if a.kind != "all":
        if not (re.fullmatch(r"\d{8}", a.start or "") and re.fullmatch(r"\d{8}", a.end or "")) \
                or a.start > a.end:
            sys.exit("날짜는 YYYYMMDD, 시작 ≤ 종료")
    if not _acquire_lock():
        print("[collect_aux] 다른 수집이 실행 중입니다 — 끝난 뒤 다시 실행하세요")
        _notify("⏸ [데이터 수집] 다른 수집이 이미 실행 중이라 이번 요청은 시작하지 않았습니다")
        sys.exit(9)
    try:
        rc = run_all() if a.kind == "all" else run(a.kind, a.start, a.end)
    except Exception as e:
        _write_state(a.kind, {"kind": a.kind, "status": "error", "start": a.start, "end": a.end,
                              "error": f"{type(e).__name__}: {str(e)[:200]}",
                              "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        _notify(f"🚨 [데이터 수집] {a.kind} 실행 실패 — {type(e).__name__}: {str(e)[:120]}")
        raise
    finally:
        _release_lock()
    sys.exit(rc)


if __name__ == "__main__":
    main()
