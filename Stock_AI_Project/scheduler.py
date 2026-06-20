import schedule
import time
import os
import subprocess
import sys
import importlib.util
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()


def _news_module_available():
    """src.processor.news 가 의존하는 transformers 가 설치되어 있는지 점검.
    최소설치(transformers 미설치) 환경에선 뉴스 작업을 건너뛰어 로그 폭발을 막는다.
    """
    return importlib.util.find_spec('transformers') is not None

_RED    = '\033[91m'
_YELLOW = '\033[33m'
_GREEN  = '\033[92m'
_RESET  = '\033[0m'

# Windows ANSI 활성화
if sys.platform == 'win32':
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

# stdout/stderr 를 UTF-8 로 강제 — 로그 리다이렉트(scheduler.log) 시 한글이 cp949 로 깨지는 것 방지 (2026-06-20).
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
# 자식 프로세스(main_collector/macro/news 등 _run_subprocess)도 UTF-8 로 출력하도록 환경변수 전파.
# → 수집기들이 scheduler.log 에 찍는 한글도 안 깨진다.
os.environ["PYTHONIOENCODING"] = "utf-8"

_FAIL_LOG = os.path.join(os.path.dirname(__file__), 'logs', 'failures.log')
os.makedirs(os.path.dirname(_FAIL_LOG), exist_ok=True)


def log(msg, level='INFO'):
    """컬러 콘솔 + 파일 동시 기록."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{now}] {msg}"

    if level == 'ERROR':
        print(f"{_RED}{line}{_RESET}", flush=True)
        with open(_FAIL_LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    elif level == 'WARNING':
        print(f"{_YELLOW}{line}{_RESET}", flush=True)
    elif level == 'OK':
        print(f"{_GREEN}{line}{_RESET}", flush=True)
    else:
        print(line, flush=True)


def _run_subprocess(label, cmd, capture_stderr=True):
    """subprocess 실행 + 실패 시 ERROR 로그 + failures.log 기록."""
    log(f"▶ 시작: {label}")
    result = subprocess.run(
        cmd, check=False,
        stderr=subprocess.PIPE if capture_stderr else None,
        text=True
    )
    if result.returncode != 0:
        err_preview = ''
        if capture_stderr and result.stderr:
            # 마지막 5줄만 요약
            lines = [l for l in result.stderr.strip().splitlines() if l.strip()]
            err_preview = ' | '.join(lines[-5:])
        log(f"✗ 실패: {label} (exit={result.returncode}) {err_preview}", level='ERROR')
        return False
    log(f"✔ 완료: {label}", level='OK')
    return True


def run_pipeline(step, market='korea'):
    """run_pipeline.py 실행. scan step 은 market 인수를 전달한다."""
    cmd = [sys.executable, "run_pipeline.py", "--step", step]
    if step == 'scan':
        cmd += ['--market', market]
    return _run_subprocess(
        f"pipeline --step {step} --market {market}",
        cmd
    )

def daily_data_job():
    """매일 06:30 - 데이터 수집 + (가능하면) 뉴스 수집 + KRX 수급 증분

    [2026-06-14] 트레이딩 시스템(C:/fin/outputs/)과 역할 분리:
      - KOSDAQ OHLCV : run_pipeline('collect') 가 수집하되, 트레이딩 시스템(pykrx, 16:05)이
        stock.db korea_stocks 에 INSERT 완료한 날짜는 main_collector 가 자동 스킵.
        → KOSPI + 미국 주식은 Stock_AI_Project 단독 담당 (트레이딩 시스템 수집 범위 외).
      - 외국인/기관 수급 : KIS API(30일 제한, 매일 1일치 누적) 는 트레이딩 시스템 pykrx 와
        출처가 달라 완전 중복이 아님 — 교차 검증용으로 유지.
      - DART 공시 : 30분마다 감성분석(여기) vs 19:00 raw 목록(트레이딩 시스템) — 역할 다름, 유지.
    """
    log("===== 일일 데이터 수집 시작 =====")
    run_pipeline('collect')

    # KIS API '투자자별 매매동향' 으로 외국인/기관 일별 순매수 누적.
    # INSERT OR IGNORE 라 매일 호출해도 중복 무해, 어제 1일치만 새로 들어감.
    _run_subprocess("KRX 수급(KIS) 수집",
                    [sys.executable, "-m", "src.collector.supply_demand"])

    # 키움 수집은 여기서 하지 않는다 (2026-06-12 변경).
    # 키움 API 키를 다른 시스템과 공유 중이라 주중 사용 시 토큰/한도 충돌 우려
    # → 토요일 08:00 kiwoom_weekly_job 에서 주 1회 일괄 수집.

    if _news_module_available():
        # 'historical all' = 전 섹터 공시 수집 (sec 없으면 ACTIVE_SECTOR 하나만 받던 버그 수정,
        #  2026-06-20). news 가 증분이라 전 섹터도 빠름. 30분 realtime 잡은 주도섹터만 받음(빈도↑).
        _run_subprocess("뉴스/공시 수집 (전체 섹터)",
                        [sys.executable, "-m", "src.processor.news", "historical", "all"])
    else:
        log("transformers 미설치 - 뉴스/공시 수집 스킵", level='WARNING')
    log("===== 일일 데이터 수집 완료 =====")

def daily_macro_job():
    """매일 06:40 - 거시지표 수집 (NASDAQ/VIX/SOX/환율).

    미국장 마감(16:00 ET ≈ KST 05~06시) 데이터를 FinanceDataReader 로 받아
    stock.db macro_indicators 에 INSERT OR REPLACE(자가치유) 저장한다.
    main_collector(OHLCV) 가 macro 를 다루지 않아 별도 수집이 필요하다
    (2026-06-19 추가 — 이전엔 스케줄 누락으로 macro 가 정체됐었음).
    06:30 OHLCV 직후·06:50 스캔 직전. FinanceDataReader 는 키움/KIS 와 다른 소스라 토큰 충돌 없음.
    macro 피처(vix/usdkrw/sox)는 live_signal(18:30)·주간학습이 사용하므로 아침에 갱신 완료해야 함."""
    log("===== 거시지표 수집 시작 =====")
    _run_subprocess("거시지표(NASDAQ/VIX/환율) 수집",
                    [sys.executable, "-m", "src.collector.macro"])
    log("===== 거시지표 수집 완료 =====")

def morning_scan_job():
    """매일 06:50 - 섹터 스캔"""
    log("===== 모닝 섹터 스캔 시작 =====")
    run_pipeline('scan')
    log("===== 모닝 섹터 스캔 완료 =====")

def _is_weekday():
    """주말(토/일) 가드. schedule.every().day 는 주말에도 실행되므로
    매매/모니터 작업은 평일에만 동작하도록 차단한다."""
    return datetime.now().weekday() < 5

def korea_market_scanner_job():
    """매일 09:00 - 거래량/등락률 상위 종목 분당 폴링 (live_movers 누적, 15:30 자동 종료).
    매수에는 미연결, 데이터 누적만."""
    if not _is_weekday():
        return
    log("===== 한국 시장 스캐너 시작 =====")
    subprocess.Popen(
        [sys.executable, "-m", "src.trader.market_scanner",
         "--interval", "60", "--count", "20"]
    )

def usa_scan_job():
    """매일 22:20 - 미국장 전 섹터 스캔 (USA 시장 대상)"""
    log("===== 미국장 전 섹터 스캔 =====")
    run_pipeline('scan', market='usa')

def dart_realtime_job():
    """30분마다 DART 공시 수집 + 감성분석.
    transformers 미설치 환경에서는 무동작(스킵). 로그 노이즈 방지."""
    if not _news_module_available():
        return
    _run_subprocess("DART 실시간 공시",
                    [sys.executable, "-m", "src.processor.news", "realtime"])

def kiwoom_weekly_job():
    """매주 토요일 08:00 - 키움 신용/대차 주간 수집 (공백 감지 기반).

    키움 API 키를 다른 시스템과 공유 중이라 주중에는 키움 키를 쓰지 않는다
    (토큰 재발급이 상대 토큰을 무효화하거나 호출 한도를 나눠 먹는 충돌 방지).

    동작: 최근 30일 윈도우에서 거래일 대비 빠진 날짜가 있는 종목만,
    그 공백 구간만 호출 (공백 없으면 API 호출 0회).
    30일 윈도우라 토요일에 PC 가 꺼져 한 주를 건너뛰어도
    다음 주에 자동으로 메워진다 (자가치유)."""
    since = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    log(f"===== 키움 주간 데이터 수집 시작 (since={since}) =====")
    _run_subprocess("키움 신용/대차 수집",
                    [sys.executable, "-m", "src.collector.kiwoom_extra", "--since", since])


def weekend_audit_job():
    """매주 토요일 07:00 - 데이터 갭 감지 + 자동 백필.
    일요일 07:00 weekly_job 이 깨끗한 데이터로 재학습할 수 있게 미리 정리.
    실행 시간: 1~3시간 (갭이 적으면 빠름)."""
    _run_subprocess("주말 데이터 갭 감지 + 백필",
                    [sys.executable, "-m", "src.collector.weekend_audit"])


def monthly_sector_update_job():
    """매월 1일 06:00 - 네이버 업종 분류 갱신.

    네이버 금융은 분기 리밸런싱(3/6/9/12월) 시즌과 테마 편입/편출 등으로
    업종 분류를 수시로 바꾼다. 월 1회 갱신으로 2차전지/방산 등 테마 변동을 반영.
    갱신 후 korea_stocks.sector 도 자동 교정 (krx_sector.py 내부 처리).
    """
    today = datetime.now()
    if today.day != 1:
        return   # 매월 1일에만 실행 (schedule 라이브러리가 daily 로 호출)
    _run_subprocess("월간 섹터 분류 갱신",
                    [sys.executable, "-m", "src.collector.krx_sector"])


def weekly_job():
    """매주 일요일 07:00 - 전체 재학습"""
    log("===== 주간 전체 재학습 시작 =====")
    run_pipeline('indicators')
    run_pipeline('train')
    run_pipeline('backtest')
    log("===== 주간 전체 재학습 완료 =====")


def heartbeat_job():
    """5분마다 '살아있음' + 다음 작업 표시.
    콘솔에 침묵이 길어지면 사용자가 죽었는지 헷갈리는 걸 방지.
    또한 헬스체크 기능도 겸함 - 이 함수가 안 찍히면 scheduler 가 멈춤."""
    next_job = None
    next_time = None
    for job in schedule.jobs:
        # heartbeat 본인은 제외 (자기 자신을 next 로 표시하지 않게)
        if job.job_func.__name__ == 'heartbeat_job':
            continue
        if job.next_run and (next_time is None or job.next_run < next_time):
            next_time = job.next_run
            next_job = job.job_func.__name__

    if next_time:
        delta = next_time - datetime.now()
        total_sec = max(int(delta.total_seconds()), 0)
        hours, rem = divmod(total_sec, 3600)
        mins = rem // 60
        log(f"♥ alive | next: {next_job} at {next_time.strftime('%m-%d %H:%M')} "
            f"({hours}h {mins}m later)")
    else:
        log("♥ alive (no upcoming jobs)")

# 스케줄 등록
schedule.every().day.at("06:30").do(daily_data_job)               # 데이터 수집 (OHLCV+수급+뉴스)
schedule.every().day.at("06:40").do(daily_macro_job)             # 거시지표 (NASDAQ/VIX/환율) — OHLCV 직후
schedule.every().day.at("06:50").do(morning_scan_job)             # 섹터 스캔
# [2026-06-14] 역할 분리 / [2026-06-19] 트레이딩 모듈 제거:
# 모든 매매·장중모니터는 천억이(outputs)가 단독 담당. Stock_AI_Project 는 데이터 수집/학습 전용.
schedule.every().day.at("09:00").do(korea_market_scanner_job)     # 한국 시장 스캐너 (데이터 수집)
schedule.every().day.at("22:20").do(usa_scan_job)                 # 미국 스캔 (데이터 수집)
schedule.every(30).minutes.do(dart_realtime_job)                  # DART 30분마다
schedule.every().saturday.at("07:00").do(weekend_audit_job)       # 주말 갭 백필
schedule.every().saturday.at("08:00").do(kiwoom_weekly_job)       # 키움 주간 수집 (신용/대차)
schedule.every().sunday.at("07:00").do(weekly_job)                # 주간 재학습
schedule.every().day.at("06:00").do(monthly_sector_update_job)    # 월간 섹터 분류 갱신 (매월 1일만 실제 실행)
schedule.every(5).minutes.do(heartbeat_job)                       # 살아있음 + 다음 작업

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == 'all':
        log("전체 파이프라인 즉시 실행")
        run_pipeline('all')

    elif len(sys.argv) > 1 and sys.argv[1] == 'dart':
        log("DART 공시 즉시 수집")
        subprocess.run(
            [sys.executable, "-m", "src.processor.news", "realtime"],
            check=False
        )

    else:
        log("스케줄러 시작 (데이터 수집 전용 모드)")
        log("06:30 daily_data_job  - OHLCV + 수급(KIS) + 뉴스")
        log("06:40 daily_macro_job  - 거시지표 (NASDAQ/VIX/환율)")
        log("06:50 morning_scan_job - 섹터 스캔")
        log("09:00 korea_market_scanner_job - 한국 시장 스캐너 (데이터 누적)")
        log("22:20 usa_scan_job - 미국장 전 스캔")
        log("30min dart_realtime_job - DART realtime disclosure")
        log("Sat 07:00 weekend_audit_job - gap backfill")
        log("Sat 08:00 kiwoom_weekly_job - kiwoom credit/program weekly")
        log("Sun 07:00 weekly_job - full retrain")
        log("[disabled] trading/monitor jobs handled by kiwoom_trader.py")
