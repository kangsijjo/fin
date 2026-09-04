"""
한국투자증권 모의투자 자동 집행기 — v2: 안D 포트폴리오 + 전략별 stop-loss

인증: .env 의 KIS_MOCK_APP_KEY / KIS_MOCK_APP_SECRET / KIS_MOCK_ACCOUNT

명령:
  python kis_trader.py status   # 예수금·잔고·슬롯 현황 출력
  python kis_trader.py buy      # 전략별 우선순위로 신호 종목 매수
  python kis_trader.py sell     # 만기 or stop 발동 종목 매도
  python kis_trader.py daily    # sell → buy → status 일괄 (스케줄러용)

전략별 슬롯 구조 (총 10) — 안D 포트폴리오 (2026-06-17):
  슬롯 1-4  →  h52w_for3d_mkt  (52주신고가+외국인3일+시장강세, 20일)  stop -15%
  슬롯 5-8  →  for_high20_mkt  (20일신고가+외국인3일+시장강세,  20일)  stop -10%
  슬롯 9-10 →  gc_for3d        (골든크로스20/60+외국인3일,       15일)  stop -26%

  신호 파일: kis_paper_signals.csv (kis_live_signal.py 생성)
  키움(안C)과 신호 파일 분리 → 두 시스템 독립 운용

운용 룰:
  - 매수: 신호 다음 영업일 09:01 시장가. 전략별 빈 슬롯 확인 후 배정.
  - 매도 조건 (둘 중 먼저 발동, 둘 다 09:01 daily 에서 실행):
      1. 만기: 진입일 포함 holding_days 영업일째 09:01 시장가(시가)
         ※ 백테스트 가정은 '종가' 청산 — 15:21 sell 트리거 추가 전까지의 실동작(2026-07-07 정정)
      2. stop: 전일 종가 ≤ 진입가 × (1 + stop_pct) 이면 당일 09:01 시장가
  - 진입가 추적: db/kis/kis_positions.csv (매수 체결 시 기록)
  - 멱등: db/kiwoom/orders_*.csv 로 중복 주문 방지

⚠️ 안전장치: 이 스크립트는 항상 모의투자(openapivts) URL만 사용한다.
   실전 계좌 주문은 절대 실행되지 않는다.

KIS 모의 TR ID:
  VTTC0802U  매수주문
  VTTC0801U  매도주문
  VTTC8434R  잔고조회
"""

import os
from fin_paths import KIWOOM_LOCK as _FIN_KIWOOM_LOCK   # 절대경로 단일 출처(2026-09-04)
import re as _re
import sys
import csv
import time
import json
import glob as _glob
from contextlib import contextmanager
from datetime import datetime

import requests
import pandas as pd


import kill_switch

def _retry_note():
    """크래시 알림에 붙일 '이후 어떻게 되는지' 안내.

    [2026-08-21 수정] 종전 문구는 "실행 중단(자동 재시도 없음)" 이었는데, 사실과
    다르다 — 2026-07-21 에 run_kis_trader.bat 에 재시도 루프(5분 간격 2회)를 넣고
    이 문구를 안 고쳤다. 사용자가 '완전히 멈췄다'고 오해하게 만든다
    (08-21 15:22 PM ReadTimeout 크래시 때 실제로 그렇게 읽혔다).
    """
    return ("run_kis_trader.bat 이 5분 뒤 재시도합니다(최대 2회). "
            "재시도 시작/종료도 로그에 남으니, 로그에 attempt 2 가 없으면 "
            "재시도 대기 중 프로세스가 죽은 것입니다.")

# ── 상수 ──────────────────────────────────────────────────────────────────────
# 콘솔 인코딩 방탄(2026-07-17) — bat 는 PYTHONIOENCODING=utf-8 을 세팅하지만 수동/타
# 런처 실행은 cp949 콘솔이라 ⚠🚨 등 이모지 print 가 UnicodeEncodeError 로 즉사
# (가드 실사격 테스트에서 실제 크래시 — progress.py 게이지 크래시와 동일 클래스).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MOCK_URL         = "https://openapivts.koreainvestment.com:29443"
TOKEN_CACHE      = ".kis_mock_token.json"
SIGNALS_CSV      = "./kis_paper_signals.csv"   # kis_live_signal.py 출력 파일
ORDERS_DIR       = "./db/kiwoom"               # 키움과 공유 (일자별 주문 로그)
KIS_POSITIONS_CSV = "./db/kis/kis_positions.csv"   # KIS 전용 진입가 추적
HOLDING_DAYS_DEFAULT = 20
MAX_CONCURRENT   = 10
MIN_ORDER_AMOUNT = 100_000

STRATEGY_MAX_SLOTS = {
    "h52w_for3d_mkt": 4,
    "for_high20_mkt": 4,
    "gc_for3d":       2,
}
STRATEGY_PRIORITY = ["h52w_for3d_mkt", "for_high20_mkt", "gc_for3d"]

# 전략별 stop-loss (백테스트 최적, delta 양수)
# h52w_for3d_mkt: stop-15  delta +0.14%
# for_high20_mkt: stop-10  delta +0.22%
# gc_for3d:       stop-26  delta +0.07%
STRATEGY_STOP = {
    "h52w_for3d_mkt": -0.15,
    "for_high20_mkt": -0.10,
    "gc_for3d":       -0.26,
}

# 강도 필터(2026-07-21 신설, 사용자 결정) — score_ic(0~10)가 이 값 미만이면 매수 스킵.
# 종전 KIS 는 강도로 '정렬만' 하고 차단은 안 해(표본 축적 우선) 6 미만도 전량 매수 후보였음
# — 실제로 7월 KIS 신호 14건 중 6.0 이상은 1건뿐이라 대부분이 6 미만 체결(보유 092730
# 네오팜 5.84 등).
# [KIS 는 5.7] 처음엔 양 계좌 6.0 통일이었으나 사용자 재검토로 KIS 만 5.7 로 조정 —
# KIS 신호 풀이 월 14건 수준으로 희소해 6.0 이면 월 1건까지 줄어 사실상 매매 정지가 됨
# (7월 소급: 6.0→1건 vs 5.7→3건). 키움은 신호가 월 595건이라 6.0 을 감당 가능.
# ※ 계좌별 임계가 다르므로 계좌 간 성과 비교 시 이 차이를 감안할 것.
# 강도 무기록 신호는 매수 전 '강도 재확인'에서 즉석 재계산 후에도 불가하면 차단(fail-closed,
# 2026-07-21) — 종전 fail-open 이 6 미만 체결의 잔여 경로였음.
MIN_STRENGTH_SCORE = 5.7


# ── 환경변수 로드 ──────────────────────────────────────────────────────────────
def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            # 인라인 주석은 '공백 뒤 #' 부터만 제거 — 값 안의 '#'(비밀키 등) 보존(2026-07-07)
            for _i, _ch in enumerate(v):
                if _ch == "#" and (_i == 0 or v[_i - 1] in " \t"):
                    v = v[:_i]
                    break
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_dotenv()

_APP_KEY    = os.getenv("KIS_MOCK_APP_KEY", "")
_APP_SECRET = os.getenv("KIS_MOCK_APP_SECRET", "")
_ACCOUNT    = os.getenv("KIS_MOCK_ACCOUNT", "")

if not _APP_KEY or not _APP_SECRET or not _ACCOUNT:
    print("[ERROR] .env 에 KIS_MOCK_APP_KEY / KIS_MOCK_APP_SECRET / KIS_MOCK_ACCOUNT 없음")
    sys.exit(1)

_CANO         = _ACCOUNT[:8]
_ACNT_PRDT_CD = _ACCOUNT[8:] if len(_ACCOUNT) > 8 else "01"


# ── 파일락 (kiwoom_trader와 동일 락 파일 공유) ──────────────────────────────────
_LOCK_CANDIDATES = [
    os.getenv("KIWOOM_LOCK", ""),
    "../Stock_AI_Project/data/.kiwoom.lock",
    str(_FIN_KIWOOM_LOCK),                    # FIN_ROOT 기반(fin_paths) — 이관 시 자동 추적
]
_LOCK_PATH = next(
    (p for p in _LOCK_CANDIDATES
     if p and os.path.dirname(p) and os.path.isdir(os.path.dirname(p))),
    None
)


def _lock_try_acquire(who: str):
    """O_CREAT|O_EXCL 원자적 잠금 시도. True=획득 / False=남이 보유 / None=쓰기불가.

    [2026-07-07] '존재확인 후 open(w)' 는 확인~쓰기 사이 레이스로 두 프로세스가
    동시 획득 가능했음 — kiwoom_trader 와 동일 수정."""
    try:
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            json.dump({"who": who, "ts": time.time(),
                       "started": datetime.now().isoformat()}, f)
        return True
    except FileExistsError:
        return False
    except Exception:
        return None


@contextmanager
def kis_lock(timeout: int = 60):
    if _LOCK_PATH is None:
        yield
        return
    who = f"kis_trader(pid={os.getpid()})"
    deadline = time.time() + timeout
    acquired = False
    while True:
        got = _lock_try_acquire(who)
        if got is True:
            acquired = True
            break
        if got is None:
            break
        try:
            with open(_LOCK_PATH) as f:
                info = json.load(f)
            age = time.time() - info.get("ts", 0)
            if age > 300:
                # 삭제 직전 재확인 — 다른 대기자의 '새 락' 오삭제 방지(2026-07-10, 키움 동일)
                with open(_LOCK_PATH) as f2:
                    if json.load(f2).get("ts") != info.get("ts"):
                        continue
                os.remove(_LOCK_PATH)
                continue
            print(f"[kis_lock] 대기 중 ({info.get('who','?')}, {age:.0f}s)...")
        except FileNotFoundError:
            continue
        except Exception:
            break
        if time.time() > deadline:
            print("[kis_lock] 타임아웃 — 잠금 무시하고 진행")
            break
        time.sleep(5)
    try:
        yield
    finally:
        if acquired:   # 내가 획득한 잠금만 해제(타임아웃 진행 시 남의 락 오삭제 방지)
            try:
                os.remove(_LOCK_PATH)
            except Exception:
                pass


# ── KIS 모의투자 클라이언트 ────────────────────────────────────────────────────
# ── 잔고조회 타임아웃 (2026-09-04 실측 기반) ────────────────────────────────
# 한산한 장중(11시대)에 잰 잔고조회 응답시간: 평균 5.73초 / 최대 6.49초.
# 종전 timeout=15 는 기준선의 2.6배밖에 안 됐다 — 마감 동시호가(15:20~15:30)에
# 부하가 조금만 올라도 그대로 넘긴다. 실제로 08-11/12/14/21/26, 09-01/03 의
# ReadTimeout 이 거의 전부 15:21 PM 실행이었다.
#
# 만기청산은 **15:30 이라는 마감시한**이 있는 경로다. 종전 구조는 15s x 6회로
# 잘게 끊어 시도했는데, 서버가 느린 것이지 죽은 게 아니라면 짧은 시도를 여러 번
# 하는 건 전부 헛되다. 총 대기 예산은 비슷하게 두고 **한 번의 시도가 서버를
# 기다리는 시간**을 늘린다: 15s x 6 = 90초 대기 → 40s x 3 = 120초 대기.
# (최악 소요는 backoff 포함 약 126초로 종전 120초와 거의 같다)
BALANCE_TIMEOUT = 20          # 기본 — 측정 기준선의 3.5배
BALANCE_TIMEOUT_EXPIRE = 40   # 만기청산 경로 — 마감시한이 있어 '느린 서버'를 더 기다린다


# ── KIS 토큰 오류 판정 (2026-09-04 신설) ────────────────────────────────────
# KIS 는 **만료된 토큰에 401 이 아니라 HTTP 500** 을 준다. 원인은 본문에만 있다.
#   {"rt_cd":"1","msg1":"기간이 만료된 token 입니다.","msg_cd":"EGW00123"}
# 상태코드만 보면 '서버 장애'로 오분류되어, 같은 만료 토큰으로 재시도만 반복하다
# 포기한다. 실사고 2026-09-04: 잔고조회가 하루 종일 500, KisStopCheck 이 15분마다
# 실패, KisTraderAM 도 3회 재시도 전부 실패. 로컬 만료 계산은 '아직 1.5시간 남음'
# 이었다 — **로컬 계산만 믿으면 안 된다. 서버가 만료라면 만료다.**
_TOKEN_ERR_CODES = {
    "EGW00123",   # 기간이 만료된 token
    "EGW00121",   # 유효하지 않은 token
    "EGW00105",   # 사용할 수 없는 token
    "EGW00103",   # token 이 없음
}


def _kis_msg(resp):
    """응답 본문의 msg_cd/msg1 을 짧게 — 경보에 '500' 만 뜨면 원인을 못 찾는다."""
    try:
        d = resp.json()
        cd, m1 = str(d.get("msg_cd", "")).strip(), str(d.get("msg1", "")).strip()
        return (f"{cd} {m1}".strip() or f"{resp.status_code} Server Error")
    except Exception:
        return f"{resp.status_code} Server Error"


def _is_token_error(resp):
    """이 응답이 '토큰 문제'인가 — 상태코드가 아니라 본문 msg_cd 로 판정한다."""
    try:
        return str(resp.json().get("msg_cd", "")) in _TOKEN_ERR_CODES
    except Exception:
        return False


class KISMockClient:
    """항상 openapivts(모의) URL만 사용. 실전 계좌는 절대 접근 불가."""

    def __init__(self):
        self._token = None

    @staticmethod
    def _key_fingerprint():
        """현재 APP_KEY 의 짧은 지문 — 캐시가 '어느 키로 받은 토큰'인지 식별용.
        키 원문을 파일에 남기지 않기 위해 해시 앞 12자만 쓴다."""
        import hashlib
        return hashlib.sha256(_APP_KEY.encode("utf-8")).hexdigest()[:12]

    def _load_cached_token(self):
        """캐시된 토큰 재사용. 단 **발급에 쓴 앱키가 지금과 같을 때만**.

        [2026-08-21 신설] 모의계좌를 재발급받으면 APP_KEY/SECRET 이 바뀌는데,
        캐시 파일에는 어느 키로 받은 토큰인지 정보가 없어서 만료 전(최대 24시간)
        까지 **옛 토큰을 새 자격증명과 섞어 계속 보냈다**. 증상은 인증 실패인데
        원인은 캐시라, '재발급했는데도 안 된다'로 오진하기 딱 좋다.
        지문이 다르면 캐시를 무시하고 새로 발급한다(구 형식 = 지문 없음도 무시).
        """
        if not os.path.exists(TOKEN_CACHE):
            return None
        try:
            with open(TOKEN_CACHE) as f:
                d = json.load(f)
            if d.get("key_fp") != self._key_fingerprint():
                print("[KIS 모의] 앱키가 바뀌었습니다 — 캐시된 토큰 폐기 후 재발급")
                return None
            if d.get("expire", 0) - 300 > time.time():
                return d["token"]
        except Exception:
            pass
        return None

    def invalidate_token(self):
        """캐시된 토큰 폐기 — 다음 호출에서 새로 발급받는다.

        서버가 '만료'라고 하면 로컬 만료 계산이 뭐라 하든 그게 사실이다.
        """
        self._token = None
        try:
            if os.path.exists(TOKEN_CACHE):
                os.remove(TOKEN_CACHE)
        except Exception as e:
            print(f"[warn] 토큰 캐시 삭제 실패(무시하고 재발급 시도): {e}")

    def _request_token(self):
        r = requests.post(
            f"{MOCK_URL}/oauth2/tokenP",
            headers={"Content-Type": "application/json"},
            json={"grant_type": "client_credentials",
                  "appkey": _APP_KEY, "appsecret": _APP_SECRET},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        token = d["access_token"]
        # KIS 는 expires_in=86400 을 주지만 **그보다 먼저 만료시킨다**(2026-09-04 실측:
        # 09-03 13:05 발급분이 09-04 09:00 에 이미 EGW00123). 로컬 신뢰 수명을 12시간으로
        # 잘라 하루 2회 재발급한다 — 발급 비용은 무시할 수준이고, 아래 반응형 재발급이
        # 최종 안전망이라 이건 왕복 한 번을 아끼는 보조 장치다.
        _ttl = min(int(d.get("expires_in", 86400)), 12 * 3600)
        expire = time.time() + _ttl
        with open(TOKEN_CACHE, "w") as f:
            # key_fp: 어느 앱키로 받은 토큰인지 — 재발급 후 옛 토큰 재사용 방지
            json.dump({"token": token, "expire": expire,
                       "key_fp": self._key_fingerprint()}, f)
        return token

    def get_token(self):
        cached = self._load_cached_token()
        if cached:
            return cached
        token = self._request_token()
        print(f"[KIS 모의] 토큰 발급 OK ({MOCK_URL})")
        return token

    def _hdrs(self, tr_id, hashkey=None):
        h = {
            "Content-Type":  "application/json; charset=utf-8",
            "authorization": f"Bearer {self.get_token()}",
            "appkey":        _APP_KEY,
            "appsecret":     _APP_SECRET,
            "tr_id":         tr_id,
            "custtype":      "P",
        }
        if hashkey:
            h["hashkey"] = hashkey
        return h

    def _hashkey(self, body: dict) -> str:
        # [2026-07-14] 경로 수정: /oauth2/Hashkey → /uapi/hashkey (KIS 공식).
        # 종전 경로는 매 주문마다 404 를 반환해 왕복 낭비+로그 노이즈였음(실측 07-13,
        # 폴백 "" 덕에 주문 자체는 성공 — hashkey 는 모의에서 선택 헤더).
        try:
            r = requests.post(
                f"{MOCK_URL}/uapi/hashkey",
                headers={"Content-Type": "application/json",
                         "appkey": _APP_KEY, "appsecret": _APP_SECRET},
                json=body, timeout=10,
            )
            r.raise_for_status()
            return r.json().get("HASH", "")
        except Exception as e:
            print(f"[warn] hashkey 실패(주문은 hashkey 없이 진행): {e}")
            return ""

    def get_balance(self, attempts: int = 3, timeout: int | None = None):
        """잔고 조회 → (deposit, positions).

        deposit   : int — 예수금(주문가능)
        positions : dict[code → {qty, name}]
        """
        params = {
            "CANO": _CANO, "ACNT_PRDT_CD": _ACNT_PRDT_CD,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
            "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        # 5xx/네트워크 오류는 3회 지수백오프 재시도 — 07-01 KIS 모의서버 일시 500 으로
        # cmd_buy 가 통째 중단(신호 2건 미매수)된 재발 방지. 4xx 는 즉시 raise(2026-07-02).
        # [2026-08-21] attempts 를 호출측이 정할 수 있게 했다. 08-21 15:21 PM 은
        # 3회(15s×3 + 백오프 6s)가 전부 타임아웃되며 만기청산을 시도조차 못 하고
        # 죽었다 — 그런데 마감 동시호가는 15:30 까지라 그 시점에 아직 8분이 남아
        # 있었고, 실제로 15:40 조회는 정상이었다. 창이 넉넉한 청산 경로에서는
        # 더 끈질기게 기다리는 편이 낫다(매수 경로는 시가 진입이 목적이라 3회 유지).
        r = None
        last_err = None
        _token_retried = False
        _n = max(1, int(attempts))
        for _attempt in range(_n):
            try:
                r = requests.get(
                    f"{MOCK_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
                    headers=self._hdrs("VTTC8434R"),
                    params=params, timeout=(timeout or BALANCE_TIMEOUT),
                )
                if r.status_code >= 500:
                    # 만료 토큰도 500 으로 온다 — 재시도가 아니라 재발급이 답이다.
                    if _is_token_error(r) and not _token_retried:
                        _token_retried = True
                        print("[KIS 모의] 토큰 만료 감지 — 재발급 후 재시도")
                        self.invalidate_token()
                    raise requests.exceptions.HTTPError(
                        f"{r.status_code} {_kis_msg(r)} (잔고조회)", response=r)
                r.raise_for_status()
                break
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError) as e:
                _st = getattr(getattr(e, "response", None), "status_code", None)
                if _st is not None and 400 <= _st < 500:
                    raise                       # 4xx = 요청 문제, 재시도 무의미
                last_err = e
                if _attempt < _n - 1:
                    # 토큰을 방금 재발급했으면 기다릴 이유가 없다(서버 장애가 아니다)
                    wait_s = 0 if _token_retried and _attempt == 0 else 2 * (_attempt + 1)
                    print(f"[warn] 잔고조회 실패({e}) — {wait_s}초 후 재시도 "
                          f"{_attempt + 2}/{_n}")
                    if wait_s:
                        time.sleep(wait_s)
        else:
            raise last_err
        data = r.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"잔고조회 오류: {data.get('msg1','')}")

        o2 = data.get("output2", [{}])
        o2d = o2[0] if o2 else {}
        dep_raw = o2d.get("dnca_tot_amt", "0")
        deposit = _to_int(dep_raw)

        # 총평가금액·평가손익 등 요약(앱 헤드라인과 동일 항목). 반환 시그니처는 유지하고
        # 마지막 조회 요약을 속성에 보관 → cmd_status 가 스냅샷에 함께 기록.
        self.last_summary = {
            "deposit":      deposit,                                       # 예수금총금액(정산완료분)
            "orderable":    _to_int(o2d.get("prvs_rcdl_excc_amt", 0)),    # 가수도정산금액(D+2 정산대기 포함 주문가능)
            "total_eval":   _to_int(o2d.get("tot_evlu_amt", 0)),          # 총평가금액(앱 헤드라인)
            "securities":   _to_int(o2d.get("scts_evlu_amt", 0)),         # 유가증권 평가금액
            "eval_pnl":     _to_int(o2d.get("evlu_pfls_smtl_amt", 0)),    # 평가손익합계
            "purchase_amt": _to_int(o2d.get("pchs_amt_smtl_amt", 0)),     # 매입금액합계
        }

        positions = {}
        for it in data.get("output1", []):
            code = str(it.get("pdno", "")).zfill(6)
            qty  = _to_int(it.get("hldg_qty", "0"))
            name = it.get("prdt_name", "")
            if code and qty > 0:
                positions[code] = {
                    "qty": qty, "name": name,
                    "price":     _to_int(it.get("prpr", 0)),          # 현재가(장중 라이브)
                    "avg_price": _to_int(it.get("pchs_avg_pric", 0)),  # 매입평균
                    "pnl_pct":   _to_float(it.get("evlu_pfls_rt", 0)), # 평가손익률(%)
                }
        return deposit, positions

    def _order(self, tr_id: str, code: str, qty: int, label: str) -> str:
        """시장가 주문 전송 — 토큰 만료면 **한 번만** 재발급 후 재전송.

        [2026-09-04 신설] 종전엔 raise_for_status() 뿐이라, 토큰이 만료되면
        (KIS 는 만료에 500 을 준다) 주문이 그대로 실패했다. 잔고조회는 재시도라도
        했지만 주문은 그마저 없었다. 그날은 신호가 전부 강도 미달이라 주문이 0건
        이어서 안 터졌을 뿐, 매수가 있었으면 그대로 유실됐다.

        재시도를 **토큰 오류로만** 한정하는 이유가 핵심이다:
          · 일반 5xx/타임아웃은 주문이 접수됐는지 알 수 없다 → 재전송은 곧 이중주문.
          · 토큰 오류는 KIS 게이트웨이가 **인증 단계에서** 거부한 것이라
            주문이 접수되지 않은 것이 확실하다 → 재전송이 안전하다.
        이 조건을 넓히면 이중주문이 난다. 절대 완화하지 말 것.
        """
        body = {
            "CANO": _CANO, "ACNT_PRDT_CD": _ACNT_PRDT_CD,
            "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0",
        }
        for _try in range(2):
            r = requests.post(
                f"{MOCK_URL}/uapi/domestic-stock/v1/trading/order-cash",
                headers=self._hdrs(tr_id, self._hashkey(body)),
                json=body, timeout=15,
            )
            if r.status_code >= 500:
                if _try == 0 and _is_token_error(r):
                    print(f"[KIS 모의] {label} 중 토큰 만료 — 재발급 후 재전송 "
                          f"({code} {qty}주, 주문 미접수 확인됨)")
                    self.invalidate_token()
                    continue
                raise requests.exceptions.HTTPError(
                    f"{r.status_code} {_kis_msg(r)} ({label})", response=r)
            r.raise_for_status()
            data = r.json()
            if data.get("rt_cd") != "0":
                _raise_order_error(label, data)
            return data.get("output", {}).get("odno", "")
        raise RuntimeError(f"{label} 실패 — 토큰 재발급 후에도 접수되지 않음")

    def order_buy(self, code: str, qty: int) -> str:
        """시장가 매수. 주문번호(odno) 반환."""
        return self._order("VTTC0802U", code, qty, "매수")

    def order_sell(self, code: str, qty: int) -> str:
        """시장가 매도. 주문번호(odno) 반환."""
        return self._order("VTTC0801U", code, qty, "매도")


class AccountBlocked(RuntimeError):
    """계좌 자체가 주문을 받지 못하는 상태(모의계좌 만료·정지 등).

    [2026-08-20 신설] 08-10 이후 모의계좌가 모든 주문을 msg_cd=40910000
    '모의투자 주문이 불가한 계좌입니다' 로 거부했는데, 종목 단위 주문 실패와
    똑같이 '[실패] …' 한 줄만 찍히고 exit 0 으로 끝나 열흘 동안 아무도 몰랐다.
    그 사이 만기가 지난 017890 한국알콜이 청산되지 못하고 계속 보유됐다.
    '이 종목이 안 되는 것'과 '계좌가 죽은 것'은 완전히 다른 사건이므로 타입을
    분리해 긴급 경보로 승격한다.
    """


# 계좌 단위 치명 오류 판별 — 종목/가격/수량 사유(정상 거부)와 구분한다.
#   msg_cd 40910000 : 모의투자 주문이 불가한 계좌입니다 (계좌 만료/정지)
#   그 외 문구 매칭  : 코드 체계가 바뀌어도 잡히도록 보조
_BLOCKED_MSG_CODES = {"40910000"}
_BLOCKED_MSG_WORDS = ("주문이 불가한 계좌", "사용할 수 없는 계좌", "해지된 계좌",
                      "정지된 계좌", "계좌가 없습니다")


def _is_account_blocked(msg_cd: str, msg1: str) -> bool:
    if str(msg_cd or "").strip() in _BLOCKED_MSG_CODES:
        return True
    m = str(msg1 or "")
    return any(w in m for w in _BLOCKED_MSG_WORDS)


def _raise_order_error(side: str, data: dict):
    """주문 응답(rt_cd != 0)을 적절한 예외로 승격. 반드시 예외를 던진다."""
    msg_cd = data.get("msg_cd", "")
    msg1 = data.get("msg1", "")
    if _is_account_blocked(msg_cd, msg1):
        raise AccountBlocked(f"{side} 불가 — 계좌 상태 이상 [{msg_cd}] {msg1}")
    raise RuntimeError(f"{side} 오류: {msg1}")


def _alert_account_blocked(where: str, detail: str):
    """계좌 주문불가를 긴급 등급으로 1회 통지(같은 실행 안에서는 중복 억제)."""
    if getattr(_alert_account_blocked, "_sent", False):
        return
    _alert_account_blocked._sent = True
    print(f"🚨 [KIS 계좌 주문불가] {where}: {detail}")
    try:
        import notifier
        notifier.safe_send(
            f"🚨 [KIS 안D] 계좌가 주문을 받지 못합니다 — {where}\n"
            f"  {detail}\n"
            f"  매수·매도 전부 거부됩니다(만기 청산 포함). 모의투자 계좌 기간만료/재발급 여부를\n"
            f"  한국투자증권 오픈API 포털에서 확인해 주세요. 확인 전까지 KIS 매매는 사실상 정지 상태입니다.")
    except Exception:
        pass


def _to_int(v, default=0):
    try:
        return int(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return default


def _to_float(v, default=0.0):
    # get_balance 의 evlu_pfls_rt(평가손익률 %) 등 실수 필드용 — 2026-06-29 추가.
    # (과거 line 275 가 미정의 _to_float 를 호출해 get_balance NameError → 잔고/매수/매도 전부 크래시)
    try:
        return float(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return default


# ── 진입가 추적 (stop-loss 계산용) ────────────────────────────────────────────
def load_kis_positions():
    """kis_positions.csv → {code: {entry_px, strategy, signal_date, holding_days}}.

    매수 체결 시 기록, 매도 체결 시 삭제.
    [2026-07-10] 전 컬럼 dtype=str — 한 행이라도 signal_date 가 비면 pandas float 승격으로
    전 행이 'YYYYMMDD.0' 이 되어 원장 만기판정이 통째로 무력화되던 위험 차단(키움 동일).
    """
    path = KIS_POSITIONS_CSV
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
        df["code"] = df["code"].astype(str).str.zfill(6)
        result = {}
        for _, r in df.iterrows():
            try:
                holding = int(float(str(r.get("holding_days", "") or 0)))
            except (TypeError, ValueError):
                holding = HOLDING_DAYS_DEFAULT
            result[r["code"]] = {
                "entry_px":    _to_float(r.get("entry_px", 0), 0.0),
                "strategy":    str(r.get("strategy", "") or ""),
                "signal_date": _re.sub(r"\.0$", "", str(r.get("signal_date", "") or "")),
                "holding_days": holding if holding > 0 else HOLDING_DAYS_DEFAULT,
            }
        return result
    except Exception as e:
        print(f"[warn] kis_positions 로드 실패: {e}")
        return {}


def save_kis_position(code, entry_px, strategy, signal_date, holding_days):
    """매수 후 진입가 기록."""
    os.makedirs(os.path.dirname(KIS_POSITIONS_CSV), exist_ok=True)
    existing = load_kis_positions()
    existing[str(code).zfill(6)] = {
        "entry_px": entry_px,
        "strategy": strategy,
        "signal_date": signal_date,
        "holding_days": holding_days,
    }
    _write_positions(existing)


def remove_kis_position(code):
    """매도 후 진입가 레코드 삭제."""
    existing = load_kis_positions()
    existing.pop(str(code).zfill(6), None)
    _write_positions(existing)


def _write_positions(pos_dict):
    """원자적 재작성(tmp + os.replace) — 도중 크래시로 0바이트/부분행이 되면 전 종목
    stop-loss 가 조용히 죽는 파일이라 truncate 창을 없앤다(2026-07-10, daily_loader 패턴)."""
    os.makedirs(os.path.dirname(KIS_POSITIONS_CSV), exist_ok=True)
    fields = ["code", "entry_px", "strategy", "signal_date", "holding_days"]
    tmp = KIS_POSITIONS_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for code, d in pos_dict.items():
            w.writerow({"code": code, **d})
    os.replace(tmp, KIS_POSITIONS_CSV)


# ── KIS 원장 자가치유(2026-07-10 신설 — 키움 ensure_kiwoom_ledger 와 대칭) ──────
# 배경: KIS 는 heal 이 없어 원장 유실(파일 손상·수동 조작) 시 전 종목 stop-loss 가
# 조용히 무력화됐음(로드 실패 → {} → 경고 한 줄). 브로커 보유인데 원장에 없는 종목을
# 주문로그(kis_orders_*.csv)와 신호CSV 역추적으로 재등록한다.
_KIS_HEAL_HOLDING = {"h52w_for3d_mkt": 20, "for_high20_mkt": 20, "gc_for3d": 15}


def _kis_held_buy_map():
    """kis_orders_*.csv 전체에서 {code → 마지막 '성공 매수'의 {strategy, date, ref_px}}."""
    out = {}
    try:
        for f in sorted(_glob.glob(f"{ORDERS_DIR}/kis_orders_*.csv")):
            try:
                df = pd.read_csv(f, dtype={"code": str})
            except Exception:
                continue
            if "side" not in df.columns:
                continue
            b = df[(df["side"] == "buy")
                   & df["ok"].astype(str).str.lower().isin(("true", "1"))]
            date = os.path.basename(f)[11:19]   # kis_orders_YYYYMMDD.csv
            for _, r in b.iterrows():   # 파일 오름차순 → 최근 매수가 덮어씀
                out[str(r.get("code", "")).zfill(6)] = {
                    "strategy": str(r.get("strategy", "") or ""),
                    "date": date,
                    "ref_px": _to_int(r.get("price", 0)),
                }
    except Exception:
        pass
    return out


def _kis_signal_date_lookup(code, strategy, buy_date):
    """kis_paper_signals.csv 에서 매수일 직전의 실제 신호일 역추적 — 치유용(키움 동일 패턴)."""
    try:
        if not os.path.exists(SIGNALS_CSV) or not buy_date:
            return ""
        s = pd.read_csv(SIGNALS_CSV, dtype=str)
        s["code"] = s["code"].astype(str).str.zfill(6)
        s["signal_date"] = s["signal_date"].astype(str).str.replace("-", "", regex=False)
        s = s[(s["code"] == str(code).zfill(6)) & (s["signal_date"] < str(buy_date))]
        if strategy and "strategy" in s.columns:
            st = s[s["strategy"] == strategy]
            if not st.empty:
                s = st
        return str(s["signal_date"].max()) if not s.empty else ""
    except Exception:
        return ""


def ensure_kis_ledger(positions):
    """원장 자가치유 — 정리(prune: 원장에만 있음) + 등록(heal: 브로커에만 있음).

    키움 ensure_kiwoom_ledger 와 동일 정책:
    - 오늘 매도 코드 재등록 금지(체결 지연 잔상 부활 방지)
    - 오늘 매수 코드는 잔고 미반영이어도 prune 제외
    - 잔고가 통째 빈 응답(글리치 의심)이면 오늘 매도분만 정리
    - heal 메타데이터: entry_px=브로커 매입평균 우선, holding 전략별, signal_date 역추적"""
    try:
        led = load_kis_positions()
        sold_today = today_ordered_codes("sell")
        bought_today = today_ordered_codes("buy")
        pos_codes = {str(c).zfill(6) for c in positions.keys()}
        changed = False

        for code in list(led.keys()):   # prune
            if code in pos_codes or code in bought_today:
                continue
            if code in sold_today or pos_codes:
                led.pop(code, None)
                changed = True
                why = "오늘 매도 체결" if code in sold_today else "브로커 미보유(수동매도/체결 잔재)"
                print(f"[ledger] KIS 원장 정리: {code} — {why}")

        # 실체결 보정(2026-07-11): cmd_buy 는 entry_px 에 신호일 종가를 기록하지만
        # 백테스트 stop 기준은 '실제 진입 체결가' — 갭 진입 시 발동선이 수 %p 어긋남.
        # 브로커 매입평균이 잡히는 대로 보정(매수 당일 status 에서 즉시 반영).
        for code, p in positions.items():
            code = str(code).zfill(6)
            row = led.get(code)
            avg = float(p.get("avg_price", 0) or 0)
            if row and avg > 0 and float(row.get("entry_px", 0) or 0) != avg:
                print(f"[ledger] KIS entry_px 실체결 보정: {code} "
                      f"{float(row.get('entry_px', 0) or 0):,.0f} → {avg:,.0f} (stop 기준가 갱신)")
                row["entry_px"] = avg
                changed = True

        buys = None
        added = 0
        blind = []   # 메타데이터 없이 치유된 코드 — 만기·stop 추적 불가라 텔레그램 경보
        for code, p in positions.items():   # heal
            code = str(code).zfill(6)
            if code in led or code in sold_today:
                continue
            if buys is None:
                buys = _kis_held_buy_map()
            info = buys.get(code, {})
            entry = float(p.get("avg_price", 0) or 0) or float(info.get("ref_px", 0) or 0)
            strat = info.get("strategy", "")
            buy_date = info.get("date", "")
            sig_date = _kis_signal_date_lookup(code, strat, buy_date) or buy_date
            led[code] = {"entry_px": entry,
                         "strategy": strat,
                         "signal_date": sig_date,
                         "holding_days": _KIS_HEAL_HOLDING.get(strat, HOLDING_DAYS_DEFAULT)}
            if not strat or not sig_date:
                blind.append(code)
            added += 1
            changed = True
            print(f"[ledger] KIS 원장 자가치유 등록: {code} (전략 {strat or '?'}, 진입 {entry:,.0f})")
        if blind:   # '조용한 실패' 격상 — 빈 메타데이터 행은 stop_pct=None·만기 판정불가(2026-07-11)
            try:
                import notifier
                notifier.safe_send(
                    f"⚠️ [KIS] 매수 기록이 없는 보유 종목 발견: {blind}\n"
                    f"  원장에 자동 등록했지만 진입일/전략을 몰라 **손절·만기 자동매도가 안 됩니다**.\n"
                    f"  조치: 대시보드에서 확인 후 직접 매도하거나 그대로 두셔도 됩니다"
                    f"(다른 종목 매매엔 영향 없음).")
            except Exception:
                pass
        if changed:
            _write_positions(led)
            if added:
                print(f"[ledger] KIS 자가치유 {added}건 → {KIS_POSITIONS_CSV}")
    except Exception as e:
        print(f"[warn] KIS 원장 자가치유 실패(무시): {e}")


# ── 공용 헬퍼 ─────────────────────────────────────────────────────────────────
def log_order(row):
    os.makedirs(ORDERS_DIR, exist_ok=True)
    path = f"{ORDERS_DIR}/kis_orders_{datetime.today():%Y%m%d}.csv"
    exists = os.path.exists(path)
    fields = ["time", "side", "code", "name", "strategy", "qty", "price",
              "order_type", "reason", "ok", "order_no", "msg"]
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def get_signal_strategy_map():
    """kis_paper_signals.csv → {code: strategy} (최신 신호 기준)."""
    if not os.path.exists(SIGNALS_CSV):
        return {}
    try:
        s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
        s["code"] = s["code"].astype(str).str.zfill(6)
        if "strategy" not in s.columns:
            return {c: "h52w_for3d_mkt" for c in s["code"].unique()}
        s = s.sort_values("signal_date", ascending=False).drop_duplicates("code", keep="first")
        return dict(zip(s["code"], s["strategy"]))
    except Exception:
        return {}


def count_slots_by_strategy(pos_codes, strategy_map):
    """보유 종목 → 전략별 슬롯 사용 수. 안D에 없는 전략은 _legacy."""
    counts = {k: 0 for k in STRATEGY_MAX_SLOTS}
    counts["_legacy"] = 0
    for code in pos_codes:
        strat = strategy_map.get(str(code).zfill(6), "h52w_for3d_mkt")
        if strat in STRATEGY_MAX_SLOTS:
            counts[strat] += 1
        else:
            counts["_legacy"] += 1
    return counts


def today_ordered_codes(side=None):
    path = f"{ORDERS_DIR}/kis_orders_{datetime.today():%Y%m%d}.csv"
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, dtype={"code": str})
        if side:
            df = df[df["side"] == side]
        ok = df["ok"].astype(str).str.lower().isin(("true", "1"))
        return set(df[ok]["code"].astype(str).str.zfill(6))
    except Exception:
        return set()


def latest_macro_date():
    files = sorted(_glob.glob("./macro_data/daily/*.csv"))
    return os.path.basename(files[-1])[:-4] if files else None


def _today_close_map():
    """최신 macro CSV에서 종가 맵 {code: close}.

    오늘 macro가 15:21 시점엔 아직 미갱신 → latest_macro_date()로 가장 최근 파일 사용.
    stop 체크는 '전일 종가 <= 진입가 x (1+stop_pct)' 기준이므로 정상 동작.
    """
    latest = latest_macro_date()
    if not latest:
        return {}
    path = f"./macro_data/daily/{latest}.csv"
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype={"code": str})
        df["code"] = df["code"].astype(str).str.zfill(6)
        col = next((c for c in df.columns if c in ("close", "종가")), None)
        if col:
            return dict(zip(df["code"], df[col].astype(float)))
    except Exception:
        pass
    return {}


# ── 청산 판단 ─────────────────────────────────────────────────────────────────
def _expiry_due(ds, signal_date, holding, today):
    """만기 도달 여부 — 진입=신호 다음 영업일, 청산=진입일 포함 holding일째.
    판정 불가(달력에 signal_date 없음)면 None.

    [2026-07-10 오프바이원 수정] 실행 시점(09:01/15:21)의 macro 달력은 '어제'까지라
    만기일 당일엔 xi==len(ds) 가 되어 항상 False → 만기가 익영업일에야 발동하고
    보유가 실질 +1일(오버나이트 갭 1회 추가)이던 버그. 달력 마지막의 '다음 영업일'이
    만기(xi-(len-1)==1)이고 today 가 달력 마지막보다 뒤면 오늘=만기일로 판정한다
    (트레이더는 거래일에만 실행되므로 안전. 데이터가 2일+ 정체면 종전처럼 보수적 대기)."""
    if not ds or not signal_date or signal_date not in ds:
        return None
    ei = ds.index(signal_date) + 1
    xi = ei + int(holding) - 1
    if xi < len(ds):
        return ds[xi] <= today
    return (xi - (len(ds) - 1)) == 1 and today > ds[-1]


def codes_due_for_exit(close_map, reasons=("expire", "stop")):
    """만기 도달 or stop 발동 종목 → {code: reason}.

    reason: 'expire' | 'stop' / reasons: 이번 실행에서 판정할 사유 필터(2026-07-17).
    stop 판단: 전일 종가(close_map) ≤ 진입가 × (1 + stop_pct)
    만기 판단: 진입일 포함 holding_days 영업일째

    [2026-07-07 변경] 판정을 원장(kis_positions.csv) 우선으로 교체 — 신호CSV 전 이력
    스캔은 같은 코드의 '옛 신호' 만기가 지나 있으면 최근 신호로 산 보유분까지 due 로
    오판(키움 CJ ENM 사례의 일반형). stop 도 원장의 전략/진입가 기준(최신 신호 전략으로
    stop_pct 를 잘못 고르는 것 방지). 원장에 없는 코드만 신호CSV 만기 폴백.

    [2026-07-17 트리거 분리] 만기(expire)는 15:21 마감 동시호가 매도(=백테스트의 '만기일
    종가 청산'과 정합, daily-pm), stop 은 09:01 시가 매도(전일 종가 판정→익일 시가 —
    stop_sweep 재수정 의미론과 정합, daily-am). 인자 없는 구 'daily' 는 기본값으로 둘 다
    판정(작업 재등록 전 하위호환 — 종전과 동일하게 만기가 09:01 시가에 나감).
    """
    from strategies.daily_loader import load_macro_daily
    # 대시 없는 'YYYYMMDD' — ds(load_macro_daily date)·signal_date 와 동일 형식.
    # (과거 '%Y-%m-%d' 대시형이라 만기 비교가 항상 False → 만기청산 영영 미발동 버그.
    #  2026-06-29 수정. 과거 매수 0건 버그와 동일 클래스.)
    today = datetime.today().strftime("%Y%m%d")
    due = {}
    kis_pos = load_kis_positions()

    df = load_macro_daily()
    code_dates = {c: sorted(g["date"].astype(str).tolist())
                  for c, g in df.groupby("code")}

    ledger_decided = set()   # 원장에 있는 코드 — 신호CSV 폴백 제외(무조건)
    undecidable = []          # 판정불가 코드 — '조용한 실패' 방지, 텔레그램 격상(2026-07-11)
    for code, info in kis_pos.items():
        # 만기 — 원장 signal_date/holding_days 기준.
        # [2026-07-10] 원장에 있으면 판정불가(verdict None)여도 폴백 금지 — None 경로로
        # '옛 신호 만기 오염'(조기청산)이 재유입되던 구멍. 판정불가는 보류+경고로 처리.
        ledger_decided.add(code)
        if "expire" in reasons:
            sd = str(info.get("signal_date", "")).replace("-", "")
            verdict = _expiry_due(code_dates.get(code), sd,
                                  info.get("holding_days", HOLDING_DAYS_DEFAULT), today)
            if verdict is None:
                print(f"[sell][warn] {code} 원장 signal_date({sd}) 달력 판정불가 — 만기 보류, 원장 확인 필요")
                undecidable.append(code)
            elif verdict:
                due[code] = "expire"
                continue
        if "stop" not in reasons:
            continue
        # stop — 원장 전략/진입가 기준. cur_close 는 NaN 가능(CSV 빈 셀) → 'not >0' 로 차단
        stop_pct = STRATEGY_STOP.get(str(info.get("strategy", "")))
        entry_px = float(info.get("entry_px", 0) or 0)
        cur_close = close_map.get(code, 0)
        if stop_pct is None or entry_px <= 0 or not (cur_close > 0):
            continue
        if cur_close <= entry_px * (1 + stop_pct):
            due[code] = "stop"

    if undecidable:
        try:
            import notifier
            notifier.safe_send(f"⚠ [KIS 만기] 판정불가 보유 {undecidable} — 원장 signal_date 확인 필요(청산 경로 없음)")
        except Exception:
            pass

    # 원장에 없는 코드(원장 유실·수동매수) 만기 폴백 — 신호CSV 스캔 (expire 판정 시에만)
    if "expire" not in reasons or not os.path.exists(SIGNALS_CSV):
        return due
    s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    s["signal_date"] = s["signal_date"].astype(str)
    s["code"] = s["code"].astype(str).str.zfill(6)
    if "holding_days" not in s.columns:
        s["holding_days"] = HOLDING_DAYS_DEFAULT
    s["holding_days"] = pd.to_numeric(s["holding_days"], errors="coerce").fillna(HOLDING_DAYS_DEFAULT).astype(int)

    for _, r in s.iterrows():
        code = r["code"]
        if code in ledger_decided or code in due:
            continue   # 원장 판정 우선 — 옛 신호의 만기 오염 차단
        if _expiry_due(code_dates.get(code), r["signal_date"],
                       int(r["holding_days"]), today):
            due[code] = "expire"

    return due


# ── 명령 구현 ─────────────────────────────────────────────────────────────────
def cmd_status():
    client = KISMockClient()
    deposit, positions = client.get_balance()

    # 원장 자가치유(prune + heal) — 2026-07-10 heal 추가로 ensure_kis_ledger 로 통합.
    # 원장 유실(파일 손상 등) 시에도 다음 status 에서 브로커 보유 기준으로 복원돼
    # stop-loss 무력화가 하루 이상 지속되지 않는다.
    ensure_kis_ledger(positions)

    strategy_map = get_signal_strategy_map()
    kis_pos = load_kis_positions()
    for _c, _v in kis_pos.items():   # 매수 당시 전략 우선(슬롯 표시 정확성)
        if _v.get("strategy"):
            strategy_map[_c] = _v["strategy"]
    slot_used = count_slots_by_strategy(positions.keys(), strategy_map)

    _sm = getattr(client, "last_summary", {}) or {}
    print(f"\n[KIS 모의 — 안D] 총평가금액: {_sm.get('total_eval', 0):,} 원")
    print(f"  예수금(정산완료) {deposit:,}  /  주문가능(D+2정산포함) {_sm.get('orderable', 0):,}"
          f"  /  보유평가 {_sm.get('securities', 0):,}")
    print(f"[평가손익] {_sm.get('eval_pnl', 0):,} 원  (매입합계 {_sm.get('purchase_amt', 0):,})")
    print(f"[보유 종목] {len(positions)} / {MAX_CONCURRENT} 슬롯")
    for strat in STRATEGY_PRIORITY:
        stop_pct = STRATEGY_STOP[strat]
        print(f"  {strat}: {slot_used[strat]}/{STRATEGY_MAX_SLOTS[strat]}"
              f"  (stop {stop_pct*100:.0f}%)")
    for code, p in positions.items():
        pos_info = kis_pos.get(code, {})
        entry_px = pos_info.get("entry_px", 0)
        strat = pos_info.get("strategy") or strategy_map.get(code, "")   # 원장 우선
        print(f"  {code} {p['name']}: {p['qty']:,}주"
              f"  진입가:{entry_px:,.0f}  전략:{strat}")

    # 스냅샷 저장
    try:
        os.makedirs(ORDERS_DIR, exist_ok=True)
        snap = {
            "date":      datetime.today().strftime("%Y%m%d"),
            "time":      datetime.now().strftime("%H:%M"),
            "broker":    "KIS_mock_andD",
            "deposit":   deposit,
            "orderable":    _sm.get("orderable"),      # 주문가능(D+2 정산대기 포함)
            "total_eval":   _sm.get("total_eval"),     # 총평가금액(앱 헤드라인)
            "securities":   _sm.get("securities"),     # 보유 평가금액
            "eval_pnl":     _sm.get("eval_pnl"),       # 평가손익합계
            "purchase_amt": _sm.get("purchase_amt"),   # 매입금액합계
            "positions": [{"code": c, "name": p["name"], "qty": p["qty"],
                           "price":     p.get("price", 0),       # 현재가
                           "avg_price": p.get("avg_price", 0),   # 매입평균
                           # 평가손익(원) = 수량 × (현재가 − 매입평균). API 가 금액을
                           # 안 주므로 계산(키움 스냅샷 pnl 과 동일 의미).
                           "pnl":       p.get("qty", 0) * (p.get("price", 0) - p.get("avg_price", 0)),
                           "pnl_pct":   p.get("pnl_pct", 0)}
                          for c, p in positions.items()],
        }
        with open(f"{ORDERS_DIR}/kis_snapshot.json", "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        hist = f"{ORDERS_DIR}/kis_equity_history.csv"
        new_file = not os.path.exists(hist)
        with open(hist, "a", newline="", encoding="utf-8-sig") as f:
            if new_file:
                f.write("date,time,deposit,n_positions\n")
            f.write(f"{snap['date']},{snap['time']},{deposit},{len(positions)}\n")
    except Exception as e:
        print(f"[warn] 스냅샷 저장 실패: {e}")


def todays_signals():
    """직전 영업일 신호 목록 (kis_paper_signals.csv 기준)."""
    if not os.path.exists(SIGNALS_CSV):
        print(f"[buy][WARN] {SIGNALS_CSV} 없음 — kis_live_signal.py 먼저 실행 필요")
        return []
    s = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    s["signal_date"] = s["signal_date"].astype(str)
    s["code"] = s["code"].astype(str).str.zfill(6)
    if "strategy" not in s.columns:
        s["strategy"] = "h52w_for3d_mkt"
    if "holding_days" not in s.columns:
        s["holding_days"] = HOLDING_DAYS_DEFAULT
    s["holding_days"] = pd.to_numeric(s["holding_days"], errors="coerce").fillna(HOLDING_DAYS_DEFAULT).astype(int)

    target_raw = latest_macro_date()
    if not target_raw:
        return []
    # [fix 2026-06-23] CSV signal_date 는 'YYYYMMDD'(대시 없음). 과거엔 target 을 대시형식으로
    #   바꿔 비교 → 항상 0건 → 매수 안 됨. 양쪽 대시 제거 후 비교.
    s["signal_date"] = s["signal_date"].str.replace("-", "", regex=False)
    target = target_raw.replace("-", "")
    today_raw = datetime.today().strftime("%Y%m%d")
    if target_raw == today_raw:
        return []
    print(f"[buy] 신호 기준일: {target}")
    if not _signals_fresh(target_raw, "KIS 안D"):
        return []
    return s[s["signal_date"] == target].to_dict("records")


def _signals_fresh(latest_date, label):
    """유니버스 신선도 게이트(키움과 동일 규약). 상세는 market_calendar 참조.

    판정 실패 시 종전대로 진행(fail-open) — 달력 오류가 매매를 멈추면 안 된다.
    """
    try:
        import market_calendar as mc
        allowed, msg = mc.check_signal_freshness(latest_date, label)
    except Exception as e:
        print(f"[buy][warn] 유니버스 신선도 판정 생략({type(e).__name__}: {e})")
        return True
    if msg:
        print(msg)
        try:
            import notifier
            notifier.safe_send(msg)
        except Exception:
            pass
    return allowed


def _strength_map(sigs):
    """db/signal_strength_log.csv → {(code, strategy): score_ic} (해당 신호일·KIS 계좌만).

    매수 후보 정렬용(키움 _strength_map 과 동일 패턴, 계좌 필터만 다름).
    파일 없음/파싱 실패 시 빈 dict — 정렬이 거래대금순으로 자연 폴백(fail-open)."""
    path = "./db/signal_strength_log.csv"
    if not os.path.exists(path) or not sigs:
        return {}
    try:
        dates = {str(s.get("signal_date", "")).replace("-", "") for s in sigs}
        df = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
        df["signal_date"] = df["signal_date"].astype(str).str.replace("-", "")
        df = df[df["signal_date"].isin(dates)]
        if "account" in df.columns:   # KIS 계좌 기록만 (키움 기록과 혼동 방지)
            df = df[df["account"].astype(str).str.lower().str.startswith("kis")]
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["score_ic"] = pd.to_numeric(df["score_ic"], errors="coerce")
        out = {}
        for _, r in df.iterrows():
            if pd.notna(r["score_ic"]):
                out[(str(r["code"]), str(r.get("strategy", "")))] = float(r["score_ic"])
        return out
    except Exception as e:
        print(f"[warn] 강도 로그 로드 실패(tv순 폴백): {e}")
        return {}


def verify_strength(sigs, strength, account="KIS_안D"):
    """[매매 전 사전점검] 강도 재확인 — 키움 verify_strength 와 동일 규약(2026-07-21).

    무기록 후보가 있으면 strength_logger 로 즉석 재계산(로그 기록까지 되어 감사와 정합).
    재계산 후에도 없으면 호출부가 차단(fail-closed).
    반환: (갱신된 strength map, 여전히 무기록인 {(code, strategy)} 집합)
    """
    need = {(str(s.get("code", "")).zfill(6), str(s.get("strategy", ""))) for s in sigs}
    missing = {k for k in need if k not in strength}
    if not missing:
        print(f"[verify] 강도 재확인 OK — 후보 {len(need)}건 전원 기록 확보")
        return strength, set()

    print(f"[verify] 강도 무기록 {len(missing)}건 — 즉석 재계산 시도(약 10초)")
    try:
        from strategies.daily_loader import load_macro_daily
        import strength_logger
        df = load_macro_daily()
        last_date = str(sigs[0].get("signal_date", "")).replace("-", "")
        strength_logger.log_strength(account, df, last_date, sigs, verbose=False)
        strength = _strength_map(sigs)
    except Exception as e:
        print(f"[verify] 강도 재계산 실패: {e}")

    still = {k for k in need if k not in strength}
    if still:
        # [2026-08-09 문구 개선] 키움과 동일 — 안전장치 정상 작동이지 고장이 아니다.
        msg = (f"ℹ️ [{account}] 강도 확인 안 된 후보 {len(still)}건은 안전하게 매수 제외했습니다"
               f"(나머지 후보는 정상 진행). 매매 이상 아님 — 며칠 반복되면 강도 로깅 점검")
        print(f"[verify] {msg}")
        try:
            import notifier
            notifier.safe_send(msg)
        except Exception:
            pass
    else:
        print(f"[verify] 강도 재계산 완료 — 후보 {len(need)}건 전원 확보")
    return strength, still


def _order_candidates(cands, strength, tv_map, strat):
    """매수 후보 정렬 — 강도(score_ic) 내림차순, 무기록은 뒤에서 거래대금 내림차순.

    [2026-07-10 도입 — 사용자 결정] 키움은 2026-07-06 tv순 역선택 진단(체결분 -3.3%p)
    으로 강도순 전환·실측 검증됨(≥6 평균 +1.90%). KIS 는 자체 표본이 13건뿐이라 통계
    확증은 불가하지만, 정렬은 후보>빈슬롯일 때만 작동하는 저위험 변경이라 대칭 적용.
    ※ 2026-07-21 부터 KIS 도 MIN_STRENGTH_SCORE(6.0) 차단 필터 적용 — 정렬은 그 후
    통과분의 우선순위를 정한다(키움과 동일 구조)."""
    def _key(r):
        code = str(r.get("code", "")).zfill(6)
        sc = strength.get((code, str(r.get("strategy", strat))))
        return (sc if sc is not None else float("-inf"),
                float(tv_map.get(r["code"], 0) or 0))
    return sorted(cands, key=_key, reverse=True)


def _reconcile_diff(positions):
    """브로커 잔고 vs 로컬 추적(kis_positions.csv) 불일치 계산.
    반환: (orphan, stale) — orphan=브로커만 보유(추적 누락), stale=로컬만 보유(브로커 없음).
    """
    broker = set(str(c).zfill(6) for c in positions.keys())
    local = set(str(c).zfill(6) for c in load_kis_positions().keys())
    return sorted(broker - local), sorted(local - broker)


def cmd_reconcile():
    """장 시작 직전/직후 잔고 정합성 점검. 불일치 시 텔레그램 경고."""
    client = KISMockClient()
    _, positions = client.get_balance()
    orphan, stale = _reconcile_diff(positions)
    print(f"[reconcile] 브로커 보유 {len(positions)}건 / orphan(브로커만) {orphan} / stale(로컬만) {stale}")
    if orphan or stale:
        msg = (f"⚠ 잔고 정합성 불일치\n브로커만(추적누락): {orphan}\n로컬만(브로커없음): {stale}\n"
               f"→ 손절·매수 관리 사각지대. 확인 필요.")
        try:
            import notifier
            notifier.safe_send(msg)
        except Exception:
            pass
        return False
    print("[reconcile] 일치 — 정상")
    return True


def cmd_buy():
    # [2026-08-27] 3층 안전장치 — 계좌 단위 정지 상태면 신규매수만 건너뛴다.
    #   매도·만기청산·손절은 이 게이트를 타지 않는다.
    kill_switch.guard_buy("KIS 안D")
    sigs = todays_signals()
    if not sigs:
        print("[buy] 오늘 신호 없음 — 종료")
        return

    client = KISMockClient()
    deposit, positions = client.get_balance()

    # ── 정합성 게이트(오주문 방지) ──────────────────────────────────────────
    # 브로커 잔고가 비었는데 로컬 추적엔 보유가 있으면 = API 응답 누락 의심.
    # 이 상태로 슬롯을 세면 '다 비었다'고 오판해 과다 매수 → 강제 중단.
    orphan, stale = _reconcile_diff(positions)
    if not positions and load_kis_positions():
        print("[buy] 잔고 응답 의심(브로커 0 / 로컬 보유 있음) — 오주문 방지 위해 매수 중단")
        try:
            import notifier
            notifier.safe_send("⛔ [KIS buy 중단] 잔고 응답이 비어 있음(로컬은 보유). API 누락 의심 → 매수 스킵.")
        except Exception:
            pass
        return
    if orphan or stale:
        print(f"[buy] 정합성 경고 — orphan(브로커만){orphan} / stale(로컬만){stale} (경고만, 매수는 진행)")
        try:
            import notifier
            notifier.safe_send(f"⚠ [KIS buy] 잔고 불일치 orphan{orphan}/stale{stale} — 진행하되 확인 권장")
        except Exception:
            pass

    already = today_ordered_codes("buy")
    # 원장(매수 당시 전략)으로 덮어씀 — '최신 신호' 전략맵은 보유 중 종목이 다른
    # 전략으로 재신호되면 슬롯을 오배정(2026-07-07, 키움과 동일 수정).
    strategy_map = get_signal_strategy_map()
    for _c, _v in load_kis_positions().items():
        if _v.get("strategy"):
            strategy_map[_c] = _v["strategy"]
    slot_used = count_slots_by_strategy(positions.keys(), strategy_map)
    legacy_used = slot_used.get("_legacy", 0)
    slot_avail = {k: max(0, STRATEGY_MAX_SLOTS[k] - slot_used[k])
                  for k in STRATEGY_MAX_SLOTS}
    total_avail = max(0, sum(slot_avail.values()) - legacy_used)

    # 매수 예산 — 주문가능금액(가수도정산 포함) 우선(2026-07-07).
    # daily 는 09:01 에 매도 후 매수하는데, 매도대금(D+2 정산대기)은 dnca_tot_amt
    # (정산완료 예수금)에 안 잡혀 과소매수가 됨. API 가 orderable 을 안 주면 예수금 폴백.
    # [매매 전 사전점검 ②] 잔고 재확인(2026-07-21) — 주문 직전 시점의 잔고를 다시 조회.
    # daily-am 은 앞단 손절매도 체결이 뒤늦게 반영되므로 초기 조회값이 낡을 수 있다.
    # 실패 시 기존 조회값 유지(보수적).
    try:
        # [2026-08-20 버그수정] get_balance() 는 (예수금, 보유dict) 튜플을 반환하는데
        # 종전엔 튜플 자체에 len() 을 씌워 **보유 종목 수가 항상 '2'로 찍혔다**
        # (실보유 1종목인 날에도 "보유 2종목"). 아래 [보유 종목] 표시와 어긋나
        # 로그를 읽는 사람이 잔고 불일치로 오해하던 원인.
        _d2, _fresh_pos = client.get_balance()
        _sum = getattr(client, "last_summary", {}) or {}
        _d2 = _to_int(_sum.get("deposit", _d2 or deposit))
        if _d2:
            deposit = _d2
        print(f"[verify] 잔고 재확인 OK — 예수금 {deposit:,}원 / 보유 {len(_fresh_pos)}종목")
    except Exception as e:
        print(f"[verify] 잔고 재조회 실패({e}) — 직전 조회값으로 진행")

    _orderable = _to_int((getattr(client, "last_summary", {}) or {}).get("orderable", 0))
    buy_budget = _orderable if _orderable > 0 else deposit
    print(f"[buy] 오늘 신호 {len(sigs)}건  예수금 {deposit:,}원  주문가능 {buy_budget:,}원")
    print("  전략별 슬롯: "
          + " / ".join(f"{k}={slot_used[k]}/{STRATEGY_MAX_SLOTS[k]}"
                       for k in STRATEGY_PRIORITY))
    if total_avail <= 0:
        print("[buy] 모든 슬롯 가득 — 주문 없음")
        return

    # 거래대금 맵 (우선순위 정렬용)
    tv_map = {}
    macro_csv = f"./macro_data/daily/{latest_macro_date()}.csv"
    if os.path.exists(macro_csv):
        md = pd.read_csv(macro_csv, encoding="utf-8-sig", dtype={"code": str})
        md["code"] = md["code"].astype(str).str.zfill(6)
        for col_cand in ("거래대금", "trading_value"):
            if col_cand in md.columns:
                tv_map = dict(zip(md["code"], md[col_cand]))
                break

    # 전략별 신호 분류 + 강도(score_ic) 내림차순, 무기록은 tv순 폴백(2026-07-10 전환)
    strength = _strength_map(sigs)
    if strength:
        n_weak = sum(1 for s in sigs
                     if (strength.get((str(s.get("code", "")).zfill(6),
                                       str(s.get("strategy", "")))) or 99) < MIN_STRENGTH_SCORE)
        print(f"[buy] 후보 정렬: 강도(score_ic) 내림차순 — 기록 {len(strength)}건")
        print(f"[buy] 강도 필터: score_ic < {MIN_STRENGTH_SCORE} 스킵 예정 {n_weak}건")
    else:
        print("[buy] 강도 기록 없음 — 재확인 단계에서 재계산 시도")
    # 매매 전 사전점검 ①: 강도 재확인(무기록이면 즉석 재계산, 그래도 없으면 차단)
    strength, _unknown_strength = verify_strength(sigs, strength)
    sigs_by_strat = {k: [] for k in STRATEGY_PRIORITY}
    for sig in sigs:
        strat = str(sig.get("strategy", "h52w_for3d_mkt"))
        if strat not in sigs_by_strat:
            strat = "h52w_for3d_mkt"
        sigs_by_strat[strat].append(sig)
    for strat in STRATEGY_PRIORITY:
        sigs_by_strat[strat] = _order_candidates(sigs_by_strat[strat], strength, tv_map, strat)

    n_placed = 0
    remaining_dep = buy_budget
    placed_per_strat = {k: 0 for k in STRATEGY_PRIORITY}

    for strat in STRATEGY_PRIORITY:
        if n_placed >= total_avail:   # 전역 동시보유 상한(레거시 반영) 도달 — 과다매수 방지(2026-06-29)
            break
        avail = slot_avail[strat]
        if avail <= 0:
            continue
        strat_sigs = sigs_by_strat.get(strat, [])
        print(f"\n  [{strat}] 빈슬롯 {avail}개  후보 {len(strat_sigs)}건  stop {STRATEGY_STOP[strat]*100:.0f}%")

        placed_this = 0
        for sig in strat_sigs:
            # 전략별 빈슬롯 OR 전역 가용슬롯(레거시 반영) 중 먼저 소진되면 중단
            if placed_this >= avail or n_placed >= total_avail:
                break
            code = str(sig["code"]).zfill(6)
            name = str(sig.get("name", ""))
            close = float(sig.get("entry_price_close", 0) or 0)

            if code in positions or code in already:
                print(f"    [skip] {code} {name} — 이미 보유/주문됨")
                continue

            # 강도 필터(2026-07-21 신설) — 주문 직전 최종 게이트. verify_strength 가 이미
            # 무기록을 재계산했으므로 여기서 None 이면 확인 불가로 차단(fail-closed).
            _sc = strength.get((code, str(sig.get("strategy", strat))))
            if _sc is None:
                print(f"    [skip] {code} {name} — 강도 확인 불가(재계산 실패) → 차단")
                continue
            if _sc < MIN_STRENGTH_SCORE:
                print(f"    [skip] {code} {name} — 강도 {_sc:.2f} < {MIN_STRENGTH_SCORE}")
                continue
            # NaN 방어: 'close <= 0' 은 NaN 에서 False 라 통과 후 int(NaN) 크래시로
            # cmd_buy 전체가 죽음 — 'not (close > 0)' 은 NaN 도 걸러냄(2026-07-10)
            if not (close > 0):
                continue

            # placed_per_strat[k] 가 이미 현재 전략 포함 모든 배정 수를 추적하므로
            # placed_this 이중 차감 불필요 (버그 수정 2026-06-18)
            total_rem = sum(
                max(0, slot_avail[k] - placed_per_strat[k])
                for k in STRATEGY_PRIORITY
            )
            if total_rem <= 0:
                break
            budget = remaining_dep / total_rem
            qty = int((budget * 0.97) // close)
            if qty < 1 or qty * close < MIN_ORDER_AMOUNT:
                print(f"    [skip] {code} {name} — 예산 부족 ({budget:,.0f}원)")
                continue

            # 주문 직전 잔고 충분성 최종 확인(2026-07-21, 키움과 동일 규약)
            _need = qty * close
            if _need > remaining_dep:
                print(f"    [skip] {code} {name} — 잔고 부족(필요 {_need:,.0f} > 잔여 {remaining_dep:,.0f})")
                continue
            print(f"    [verify] {code} {name} — 강도 {_sc:.2f} ≥ {MIN_STRENGTH_SCORE} / "
                  f"잔여 {remaining_dep:,.0f} ≥ 주문 {_need:,.0f} → 주문 실행")

            try:
                ono = client.order_buy(code, qty)
                print(f"    [매수주문] {code} {name} {qty}주 @ {int(close):,} → {ono}")
                try:
                    import notifier
                    notifier.queue_fill("buy", name, code, qty, close)
                except Exception:
                    pass
                log_order({
                    "time": datetime.now().strftime("%H:%M:%S"), "side": "buy",
                    "code": code, "name": name, "strategy": strat,
                    "qty": qty, "price": int(close),
                    "order_type": "시장가(VTTC0802U)", "reason": "signal",
                    "ok": True, "order_no": ono, "msg": "",
                })
                # 진입가 기록
                save_kis_position(
                    code=code,
                    entry_px=close,
                    strategy=strat,
                    signal_date=str(sig.get("signal_date", "")),
                    holding_days=_to_int(sig.get("holding_days"), HOLDING_DAYS_DEFAULT),
                )
                remaining_dep -= qty * close
                n_placed += 1
                placed_this += 1
                placed_per_strat[strat] += 1
                already.add(code)
            except Exception as e:
                print(f"    [실패] {code} {name}: {e}")
                if isinstance(e, AccountBlocked):
                    _alert_account_blocked("신규 매수", str(e)[:160])
                log_order({
                    "time": datetime.now().strftime("%H:%M:%S"), "side": "buy",
                    "code": code, "name": name, "strategy": strat,
                    "qty": qty, "price": int(close),
                    "order_type": "시장가(VTTC0802U)", "reason": "signal",
                    "ok": False, "order_no": "", "msg": str(e)[:200],
                })
            time.sleep(0.6)   # 주문 간 간격 — 초당한도 예방(2026-07-14)

    print(f"\n[buy] 주문 {n_placed}건 완료")


def cmd_sell(reasons=("expire", "stop")):
    """만기 or stop 발동 종목 매도. reasons 로 사유 한정(am=stop만/pm=expire만, 2026-07-17).

    stop 판단: 전일 종가 <= 진입가 x (1 + stop_pct)
    """
    close_map = _today_close_map()
    due = codes_due_for_exit(close_map, reasons=reasons)
    sold_ok = set()   # 매도주문 성공 코드 — daily 가 잔고반영 대기에 사용(2026-07-10)
    if not due:
        print(f"[sell] 청산 대상 없음({'+'.join(reasons)}) — 종료")
        return sold_ok

    client = KISMockClient()
    # 만기 청산은 마감 동시호가(15:20~15:30) 안에서 끝내면 되므로 시간 여유가 있다.
    # 손절(AM)은 시가 근처가 목적이라 종전대로 3회.
    # 만기청산은 마감시한(15:30)이 있다 — 짧게 여러 번보다 길게 몇 번이 낫다.
    # (2026-09-04: 15s x 6 → 40s x 3. 총 예산 유지, 실제 대기시간은 90→120초)
    _expire = "expire" in reasons
    _bal_tries = 3
    _, positions = client.get_balance(
        attempts=_bal_tries,
        timeout=(BALANCE_TIMEOUT_EXPIRE if _expire else BALANCE_TIMEOUT))
    already = today_ordered_codes("sell")
    bought_today = today_ordered_codes("buy")
    strategy_map = get_signal_strategy_map()
    for _c, _v in load_kis_positions().items():   # 매수 당시 전략 우선(로그 정확성)
        if _v.get("strategy"):
            strategy_map[_c] = _v["strategy"]

    # 옛 신호 만기와 '오늘 새 신호 매수'가 같은 코드에 겹치면 코드 전량 매도가 오늘 산
    # 물량까지 당일 청산 → 당일 매수 코드는 만기/손절 매도 보류(2026-07-06, 키움 동일).
    targets = (set(due.keys()) & set(positions.keys())) - already - bought_today
    if set(due.keys()) & bought_today:
        print(f"[sell] 당일 매수 코드 청산 보류: {sorted(set(due.keys()) & bought_today)}")
    expire_n = sum(1 for c in targets if due.get(c) == "expire")
    stop_n   = sum(1 for c in targets if due.get(c) == "stop")
    print(f"[sell] 청산 대상: 만기 {expire_n}건  stop발동 {stop_n}건  실제보유 {len(targets)}건")

    n_placed = 0
    failures = []      # [(code, name, reason, msg)] — 청산 실패는 리스크 사건이라 집계
    for code in sorted(targets):
        qty    = positions[code]["qty"]
        name   = positions[code]["name"]
        strat  = strategy_map.get(str(code).zfill(6), "")
        reason = due.get(code, "expire")
        _cp = close_map.get(code, 0)
        ref_px = int(_cp) if (_cp and _cp == _cp) else 0   # NaN(자기비교 False) 크래시 방어
        try:
            ono = client.order_sell(code, qty)
            print(f"  [매도주문] {code} {name} {qty}주 ref:{ref_px:,}  사유:{reason} → {ono}")
            try:
                import notifier
                notifier.queue_fill("sell", name, code, qty, ref_px)
            except Exception:
                pass
            log_order({
                "time": datetime.now().strftime("%H:%M:%S"), "side": "sell",
                "code": code, "name": name, "strategy": strat,
                "qty": qty, "price": ref_px,
                "order_type": "시장가(VTTC0801U)", "reason": reason,
                "ok": True, "order_no": ono, "msg": "",
            })
            remove_kis_position(code)
            sold_ok.add(code)
            n_placed += 1
        except Exception as e:
            print(f"  [실패] {code} {name}: {e}")
            failures.append((code, name, reason, str(e)[:120]))
            if isinstance(e, AccountBlocked):
                _alert_account_blocked("만기/손절 청산", str(e)[:160])
            log_order({
                "time": datetime.now().strftime("%H:%M:%S"), "side": "sell",
                "code": code, "name": name, "strategy": strat,
                "qty": qty, "price": ref_px,
                "order_type": "시장가(VTTC0801U)", "reason": reason,
                "ok": False, "order_no": "", "msg": str(e)[:200],
            })
        time.sleep(0.6)   # 주문 간 간격 — 초당한도 예방(2026-07-14, 키움 07-13 429 실사례 계열)

    print(f"[sell] 주문 {n_placed}건 완료")
    # [2026-08-20] 청산 실패는 '보유가 계획 밖으로 연장되는' 리스크 사건이다.
    # 종전엔 [실패] 한 줄만 찍고 exit 0 이라 08-11~08-19 만기 청산 실패가 조용히
    # 반복됐다(한국알콜, 만기 초과 보유). 실패가 하나라도 있으면 반드시 알린다.
    if failures:
        _alert_sell_failures(failures)
    return sold_ok


def _alert_sell_failures(failures):
    """청산(만기·손절) 실패 통지. 계좌불가 경보가 이미 나갔으면 중복 발송하지 않는다."""
    lines = [f"  {c} {n} — {r} 실패: {m}" for c, n, r, m in failures[:5]]
    if len(failures) > 5:
        lines.append(f"  … 외 {len(failures) - 5}건")
    body = "\n".join(lines)
    print(f"⚠ [KIS] 청산 실패 {len(failures)}건 — 계획 밖 보유 연장")
    if getattr(_alert_account_blocked, "_sent", False):
        return   # 원인(계좌불가)을 이미 긴급으로 알림 — 같은 사건 반복 알림 방지
    try:
        import notifier
        notifier.safe_send(
            f"🚨 [KIS 안D] 청산 실패 {len(failures)}건 — 만기/손절이 집행되지 않았습니다\n"
            f"{body}\n  해당 종목은 계획보다 오래 보유 중입니다. 원인 확인 필요.")
    except Exception:
        pass


def _wait_positions_clear(codes, max_wait=30, interval=4):
    """매도주문 직후 잔고 API 가 체결을 반영해 codes 가 positions 에서 사라질 때까지 폴링.

    [2026-07-10] daily 가 sell 직후 무대기로 buy 를 호출하면, 방금 판 종목이 잔고에
    남아 보여 ① 슬롯이 해방 안 돼 당일 신규매수 누락(신호는 당일 한정이라 기회 영구
    소실) ② 매도대금 미반영 예산 과소 ③ 원장은 이미 삭제라 orphan 오탐 경고.
    타임아웃이어도 진행(최악은 종전과 동일한 보수적 동작)."""
    codes = {str(c).zfill(6) for c in (codes or set())}
    if not codes:
        return
    try:
        client = KISMockClient()
    except Exception:
        return
    waited = 0
    while waited < max_wait:
        try:
            _, positions = client.get_balance()
            remain = codes & set(positions.keys())
            if not remain:
                print(f"[daily] 매도 체결 잔고반영 확인({waited}s) — 슬롯 해방 {len(codes)}건")
                return
        except Exception:
            pass
        time.sleep(interval)
        waited += interval
    print(f"[daily] 매도 반영 대기 타임아웃({max_wait}s) — 미해방 슬롯은 다음날 회복")


def cmd_stop_check():
    """장중 손절 '모니터 전용'(매도 안 함). 15분마다 호출.

    ※ 2026-06-21: 장중-저가 기준 손절 재검증 결과, 안D 전략 2/3 에서 장중 손절이
       오히려 수익을 깎음(h52w_for3d_mkt Δ-0.48 / for_high20_mkt Δ-0.33).
       손절 이득은 '종가(EOD) 기준'일 때만 유효 → 실제 청산은 15:21 cmd_sell(종가기준)이
       담당하고, 이 함수는 자동매도하지 않는다. 대시보드 조기경보 표시용으로만 현황 기록.
    """
    client = KISMockClient()
    _, positions = client.get_balance()
    monitor = []
    if positions:
        kis_pos = load_kis_positions()
        strategy_map = get_signal_strategy_map()
        for code, p in positions.items():
            # 원장(매수 당시 전략) 우선 — 재신호로 전략이 바뀌면 stop_pct 를 잘못 고름(2026-07-07)
            strat = (kis_pos.get(str(code).zfill(6), {}).get("strategy")
                     or strategy_map.get(str(code).zfill(6), ""))
            stop_pct = STRATEGY_STOP.get(strat)
            if stop_pct is None:
                continue
            cur = float(p.get("price", 0) or 0)
            entry_px = float(kis_pos.get(code, {}).get("entry_px", 0) or 0)
            if entry_px <= 0:
                entry_px = float(p.get("avg_price", 0) or 0)
            if cur <= 0 or entry_px <= 0:
                continue
            pnl = cur / entry_px - 1
            room = (pnl - stop_pct) * 100   # 손절선까지 여유(%p). 0 이하면 EOD 청산 예정.
            monitor.append({
                "code": code, "name": p["name"], "pnl_pct": round(pnl * 100, 2),
                "stop_pct": round(stop_pct * 100, 1), "room_pp": round(room, 2),
                "imminent": room <= 3.0,
            })

    try:
        os.makedirs(ORDERS_DIR, exist_ok=True)
        with open(f"{ORDERS_DIR}/kis_stop_monitor.json", "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "items": sorted(monitor, key=lambda x: x["room_pp"])}, f, ensure_ascii=False)
    except Exception:
        pass
    reached = sum(1 for m in monitor if m["room_pp"] <= 0)
    print(f"[stop-monitor] 보유 {len(monitor)}건 점검 / 손절선 도달 {reached}건(청산은 EOD cmd_sell)")


def _wait_balance_settle(max_wait=20, interval=4):
    """매수/매도 직후 KIS 모의 잔고조회 API 가 체결을 즉시 반영하지 못해
    스냅샷이 pre-buy 로 박제되는 문제 방지. 우리 기록(kis_positions.csv)의
    보유종목이 API 잔고에 모두 잡힐 때까지 폴링(최대 max_wait 초).
    타임아웃이어도 그냥 진행(다음 status 가 보정). 아무 보유 기록 없으면 즉시 반환.
    """
    try:
        expected = set(load_kis_positions().keys())
    except Exception:
        expected = set()
    if not expected:
        return
    try:
        client = KISMockClient()
    except Exception:
        return
    waited = 0
    while waited < max_wait:
        try:
            _, positions = client.get_balance()
            if set(positions.keys()) >= expected:
                print(f"[daily] 잔고 체결 반영 확인({waited}s) — 보유 {len(positions)}건")
                return
        except Exception:
            pass
        time.sleep(interval)
        waited += interval
    print(f"[daily] 잔고 반영 대기 타임아웃({max_wait}s) — 다음 status 가 보정")


# ── 진입점 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    # 크래시 텔레그램(2026-07-17) — KIS 모의서버 장애(ReadTimeout/500)로 PM 만기매도가
    # '무경보 사망'한 실사례(15:21 ExitCode=1, 스케줄러 이력에만 남음). 어떤 미처리
    # 예외든 즉시 텔레그램으로 격상하고 원래 traceback/종료코드는 그대로 유지.
    def _crash_hook(_tp, _val, _tb):
        try:
            import notifier
            notifier.safe_send(f"🚨 [KIS] {cmd} 크래시: {_tp.__name__}: {_val}"
                               " — 이 시도는 중단. "
                               f"{_retry_note()}")
        except Exception:
            pass
        sys.__excepthook__(_tp, _val, _tb)
    sys.excepthook = _crash_hook

    # status 도 락 안에서 — 2026-07-10 prune/heal 도입으로 원장 '쓰기' 작업이 됐다.
    # 락 없이 daily 와 겹치면 load-modify-write 레이스로 방금 매수한 원장 행이 유실되거나
    # prune 삭제가 되살아날 수 있음.
    with kis_lock(timeout=60):
        if cmd == "status":
            cmd_status()
        elif cmd == "buy":
            cmd_buy()
        elif cmd == "sell":
            cmd_sell()
        elif cmd == "stopcheck":
            cmd_stop_check()
        elif cmd == "reconcile":
            cmd_reconcile()
        elif cmd == "daily":
            # 구(단일 트리거) 경로 — 하위호환. 만기+stop 둘 다 09:01에 발동(만기는 시가).
            # 작업 재등록(KisTraderAM/PM 분리) 후에는 daily-am/daily-pm 이 대신 쓰인다.
            print("[daily] 매도(만기/stop) 후 매수 — 구 단일 트리거 경로")
            _hour = datetime.now().hour
            if _hour >= 15 or _hour < 8:
                # [2026-07-17] 구 경로도 장외 가드 — 인자 없는 수동/정체불명 실행이
                # 10:51 에 이 경로로 점화된 실사례(정규 작업은 전부 am/pm 인자 사용).
                msg = f"⚠ [KIS] 구 daily 경로가 장외 시간({_hour}시)에 점화됨 — 전체 생략"
                print(msg)
                try:
                    import notifier
                    notifier.safe_send(msg)
                except Exception:
                    pass
            else:
                _sold = cmd_sell()
                if _sold:
                    # 매도 체결이 잔고에 반영될 때까지 대기 — 안 하면 슬롯/예산이 해방되지
                    # 않은 채 buy 가 돌아 당일 신규매수가 누락됨(2026-07-10)
                    _wait_positions_clear(_sold)
                cmd_buy()
                # 매수 직후 잔고API 체결반영 지연 대비 — 우리 기록이 잔고에 잡힐 때까지
                # 잠깐 대기 후 스냅샷 기록(대시보드가 당일 보유를 바로 반영).
                _wait_balance_settle()
                cmd_status()
        elif cmd == "daily-am":
            # 09:01 전용(2026-07-17 트리거 분리): stop 매도(전일 종가 판정→시가 매도,
            # 백테스트 의미론과 정합) + 매수. 만기는 15:21 daily-pm 담당(종가 청산 정합).
            print("[daily-am] 손절 매도 후 매수 (만기는 15:21 PM 담당)")
            _hour = datetime.now().hour
            if _hour >= 15 or _hour < 8:
                # [2026-07-17] 장외 재점화 가드 — 로그온 트리거/캐치업이 재부팅·심야에
                # AM 로직을 재점화한 실사례(12:36, 23:21). 15시 이후 손절판정·매수는
                # PM/익일 아침 소관이므로 전체 생략(장외 주문 잔여물 방지).
                msg = f"⚠ [KIS] daily-am 이 장외 시간({_hour}시)에 점화됨 — 전체 생략(재점화 가드)"
                print(msg)
                try:
                    import notifier
                    notifier.safe_send(msg)
                except Exception:
                    pass
            else:
                _sold = cmd_sell(reasons=("stop",))
                if _sold:
                    _wait_positions_clear(_sold)
                if _hour >= 12:
                    # StartWhenAvailable 지연복구가 정오를 넘긴 경우 — 한낮 시장가 매수는
                    # 가격 이동 위험이라 생략(키움 daily-am 과 동일 정책, 2026-07-12).
                    msg = "⚠ [KIS] 09:01 매수 트리거가 정오 이후 지연복구됨 — 당일 매수 생략(가격 이동 위험)"
                    print(msg)
                    try:
                        import notifier
                        notifier.safe_send(msg)
                    except Exception:
                        pass
                else:
                    cmd_buy()
                    _wait_balance_settle()
                cmd_status()
        elif cmd == "daily-pm":
            # 15:21 전용(2026-07-17 신설): 만기 매도만 — 마감 동시호가 체결 ≈ 종가 청산
            # = 백테스트 가정과 정합(종전엔 만기가 09:01 시가에 나가 갭 1회가 계통 노출).
            _hour = datetime.now().hour
            if _hour < 12:
                msg = "⚠ [KIS] 15:21 만기 트리거가 오전에 지연복구됨 — 만기 매도가 시가 체결됨(종가 아님)을 고지하고 진행"
                print(msg)
                try:
                    import notifier
                    notifier.safe_send(msg)
                except Exception:
                    pass
            print("[daily-pm] 만기 매도 (마감 동시호가 ≈ 종가 청산)")
            cmd_sell(reasons=("expire",))
            cmd_status()
            # watchdog 완료 마커 — '시작 마커'는 크래시를 못 잡음(07-17: 시작 직후
            # ReadTimeout 사망을 watchdog 이 [OK] 로 봤던 실사례). 끝까지 와야 찍힌다.
            print("[daily-pm] 완료")
        else:
            print(f"[main] 알 수 없는 명령: {cmd}")
            print("사용법: python kis_trader.py [status|buy|sell|stopcheck|reconcile|daily|daily-am|daily-pm]")
            sys.exit(1)

    # 체결 묶음 알림 — 이번 실행에서 쌓인 매수/매도를 1통으로 (없으면 전송 안 함)
    try:
        import notifier
        notifier.flush_fills("[KIS 안D]")
    except Exception:
        pass
