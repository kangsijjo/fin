"""
키움증권 REST API 클라이언트 (데이터 수집 전용 — 주문 기능 없음).

용도
----
KIS API 가 제공하지 못하는 데이터를 수집한다:
  - ka10059  종목별 일별 투자자(외국인/기관/개인) 수급 — 과거 무제한 (KIS 는 30일 한계)
  - ka10013  신용매매동향 (신용잔고/잔고율)
  - ka20068  종목별 대차거래추이 (대차잔고 — 공매도 프록시)

.env 설정 (사용자 정의 키 이름 그대로 사용)
------------------------------------------
  KIWOOM_ENV=mock | prod
  KIWOOM_MOCK_APP_KEY / KIWOOM_MOCK_APP_SECRET
  KIWOOM_PROD_APP_KEY / KIWOOM_PROD_APP_SECRET

주의
----
- 모의(mockapi.kiwoom.com)는 시세/수급성 TR 을 지원하지 않을 수 있다.
  return_code != 0 에 'TR 미지원' 류 메시지가 나오면 KIWOOM_ENV=prod 로 전환.
  (데이터 조회는 주문이 아니므로 실전 키로 호출해도 안전)
- pip 패키지 kiwoom-rest-api 는 설치되어 있어도 사용하지 않는다.
  이유: (1) 자격증명을 다른 env 변수명(KIWOOM_API_KEY)에서 import 시점에 읽고,
       (2) 연속조회 키(cont-yn/next-key)가 응답 '헤더' 로 오는데 패키지가 이를 버림.
  대신 kis_api.py 와 같은 직접 requests 패턴 (timeout/재시도/로깅) 사용.
"""
import os
import json
import time
import requests
from datetime import datetime
from dotenv import load_dotenv

from src.logger import get_logger

load_dotenv()
logger = get_logger('kiwoom_api')

_HTTP_TIMEOUT = 10
_HTTP_RETRIES = 3

# 호출 간 최소 간격 (클라이언트 측 속도 제한).
# 모의서버는 허용량이 매우 빡빡해(오류 1700) 넉넉하게, 실전은 여유 있음.
_MIN_INTERVAL = {'mock': 1.2, 'prod': 0.3}
# '허용된 요청 개수 초과'(1700) 시 백오프 재시도 횟수
_RATE_LIMIT_RETRIES = 4

# 토큰 캐시 (KIS 와 동일하게 data/ 폴더)
_TOKEN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data'
)

_BASE_URLS = {
    'prod': 'https://api.kiwoom.com',
    'mock': 'https://mockapi.kiwoom.com',
}

# TR별 리소스 경로
_RESOURCE = {
    'ka10059': '/api/dostk/stkinfo',   # 종목별투자자기관별
    'ka10013': '/api/dostk/stkinfo',   # 신용매매동향
    'ka20068': '/api/dostk/slb',       # 종목별 대차거래추이
}


def weekend_guard(force=False):
    """키움 API 는 주말(토/일)에만 사용 (2026-06-12 정책).

    키움 API 키를 다른 시스템과 공유 중 → 주중 사용 시 토큰 재발급이
    상대 토큰을 무효화하거나 호출 한도를 나눠 먹는 충돌 위험.
    반환: 실행해도 되면 True. 주중이면 False (force=True 로 강제 가능).
    """
    if datetime.now().weekday() >= 5 or force:   # 5=토, 6=일
        return True
    logger.error(
        "키움 API 는 주말에만 사용하도록 설정되어 있습니다 (다른 시스템과 키 공유). "
        "주중 실행이 꼭 필요하면 --force 옵션을 사용하세요.")
    return False


class KiwoomClient:
    def __init__(self, env=None):
        self.env = (env or os.getenv('KIWOOM_ENV', 'mock')).lower()
        if self.env not in _BASE_URLS:
            raise ValueError(f"KIWOOM_ENV 는 mock|prod 여야 함: {self.env}")
        prefix = 'KIWOOM_MOCK' if self.env == 'mock' else 'KIWOOM_PROD'
        self.app_key    = os.getenv(f'{prefix}_APP_KEY')
        self.app_secret = os.getenv(f'{prefix}_APP_SECRET')
        self.base_url   = _BASE_URLS[self.env]
        self.access_token = None
        self.last_error_msg = None
        self._last_call_ts = 0.0   # 클라이언트 측 속도 제한용
        if not self.app_key or not self.app_secret:
            logger.warning(f".env 에 {prefix}_APP_KEY/SECRET 미설정")
        logger.info(f"Kiwoom 클라이언트 초기화 ({self.env})")

    # ── 토큰 ─────────────────────────────────────────────
    def _token_cache_path(self):
        os.makedirs(_TOKEN_DIR, exist_ok=True)
        return os.path.join(_TOKEN_DIR, f'kiwoom_token_{self.env}.json')

    def _load_cached_token(self):
        path = self._token_cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # expires_dt: YYYYMMDDHHMMSS (키움이 응답으로 주는 만료시각)
            expires = datetime.strptime(data['expires_dt'], '%Y%m%d%H%M%S')
            if datetime.now() < expires:
                return data['token']
        except Exception:
            return None
        return None

    def get_token(self, force_refresh=False):
        if not force_refresh:
            cached = self._load_cached_token()
            if cached:
                self.access_token = cached
                logger.info("키움 토큰 캐시 재사용")
                return True

        url = f"{self.base_url}/oauth2/token"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }
        data = self._http_raw('POST', url,
                              headers={"Content-Type": "application/json;charset=UTF-8"},
                              json=body, retries=_HTTP_RETRIES)
        if data is None:
            logger.error("키움 토큰 발급 실패 (네트워크)")
            return False
        body_json = data[0]
        token = body_json.get('token') or body_json.get('access_token')
        if not token:
            logger.error(f"키움 토큰 발급 실패: {body_json}")
            return False
        self.access_token = token
        try:
            with open(self._token_cache_path(), 'w', encoding='utf-8') as f:
                json.dump({
                    'token': token,
                    # expires_dt 없으면 23시간 후로 보수적 설정
                    'expires_dt': body_json.get(
                        'expires_dt',
                        datetime.now().strftime('%Y%m%d') + '235959'),
                }, f)
        except Exception as e:
            logger.warning(f"키움 토큰 캐시 저장 실패(스킵): {e}")
        logger.info("키움 토큰 발급 완료")
        return True

    # ── HTTP ─────────────────────────────────────────────
    def _http_raw(self, method, url, retries=0, **kwargs):
        """반환: (body_json, response_headers) 또는 None.
        연속조회 키가 응답 '헤더'(cont-yn, next-key)로 오므로 헤더도 함께 반환."""
        kwargs.setdefault('timeout', _HTTP_TIMEOUT)
        last_exc = None
        for attempt in range(retries + 1):
            try:
                res = requests.request(method, url, **kwargs)
                return res.json(), res.headers
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    wait = 1.5 ** attempt
                    logger.warning(f"Kiwoom HTTP 실패 (시도 {attempt+1}): {e} - {wait:.1f}s 후 재시도")
                    time.sleep(wait)
        logger.error(f"Kiwoom HTTP 최종 실패: {url} - {last_exc}")
        return None

    def _throttle(self):
        """호출 간 최소 간격 보장 (rate limit 사전 예방)."""
        min_iv = _MIN_INTERVAL.get(self.env, 1.0)
        wait = self._last_call_ts + min_iv - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call_ts = time.time()

    def call(self, api_id, body, cont_yn='N', next_key=''):
        """단일 TR 호출. 호출 간격 자동 조절 + 허용량 초과(1700) 시 백오프 재시도.
        반환: (body_json, cont_yn, next_key) — 실패 시 (None, 'N', '')"""
        resource = _RESOURCE.get(api_id)
        if resource is None:
            raise ValueError(f"등록되지 않은 api_id: {api_id}")
        url = f"{self.base_url}{resource}"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": api_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
        }

        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            self._throttle()
            result = self._http_raw('POST', url, retries=_HTTP_RETRIES,
                                    headers=headers, json=body)
            if result is None:
                self.last_error_msg = '네트워크 실패'
                return None, 'N', ''
            data, res_headers = result
            rc = data.get('return_code')
            if rc in (0, '0', None):
                self.last_error_msg = None
                return (data,
                        res_headers.get('cont-yn', 'N'),
                        res_headers.get('next-key', ''))

            msg = data.get('return_msg', '') or ''
            # 허용량 초과(1700) → 지수 백오프 후 재시도.
            # 계속 밀어붙이면 모의서버가 연결 자체를 끊으므로 (timeout/reset)
            # 넉넉히 기다리는 게 전체 처리량에 오히려 이득.
            if '허용된 요청' in msg or '1700' in msg:
                if attempt < _RATE_LIMIT_RETRIES:
                    wait = 5.0 * (2 ** attempt)   # 5, 10, 20, 40초
                    logger.warning(
                        f"{api_id} 허용량 초과 - {wait:.0f}s 대기 후 재시도 "
                        f"({attempt+1}/{_RATE_LIMIT_RETRIES})")
                    time.sleep(wait)
                    continue
                self.last_error_msg = msg
                logger.error(f"{api_id} 허용량 초과 지속 - 재시도 소진"
                             + (" (모의서버 한도가 매우 낮음 → KIWOOM_ENV=prod 권장)"
                                if self.env == 'mock' else ""))
                return None, 'N', ''

            self.last_error_msg = msg
            logger.error(f"{api_id} 실패: rc={rc} {msg}"
                         + (" (모의는 이 TR 미지원일 수 있음 → KIWOOM_ENV=prod)"
                            if self.env == 'mock' else ""))
            return None, 'N', ''

        return None, 'N', ''

    # ── TR 래퍼 ──────────────────────────────────────────
    def investor_daily(self, ticker, date=None, cont_yn='N', next_key=''):
        """ka10059 종목별 일별 투자자 수급 (금액, 순매수, 1페이지).
        date(YYYYMMDD) 기준 과거 방향으로 일별 rows 반환.
        반환: (rows, cont_yn, next_key) — rows 필드: dt, frgnr_invsr(외국인),
        orgn(기관), ind_invsr(개인) 등. 단위: 금액(amt_qty_tp=1) 기준 키움 고유 단위
        → 호출 측에서 KIS 단위와 보정 필요 (backfill_supply_kiwoom.py 참조).
        """
        date = date or datetime.now().strftime('%Y%m%d')
        body = {
            "dt": date,
            "stk_cd": ticker,
            "amt_qty_tp": "1",   # 1: 금액
            "trde_tp": "0",      # 0: 순매수
            "unit_tp": "1000",   # 수량 단위 (금액 모드에선 영향 적음)
        }
        data, cy, nk = self.call('ka10059', body, cont_yn, next_key)
        rows = (data or {}).get('stk_invsr_orgn') or []
        return rows, cy, nk

    def credit_trend(self, ticker, date=None, cont_yn='N', next_key=''):
        """ka10013 신용매매동향 (융자, 일별).
        반환: (rows, cont_yn, next_key) — rows 필드: dt, remn(신용잔고),
        remn_rt(잔고율 %), shr_rt 등."""
        date = date or datetime.now().strftime('%Y%m%d')
        body = {
            "stk_cd": ticker,
            "dt": date,
            "qry_tp": "1",   # 1: 융자
        }
        data, cy, nk = self.call('ka10013', body, cont_yn, next_key)
        rows = (data or {}).get('crd_trde_trend') or []
        return rows, cy, nk

    def lending_trend(self, ticker, start_date='', end_date='', cont_yn='N', next_key=''):
        """ka20068 종목별 대차거래추이.
        반환: (rows, cont_yn, next_key) — rows 필드: dt, rmnd(대차잔고주수),
        remn_amt(잔고금액), dbrt_trde_cntrcnt(체결주수) 등."""
        body = {
            "stk_cd": ticker,
            "strt_dt": start_date,
            "end_dt": end_date,
            "all_tp": "0",
        }
        data, cy, nk = self.call('ka20068', body, cont_yn, next_key)
        rows = (data or {}).get('dbrt_trde_trnsn') or []
        return rows, cy, nk


if __name__ == "__main__":
    # 연결 테스트: 삼성전자 수급 1페이지
    c = KiwoomClient()
    if c.get_token():
        rows, cy, nk = c.investor_daily('005930')
        print(f"rows={len(rows)} cont={cy}")
        for r in rows[:3]:
            print(r.get('dt'), '외인', r.get('frgnr_invsr'), '기관', r.get('orgn'))
