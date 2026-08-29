# -*- coding: utf-8 -*-
"""
watchdog.py — '돌았어야 할 시점에 안 돌면' 자동 감지 → 텔레그램 경보.

이 시스템의 #1 약점인 '조용한 실패'(돌아가는 것처럼 보이는데 안 돌아감)를 잡는다.
이번까지 실제로 터진 실패 모드를 그대로 점검 항목으로 만든 것:
  1) 스케줄러 데몬 죽음(상주 루프 누락) → ♥ 하트비트 끊김
  2) 데이터 수집 정지 → stock.db 테이블 정체
  3) 신호 생성 미실행(18:30) → live_signal/kis_signal 오늘 로그 없음
  4) 매매 작업 미실행(09:01) → kis_trader 오늘 로그 없음
  5) 잔고 스냅샷 정지(15:40) → kis/kiwoom 스냅샷 날짜 과거

동작:
  - 평일/시각을 인지해 '지금쯤 됐어야 할 것'만 점검(0건 정상인 날 오탐 방지: 로그 존재 여부로 판정).
  - 문제 발견 시 한 통으로 묶어 텔레그램. 같은 문제는 하루 1회만(스팸 방지).
  - `--daily` 로 하루 1회 실행하면 정상이어도 'OK N/N' 하트비트를 보냄
    → 그게 안 오면 워치독/PC/텔레그램이 죽은 것(침묵 자체가 경보).

실행: python watchdog.py            # 평상시(이상 있을 때만 알림)
      python watchdog.py --daily    # 하루 마감 점검(정상이어도 하트비트 전송)
"""

from __future__ import annotations
import os
import sys
import json
import glob
import sqlite3
from datetime import datetime, date

# 콘솔/리다이렉트가 cp949 여도 죽지 않게 — PYTHONIOENCODING 미설정 경로
# (대시보드 버튼의 Popen 등)에서 '—' 같은 문자로 UnicodeEncodeError 크래시 방지(2026-07-07).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOGS = os.path.normpath(os.path.join(_HERE, "..", "logs"))
_STATE = os.path.join(_HERE, "db", "watchdog_state.json")
_KIWOOM_DIR = os.path.join(_HERE, "db", "kiwoom")
_STOCK_DB_PATHS = [
    os.path.join(_HERE, "..", "Stock_AI_Project", "data", "stock.db"),
    "C:/fin/Stock_AI_Project/data/stock.db",
]

NOW = datetime.now()
TODAY = NOW.date()
TODAY_STR = TODAY.strftime("%Y%m%d")
IS_WEEKDAY = TODAY.weekday() < 5


# ── 유틸 ──────────────────────────────────────────────────────────────────────
def _after(hhmm: str) -> bool:
    """현재 시각이 hhmm(예 '18:35') 이후인가."""
    h, m = map(int, hhmm.split(":"))
    return NOW >= NOW.replace(hour=h, minute=m, second=0, microsecond=0)


_OUT_LOGS = os.path.join(_HERE, "logs")   # 키움 트레이더 등은 outputs\logs 에 기록


def _ran_today(pattern: str) -> bool:
    """C:\\fin\\logs 와 outputs\\logs 에서 pattern 에 맞는 파일 중
    파일명에 오늘 날짜가 든 게 있나(=오늘 실행됨)."""
    for root in (_LOGS, _OUT_LOGS):
        for f in glob.glob(os.path.join(root, pattern)):
            if TODAY_STR in os.path.basename(f):
                return True
    return False


def _open_db():
    for p in _STOCK_DB_PATHS:
        try:
            if os.path.exists(p):
                return sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
        except Exception:
            continue
    return None


def _max_date(con, table, col="date"):
    try:
        r = con.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
        return str(r[0])[:10] if r and r[0] else None
    except Exception:
        return None


def _age_days(datestr):
    if not datestr:
        return 9999
    try:
        fmt = "%Y-%m-%d" if "-" in datestr else "%Y%m%d"
        return (NOW - datetime.strptime(datestr[:10], fmt)).days
    except Exception:
        return 9999


def _age_bdays(datestr):
    """영업일(주말 제외) 기준 나이 — 설·추석 3일 연휴에 달력일 임계(5일)가 초과되어
    매년 정상 상황을 '데이터 정체'로 오경보하던 것 완화(2026-07-12). 공휴일 자체는
    미반영 근사지만 주말 2일 제거만으로 3일 연휴까지 커버. numpy 없으면 달력일 폴백."""
    if not datestr:
        return 9999
    try:
        import numpy as np
        fmt = "%Y-%m-%d" if "-" in datestr else "%Y%m%d"
        d = datetime.strptime(datestr[:10], fmt).date()
        return int(np.busday_count(d, TODAY))
    except Exception:
        return _age_days(datestr)


def _log_contains_today(fname, needle):
    """오늘자 로그 파일(양쪽 로그 루트)에 needle 문자열이 있는가 — 실행 '흔적' 확인용."""
    for root in (_LOGS, _OUT_LOGS):
        p = os.path.join(root, fname)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                if needle in f.read():
                    return True
        except Exception:
            pass
    return False


def _any_log_contains_today(pattern, needle):
    """오늘 날짜가 든 pattern 매칭 로그들(실행별 파일, 예: kis_trader_YYYYMMDD_HHMM.log)
    중 하나라도 needle 을 담고 있는가 — KIS 처럼 실행마다 새 파일을 만드는 잡용."""
    for root in (_LOGS, _OUT_LOGS):
        for p in glob.glob(os.path.join(root, pattern)):
            if TODAY_STR not in os.path.basename(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    if needle in f.read():
                        return True
            except Exception:
                pass
    return False


def _snap_date(fname):
    try:
        d = json.loads(open(os.path.join(_KIWOOM_DIR, fname), encoding="utf-8").read())
        return str(d.get("date", ""))[:10].replace("-", "")
    except Exception:
        return ""


# ── 점검 항목 ─────────────────────────────────────────────────────────────────
def check_scheduler():
    """스케줄러 데몬 생존 — scheduler.log 마지막 ♥ 하트비트가 15분 이내인가."""
    path = os.path.join(_LOGS, "scheduler.log")
    if not os.path.exists(path):
        return ("scheduler", "스케줄러 데몬", False, "scheduler.log 없음 — 데몬 미가동?")
    last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-300:]
        for ln in lines:
            if "alive" in ln and ln.startswith("["):
                try:
                    ts = ln[1:ln.index("]")]
                    last = datetime.strptime(ts.strip()[:19], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
    except Exception as e:
        return ("scheduler", "스케줄러 데몬", False, f"로그 읽기 실패: {e}")
    # 보조 생존신호: scheduler.log 가 최근에 쓰이고 있으면 데몬 활동 중.
    # (scheduler.py 는 단일스레드라 daily_data 같은 긴 수집(06:30~09시 전후) 동안
    #  ♥ 잡이 실행되지 못하고, 뉴스 수집이 수백 줄을 쏟아내 ♥ 가 tail 300줄 밖으로
    #  밀림 → 07:45 점검이 '하트비트 없음' 오탐. 로그 mtime 으로 보완 — 2026-07-02)
    try:
        mtime_min = (NOW - datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 60.0
    except Exception:
        mtime_min = 1e9
    if last is None:
        if mtime_min <= 15:
            return ("scheduler", "스케줄러 데몬", True,
                    f"정상(수집 작업 진행 중 — 로그 활동 {max(0, mtime_min):.0f}분 전, ♥는 작업 후 재개)")
        if _scheduler_process_alive():
            return ("scheduler", "스케줄러 데몬", True,
                    "정상(로그 핸들로 생존 확인 — 긴 수집 자식작업 중, 출력은 완료 후 일괄 기록)")
        return ("scheduler", "스케줄러 데몬", False, "하트비트(♥) 기록 없음")
    age_min = (NOW - last).total_seconds() / 60.0
    if age_min > 15:
        if mtime_min <= 15:
            return ("scheduler", "스케줄러 데몬", True,
                    f"정상(수집 작업 진행 중 — 마지막 ♥ {age_min:.0f}분 전이나 로그 활동 {max(0, mtime_min):.0f}분 전)")
        if _scheduler_process_alive():
            return ("scheduler", "스케줄러 데몬", True,
                    f"정상(로그 핸들로 생존 확인 — 마지막 ♥ {age_min:.0f}분 전, 긴 수집 자식작업 중)")
        return ("scheduler", "스케줄러 데몬", False,
                f"응답 없음 — 마지막 ♥ {age_min:.0f}분 전(데몬 죽었을 수 있음). start_scheduler.bat 확인")
    return ("scheduler", "스케줄러 데몬", True, f"정상(♥ {max(0, age_min):.0f}분 전)")


def _scheduler_process_alive():
    """데몬 생존 3차 신호 — scheduler.log 를 배타모드로 열어보고 '잠김'이면 생존.

    배경: 토요일 갭백필처럼 단일 자식작업이 2시간+ 돌면 스케줄러가 자식 출력을 완료 후
    일괄 기록해 로그 mtime 까지 정체 → ♥/mtime 휴리스틱이 둘 다 오탐(2026-07-04 실제 발생).
    프로세스 커맨드라인 조회는 데몬이 관리자 권한(작업스케줄러 Highest)이라 일반 권한에선
    비어 나옴 → 대신 **데몬(start /min ... >> scheduler.log)이 로그 append 핸들을 상시
    보유**한다는 사실을 이용: 배타 열기가 공유위반으로 실패하면 = 데몬 생존.
    ※ 한계: (a) taillog 등 다른 프로세스가 로그를 잡고 있으면 거짓 생존(경보 억제 방향),
      (b) '떠 있으나 교착'은 못 구분 — 둘 다 다음날 데이터 신선도 점검이 결과물 정체로 잡음."""
    try:
        import subprocess
        log_path = os.path.join(_LOGS, "scheduler.log").replace("\\", "/")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"try{{$f=[IO.File]::Open('{log_path}','Open','ReadWrite','None');"
             f"$f.Close();'FREE'}}catch{{'LOCKED'}}"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace")
        return "LOCKED" in (r.stdout or "")
    except Exception:
        return False


def check_data():
    """데이터 신선도 — stock.db 8종. (주기별 임계: 일일 5~6, 뉴스 6, 신용 10)"""
    con = _open_db()
    if con is None:
        return [("db_open", "stock.db", False, "DB 열기 실패")]
    out = []
    # (table, col, threshold, busday 기준 여부) — 일일 갱신 테이블은 영업일 기준(연휴 오탐 방지)
    # [2026-08-20] korea_indicators 추가. 강도(score_ic) IC 상위 5개 중 3개
    # (bb_pct_db 10.2% / rsi_db 9.4% / macd_hist_db 7.9% = 합계 27.5% 질량)가
    # 오직 이 테이블에서 나오는데 감시 목록에 빠져 있어, 08-14 이후 정체된 것을
    # 아무도 몰랐다. 이 테이블이 멎으면 강도가 통째로 눌려 매수가 조용히 멈춘다.
    # [2026-08-29] foreign_ratio 추가. 매일 수집(daily_data_job)하는데 감시 목록에
    # 없어, 정체돼도 아무도 몰랐다 — korea_indicators 가 빠져 있던 것(08-20)과 같은 구멍.
    # 수집 작업 8개 중 감시되지 않던 유일한 테이블이었다.
    specs = [("korea_stocks", "date", 5, True), ("supply_demand", "date", 5, True),
             ("korea_indicators", "date", 5, True), ("foreign_ratio", "date", 5, True),
             ("usa_stocks", "date", 6, False), ("macro_indicators", "date", 6, False),
             ("credit_balance", "date", 10, False), ("news", "pubDate", 6, False)]
    for tbl, col, thr, use_bday in specs:
        latest = _max_date(con, tbl, col)
        age = _age_bdays(latest) if use_bday else _age_days(latest)
        unit = "영업일" if use_bday else "일"
        ok = age <= thr
        out.append((f"data_{tbl}", f"데이터 {tbl}", ok,
                    f"{age}{unit} 정체(최신 {latest})" if not ok else f"최신 {latest}"))
    con.close()
    return out


def check_signals():
    """신호 생성 — 평일 18:35 이후, 오늘 live_signal/kis_signal 로그가 있나(=작업 실행됨)."""
    out = []
    if IS_WEEKDAY and _after("18:35"):
        out.append(("sig_kiwoom", "키움 신호생성(18:30)", _ran_today("live_signal_*.log"),
                    "오늘 live_signal 미실행"))
        out.append(("sig_kis", "KIS 신호생성(18:31)", _ran_today("kis_signal_*.log"),
                    "오늘 kis_signal 미실행"))
    return out


def check_idle_buying(days=5):
    """[2026-08-09 신설] '연속 N거래일 매수 0건'을 잡는다.

    배경: 8월 초 강도 임계 6.0 이 5거래일 중 4일의 매수를 전부 차단해 계좌가 전액
    현금이 됐는데 **경보가 한 건도 없었다**. 반대로 무해한 스냅샷 지연 같은 건 매번
    시끄럽게 알렸다 — 정작 중요한 상태 변화를 놓치는 전형적 감시 공백이었다.
    (임계가 시장에 비해 높거나, 신호 생성이 조용히 죽었거나, 예산이 0 일 때 걸린다.)

    거래일 달력은 '트레이더 실행 로그'(kiwoom_YYYYMMDD.log — 매 거래일 생성)로 잡는다.
    주문 CSV 는 주문이 있는 날에만 생기므로 그것만 세면 창이 실제보다 길어져 둔감해진다.
    """
    out = []
    if not (IS_WEEKDAY and _after("09:30")):
        return out
    try:
        # 최근 N 거래일(트레이더가 실제로 돈 날)
        logs = sorted(glob.glob(os.path.join(_HERE, "logs", "kiwoom_*.log")))[-days:]
        dates = [os.path.basename(f)[7:15] for f in logs
                 if os.path.basename(f)[7:15].isdigit()]
        if len(dates) < days:
            return out                       # 이력이 짧으면 판정 보류
        bought = 0
        for d in dates:
            p = os.path.join(_KIWOOM_DIR, f"orders_{d}.csv")
            if not os.path.exists(p):
                continue                     # 그날 주문 0건 = 매수도 0건
            try:
                with open(p, encoding="utf-8-sig") as fh:
                    bought += sum(1 for ln in fh if ",buy," in ln)
            except Exception:
                return out                   # 읽기 실패 시 오탐 방지
        out.append(("idle_buying", f"매수 활동(최근 {days}거래일)", bought > 0,
                    f"{dates[0]}~{dates[-1]} 매수 0건 — 강도 임계가 시장 대비 높거나 "
                    f"신호/예산 이상일 수 있음(현금 보유 상태). 의도한 것이면 무시"))
    except Exception:
        pass
    return out


def check_account_blocked(days=3):
    """[2026-08-20 신설] '계좌 자체가 주문을 못 받는' 상태를 잡는다.

    실사고: 08-10 부터 KIS 모의계좌가 msg_cd=40910000 '모의투자 주문이 불가한
    계좌입니다' 로 매수·매도를 전부 거부했다. 종목 단위 주문 실패와 같은 모양의
    '[실패] …' 한 줄만 남고 exit 0 이라 열흘간 감지되지 않았고, 그 사이 만기가
    지난 보유 1종목이 청산되지 못한 채 남았다. 매수 0건(idle_buying)만으로는
    '살 게 없어서'와 '살 수가 없어서'를 구분하지 못하므로 별도 감시가 필요하다.

    판정: 최근 days 일치 트레이더 로그에 계좌 단위 거부 문구가 있으면 이상.
    """
    out = []
    words = ("주문이 불가한 계좌", "사용할 수 없는 계좌", "해지된 계좌",
             "정지된 계좌", "계좌가 없습니다")

    # [2026-08-21] 1순위 증거는 당일 08:50 탐침 결과다. 로그 스캔만 하면 '가장 최근
    # 주문을 시도한 날'(만기가 없으면 며칠 전)이 근거로 잡혀 경보가 낡아 보인다.
    try:
        with open(os.path.join(_HERE, "db", "preopen_probe_result.json"),
                  encoding="utf-8") as f:
            pr = json.load(f)
        blocked = [k for k in ("kiwoom", "kis") if pr.get(k, {}).get("fatal")]
        if blocked:
            out.append(("account_blocked", "계좌 주문 가능 여부", False,
                        f"{', '.join(blocked)} 계좌가 주문을 거부 "
                        f"({pr.get('probed_at','?')} 장전 탐침 실측) — "
                        f"모의투자 계좌 기간만료/재발급 확인 필요. "
                        f"매수·매도·만기청산 전부 불가"))
            return out
    except Exception:
        pass   # 탐침 기록이 없거나 깨졌으면 아래 로그 스캔으로 폴백

    recent = sorted(glob.glob(os.path.join(_LOGS, "kis_trader_*.log"))
                    + glob.glob(os.path.join(_LOGS, "kis_stop_*.log"))
                    + glob.glob(os.path.join(_HERE, "logs", "kiwoom_*.log")),
                    key=os.path.getmtime)[-(days * 4):]
    hits = []
    for p in recent:
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if any(w in txt for w in words):
            hits.append(os.path.basename(p))
    if hits:
        out.append(("account_blocked", "계좌 주문 가능 여부", False,
                    f"주문 거부(계좌 단위) 로그 {len(hits)}건: {', '.join(hits[:3])} — "
                    f"모의투자 계좌 기간만료/재발급 확인 필요. 매수·매도·만기청산 전부 불가"))
    else:
        out.append(("account_blocked", "계좌 주문 가능 여부", True, ""))
    return out


def check_ai_pipeline(max_days=9):
    """[2026-08-09 신설] AI 파이프라인이 '실제로 완주한 지' 며칠 됐나.

    배경: 종전엔 락 스킵을 실패로 보고 매번 경보했는데, 정작 중요한 건 '스킵됐다'가
    아니라 **학습이 오랫동안 완주하지 못하고 있다**는 사실이다. 스킵은 정상 상황
    (정기 실행이 수동 실행과 겹침)에서도 나므로 경보로 쓰면 양치기 소년이 된다.
    → 스킵은 조용히 넘기고(run_ai_pipeline.bat, exit 0), 여기서 '완주 이력'만 본다.
    주간 실행(일요일 03:00)이므로 9일이면 한 주를 통째로 건너뛴 것.
    """
    out = []
    try:
        logs = sorted(glob.glob(os.path.join(_LOGS, "ai_pipeline_*.log")))
        last_done = None
        for f in reversed(logs):                 # 최신 로그부터 'DONE' 을 찾는다
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    if "AI pipeline DONE" in fh.read():
                        base = os.path.basename(f)          # ai_pipeline_YYYYMMDD_HHMM.log
                        last_done = base[12:20]
                        break
            except Exception:
                continue
        if not last_done or not last_done.isdigit():
            return out                            # 이력 없음 — 판정 보류(오탐 방지)
        age = (TODAY - date(int(last_done[:4]), int(last_done[4:6]), int(last_done[6:8]))).days
        out.append(("ai_pipeline", "AI 파이프라인 완주", age <= max_days,
                    f"마지막 완주 {last_done} ({age}일 전) — 주간 학습이 건너뛰어지고 있음"))
    except Exception:
        pass
    return out


def check_trading():
    """매매 작업 — 평일 09:10 이후, 오늘 kis/kiwoom 트레이더 로그가 있나.

    [2026-07-07] 키움 추가 — 기존엔 KIS 만 감시해 키움 매매(09:03)가 조용히
    죽어도 못 잡았음. 키움 로그는 outputs\\logs\\kiwoom_YYYYMMDD.log."""
    out = []
    if IS_WEEKDAY and _after("09:10"):
        out.append(("trade_kis", "KIS 매매(09:01)", _ran_today("kis_trader_*.log"),
                    "오늘 kis_trader 미실행"))
        out.append(("trade_kiwoom", "키움 매매(09:03)", _ran_today(f"kiwoom_{TODAY_STR}.log"),
                    "오늘 kiwoom_trader 미실행"))
    if IS_WEEKDAY and _after("15:35"):
        # [2026-07-12] 로그 '파일 존재'는 09:03 매수 실행이 이미 만족시킴 — 15:21 매도
        # (만기+서킷브레이커, 키움의 유일한 청산 경로)가 죽어도 무경보이던 사각지대.
        # 오후 실행이 남기는 '오후 모드' 마커로 실행 여부를 직접 확인.
        # [2026-07-17] '시작 마커'→'완료 마커' 전환 — KIS PM 이 시작 직후 ReadTimeout 으로
        # 죽었는데 시작 마커가 이미 찍혀 [OK] 로 통과한 실사례. 끝까지 완주해야 찍히는
        # 완료 마커(트레이더가 마지막에 print)로 크래시도 잡는다.
        out.append(("trade_kiwoom_pm", "키움 매도(15:21)",
                    _log_contains_today(f"kiwoom_{TODAY_STR}.log", "오후 모드 완료"),
                    "오늘 15:21 매도(만기/서킷브레이커) 완주 흔적 없음(미실행 또는 도중 크래시)"))
        # [2026-07-17] KIS 만기매도 PM 트리거 감시 — KisTraderPM(15:21, daily-pm) 재등록
        # 확인 후 활성화(재등록 전에 넣으면 매일 오탐이라 보류했던 항목).
        # KIS 로그는 실행별 파일(kis_trader_YYYYMMDD_HHMM.log)이라 글롭 검색.
        out.append(("trade_kis_pm", "KIS 만기매도(15:21)",
                    _any_log_contains_today("kis_trader_*.log", "[daily-pm] 완료"),
                    "오늘 15:21 만기매도(daily-pm) 완주 흔적 없음(미실행 또는 도중 크래시)"))
    return out


def check_snapshots():
    """잔고 스냅샷 — 평일 15:45 이후, KIS/키움 스냅샷 날짜가 오늘인가(=status 갱신됨).

    [2026-07-07] 키움 snapshot.json 추가 — 15:40 run_kis_status.bat 가 키움 status 도
    함께 갱신하므로 같은 시각 기준으로 점검(기존엔 KIS 만 감시)."""
    out = []
    if IS_WEEKDAY and _after("15:45"):
        out.append(("snap_kis", "KIS 잔고 스냅샷(15:40)", _snap_date("kis_snapshot.json") == TODAY_STR,
                    f"오늘 미갱신(최신 {_snap_date('kis_snapshot.json') or '없음'})"))
        out.append(("snap_kiwoom", "키움 잔고 스냅샷(15:40)", _snap_date("snapshot.json") == TODAY_STR,
                    f"오늘 미갱신(최신 {_snap_date('snapshot.json') or '없음'})"))
    return out



def check_kill_switch(results):
    """[2026-08-27] 3층 안전장치 — 사고 조건이면 신규매수를 실제로 **정지**시킨다.

    종전 watchdog 은 알리기만 했다. 2026-08 에 bat 손상 3일 무실행 / KIS 계좌
    12일 주문불가 / 청산 반복 실패가 전부 '경보는 갔지만 매매는 계속'이었다.
    여기서 다루는 세 조건은 임계 최적화가 필요 없다 — 발생 자체가 사고다.

    results: run_all_checks() 결과. 이미 판정된 항목을 재활용해 이중 판정을 피한다.
    반환: watchdog 리포트에 붙일 (key, label, ok, detail) 리스트.
    """
    out = []
    try:
        import kill_switch as ks
    except Exception as e:
        return [("kill_switch", "정지 스위치", False, f"모듈 로드 실패: {str(e)[:60]}")]

    by = {k: (ok, det) for k, _lbl, ok, det in results}

    trip = None
    # ① 계좌가 주문을 거부 — check_account_blocked 결과 재사용
    if by.get("account_blocked", (True, ""))[0] is False:
        trip = ("order_blocked", by["account_blocked"][1][:200])
    # ② 트레이더 무실행 — 오늘 매매 시각이 지났는데 실행 흔적이 없음
    elif by.get("trade_kis", (True, ""))[0] is False and _no_run_days("kis_trader_*.log") >= 2:
        trip = ("no_run", f"KIS 트레이더 {_no_run_days('kis_trader_*.log')}거래일 연속 무실행")
    elif by.get("trade_kiwoom", (True, ""))[0] is False and _no_run_days("kiwoom_*.log") >= 2:
        trip = ("no_run", f"키움 트레이더 {_no_run_days('kiwoom_*.log')}거래일 연속 무실행")
    # ③ 청산 실패 반복 — 최근 로그에서 청산 실패 문구가 2일 이상
    else:
        n = _exit_fail_days()
        if n >= 2:
            trip = ("exit_failed", f"만기/손절 청산 실패가 {n}거래일에서 관측됨")

    on, st, err = ks.status()
    if trip and not on:
        ks.engage(trip[0], trip[1])
        out.append(("kill_switch", "정지 스위치", False,
                    f"신규매수 정지 발동 — {trip[1]}"))
    elif on:
        out.append(("kill_switch", "정지 스위치", False,
                    f"정지 중({st.get('reason_text','?')}, {st.get('engaged_at','?')}) — "
                    f"원인 확인 후 kill_switch.py release"))
    elif err:
        out.append(("kill_switch", "정지 스위치", False, f"상태 읽기 실패: {err}"))
    else:
        out.append(("kill_switch", "정지 스위치", True, ""))
    return out


def _no_run_days(pattern, look=6):
    """최근 거래일 중 해당 로그가 없는 연속 일수(오늘부터 거슬러)."""
    import re as _re
    days = sorted({_re.search(r"(\d{8})", os.path.basename(p)).group(1)
                   for root in (_LOGS, _OUT_LOGS)
                   for p in glob.glob(os.path.join(root, pattern))
                   if _re.search(r"(\d{8})", os.path.basename(p))})
    if not days:
        return 0
    # 달력 평일 기준으로 거슬러 올라가며 로그 없는 날을 센다
    from datetime import timedelta
    have = set(days)
    n = 0
    d = TODAY
    for _ in range(look):
        if d.weekday() < 5:
            if d.strftime("%Y%m%d") in have:
                break
            n += 1
        d -= timedelta(days=1)
    return n


def _exit_fail_days(look=4):
    """최근 로그에서 '청산 실패'가 관측된 거래일 수."""
    words = ("청산 실패", "만기/손절 청산", "[실패]")
    days = set()
    for root in (_LOGS, _OUT_LOGS):
        for p in sorted(glob.glob(os.path.join(root, "*.log")),
                        key=os.path.getmtime)[-40:]:
            base = os.path.basename(p)
            if not ("kis_trader" in base or "kiwoom_" in base):
                continue
            try:
                t = open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            if any(w in t for w in words) and "매도 오류" in t:
                import re as _re
                m = _re.search(r"(\d{8})", base)
                if m:
                    days.add(m.group(1))
    return len(days)


def run_all_checks():
    results = []
    results.append(check_scheduler())
    results += check_data()
    results += check_signals()
    results += check_trading()
    results += check_snapshots()
    results += check_idle_buying()     # 연속 매수 0건(2026-08-09 신설)
    results += check_account_blocked() # 계좌 자체 주문불가(2026-08-20 신설)
    results += check_ai_pipeline()     # 주간 학습 완주 이력(2026-08-09 신설)
    # 3층 안전장치 — 위 판정 결과를 받아 '사고'면 신규매수를 실제로 정지시킨다.
    # (알리기만 하던 종전 동작의 공백. 2026-08-27 신설)
    results += check_kill_switch(results)
    return results


# ── 상태(중복 알림 방지) ──────────────────────────────────────────────────────
def _load_state():
    try:
        s = json.loads(open(_STATE, encoding="utf-8").read())
        if s.get("date") == TODAY.isoformat():
            return s
    except Exception:
        pass
    return {"date": TODAY.isoformat(), "alerted": []}


def _save_state(s):
    try:
        os.makedirs(os.path.dirname(_STATE), exist_ok=True)
        with open(_STATE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
    except Exception:
        pass


def main():
    daily = "--daily" in sys.argv
    results = run_all_checks()
    fails = [(k, lbl, det) for (k, lbl, ok, det) in results if not ok]
    n_total, n_ok = len(results), len(results) - len(fails)

    state = _load_state()
    new_fails = [(k, lbl, det) for (k, lbl, det) in fails if k not in state["alerted"]]

    try:
        import notifier
    except Exception as e:
        print(f"[watchdog] notifier 임포트 실패: {e}")
        notifier = None

    msg = None
    if daily:
        # 하루 마감 하트비트 — 정상이어도 보냄(침묵=경보 원리)
        if fails:
            body = "\n".join(f"• {lbl}: {det}" for _, lbl, det in fails)
            msg = f"⚠️ [감시] 마감 점검 — 이상 {len(fails)}건 (정상 {n_ok}/{n_total})\n{body}\n(미해결 항목 점검 요망)"
        else:
            msg = f"✅ [감시] 오늘 점검 정상 ({n_ok}/{n_total} 통과) — {TODAY.isoformat()}"
        # 마감 땐 모든 현재 이상을 alerted 처리
        state["alerted"] = sorted(set(state["alerted"]) | {k for k, _, _ in fails})
    elif new_fails:
        body = "\n".join(f"• {lbl}: {det}" for _, lbl, det in new_fails)
        msg = f"⚠️ [감시] 점검 이상 {len(new_fails)}건 ({NOW.strftime('%m/%d %H:%M')})\n{body}"
        state["alerted"] = sorted(set(state["alerted"]) | {k for k, _, _ in new_fails})

    # 콘솔 출력(수동 점검/로그용)
    print(f"[watchdog] {NOW.strftime('%Y-%m-%d %H:%M')}  정상 {n_ok}/{n_total}"
          + (f", 이상 {len(fails)}건" if fails else ""))
    for k, lbl, ok, det in results:
        # OK 행에 실패 문구(det)를 그대로 찍으면 "[OK] ... 미실행" 같은 모순 출력이
        # 됨(2026-07-17 판독 혼선 실사례) — 정상 행은 '정상'으로만.
        print(f"  [OK] {lbl}: 정상" if ok else f"  [XX] {lbl}: {det}")

    if msg and notifier is not None:
        sent = notifier.safe_send(msg) if hasattr(notifier, "safe_send") else notifier.send(msg)
        print(f"[watchdog] 텔레그램 전송: {sent}")
    elif msg:
        print(f"[watchdog] (notifier 없음) 미전송 메시지:\n{msg}")
    else:
        print("[watchdog] 신규 이상 없음 — 알림 생략")

    _save_state(state)


if __name__ == "__main__":
    main()
