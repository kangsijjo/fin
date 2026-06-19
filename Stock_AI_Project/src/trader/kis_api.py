import requests
import os
import time
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from src.logger import get_logger

load_dotenv()
logger = get_logger('kis_api')

# 토큰 캐시 디렉터리 (DB 와 같은 data/ 폴더 사용)
_TOKEN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data'
)
# KIS 토큰은 발급 후 24시간 유효. 안전마진 1시간 두고 23시간 재사용.
_TOKEN_TTL_HOURS = 23

# 네트워크 안정성 파라미터.
# timeout 없는 requests 는 인터넷 끊김 시 '무한 대기(hang)' 가 되어
# 프로세스가 죽지 않고 멈춰버린다 (run_scheduler.bat 의 자동재시작도 못 잡음).
_HTTP_TIMEOUT = 10      # 초
_HTTP_RETRIES = 3       # 읽기 호출 재시도 횟수 (주문 호출은 0 으로 지정)

# 미국 거래소 코드 매핑: (시세용 EXCD, 주문/잔고용 OVRS_EXCG_CD)
# 티커의 상장 거래소를 모르므로 시세 조회를 순서대로 시도해서 식별한다.
_US_EXCHANGES = [('NAS', 'NASD'), ('NYS', 'NYSE'), ('AMS', 'AMEX')]

class KISTrader:
    def __init__(self, mock=True):
        self.mock = mock
        
        if mock:
            self.app_key    = os.getenv('KIS_MOCK_APP_KEY')
            self.app_secret = os.getenv('KIS_MOCK_APP_SECRET')
            self.account    = os.getenv('KIS_MOCK_ACCOUNT')
            self.base_url   = "https://openapivts.koreainvestment.com:29443"
        else:
            self.app_key    = os.getenv('KIS_APP_KEY')
            self.app_secret = os.getenv('KIS_APP_SECRET')
            self.account    = os.getenv('KIS_ACCOUNT')
            self.base_url   = "https://openapi.koreainvestment.com:9443"
        
        self.access_token = None
        # 직전 호출 실패 사유 (position_manager 의 orphan 감지 등에 사용)
        self.last_error_msg = None
        self.last_error_code = None
        # 미국 티커 → (EXCD, OVRS_EXCG_CD) 캐시 (프로세스 생애 동안 유지)
        self._us_exchange_cache = {}
        mode = "모의투자" if mock else "실전투자"
        logger.info(f"KIS Trader 초기화 ({mode})")

    def _token_cache_path(self):
        os.makedirs(_TOKEN_DIR, exist_ok=True)
        suffix = 'mock' if self.mock else 'real'
        return os.path.join(_TOKEN_DIR, f'kis_token_{suffix}.json')

    def _load_cached_token(self):
        """캐시된 토큰이 23시간 안에 발급된 거면 재사용."""
        path = self._token_cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            issued = datetime.fromisoformat(data['issued_at'])
            if datetime.now() - issued < timedelta(hours=_TOKEN_TTL_HOURS):
                return data['access_token']
        except Exception:
            return None
        return None

    def _save_cached_token(self, token):
        try:
            with open(self._token_cache_path(), 'w', encoding='utf-8') as f:
                json.dump({
                    'access_token': token,
                    'issued_at': datetime.now().isoformat(),
                }, f)
        except Exception as e:
            logger.warning(f"토큰 캐시 저장 실패(스킵): {e}")

    def get_token(self, force_refresh=False):
        """
        KIS Open API 의 토큰 발급은 1분당 1회 제한이 있다.
        파일 캐시(23시간)를 먼저 확인하고, 만료/강제재발급일 때만 API 호출.
        """
        if not force_refresh:
            cached = self._load_cached_token()
            if cached:
                self.access_token = cached
                logger.info("토큰 캐시 재사용 (만료 전)")
                return True

        url = f"{self.base_url}/oauth2/tokenP"
        headers = {"Content-Type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        # 토큰 발급은 읽기성(멱등) → 재시도 적용. 잠깐의 네트워크 끊김에
        # 하루치 작업이 통째로 날아가는 걸 막는다.
        data = self._http('POST', url, retries=_HTTP_RETRIES,
                           headers=headers, json=body)

        if data and 'access_token' in data:
            self.access_token = data['access_token']
            self._save_cached_token(self.access_token)
            logger.info("토큰 발급 완료!")
            return True
        else:
            logger.error(f"토큰 발급 실패: {data}")
            return False

    def get_headers(self, tr_id):
        return {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P"
        }

    def _http(self, method, url, retries=0, **kwargs):
        """timeout + 선택적 재시도 래퍼.

        - 모든 요청에 timeout 강제 (무한 hang 방지)
        - retries>0 이면 네트워크 예외 시 지수 백오프 재시도
        - retries=0 은 주문(buy/sell)처럼 멱등하지 않은 호출용:
          timeout 만 적용하고 재시도는 안 함 (재시도 = 중복 주문 위험)

        반환: 응답 JSON(dict). 완전 실패 시 None.
        """
        kwargs.setdefault('timeout', _HTTP_TIMEOUT)
        last_exc = None
        for attempt in range(retries + 1):
            try:
                if method == 'GET':
                    res = requests.get(url, **kwargs)
                else:
                    res = requests.post(url, **kwargs)
                return res.json()
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    wait = 1.5 ** attempt   # 1.0s, 1.5s, 2.25s ...
                    logger.warning(
                        f"HTTP {method} 실패 (시도 {attempt+1}/{retries+1}): {e} "
                        f"- {wait:.1f}s 후 재시도"
                    )
                    time.sleep(wait)
        logger.error(f"HTTP {method} 최종 실패: {url} - {last_exc}")
        return None

    def get_balance(self):
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "VTTC8434R" if self.mock else "TTTC8434R"
        headers = self.get_headers(tr_id)
        params = {
            "CANO": self.account,
            "ACNT_PRDT_CD": "01",
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "N",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        data = self._http('GET', url, retries=_HTTP_RETRIES,
                           headers=headers, params=params)

        if data and data.get('rt_cd') == '0':
            output2 = data.get('output2', [{}])[0]
            total_eval     = output2.get('tot_evlu_amt', '0')
            available_cash = output2.get('dnca_tot_amt', '0')
            logger.info(f"총 평가금액: {int(total_eval):,}원")
            logger.info(f"주문가능금액: {int(available_cash):,}원")
            return data
        else:
            # data 가 None(네트워크 완전 실패)일 수 있어 .get 직접 호출 금지
            msg = data.get('msg1') if data else '네트워크 실패'
            logger.error(f"잔고 조회 실패: {msg}")
            return None

    # ─────────────────────────────────────────────────────────────
    # 해외(미국) 주식: 시세 / 거래소 식별
    # ─────────────────────────────────────────────────────────────
    def _get_overseas_price_raw(self, ticker, excd):
        """단일 거래소(excd)에서 현재가 조회. 실패/미상장 시 None."""
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        headers = self.get_headers("HHDFS00000300")
        params = {"AUTH": "", "EXCD": excd, "SYMB": ticker}
        data = self._http('GET', url, retries=_HTTP_RETRIES,
                           headers=headers, params=params)
        if data and data.get('rt_cd') == '0':
            # 미상장 심볼도 rt_cd '0' 에 빈 last 로 오는 경우가 있어 값 검증 필수
            last = (data.get('output') or {}).get('last') or ''
            try:
                price = float(last)
            except (TypeError, ValueError):
                return None
            if price > 0:
                return price
        return None

    def _resolve_us_exchange(self, ticker):
        """티커의 상장 거래소 식별 (NAS→NYS→AMS 순 시세 조회 시도, 캐시).
        반환: (excd, ovrs_excg_cd, 현재가) 또는 (None, None, None)"""
        if ticker in self._us_exchange_cache:
            excd, excg = self._us_exchange_cache[ticker]
            return excd, excg, self._get_overseas_price_raw(ticker, excd)
        for excd, excg in _US_EXCHANGES:
            price = self._get_overseas_price_raw(ticker, excd)
            if price is not None:
                self._us_exchange_cache[ticker] = (excd, excg)
                return excd, excg, price
            time.sleep(0.2)
        return None, None, None

    def get_overseas_price(self, ticker):
        """미국 주식 현재가 (USD, float). 실패 시 None."""
        _, _, price = self._resolve_us_exchange(ticker)
        if price is None:
            logger.error(f"해외 현재가 조회 실패: {ticker}")
            return None
        logger.info(f"{ticker} 현재가: ${price:,.2f}")
        time.sleep(0.3)
        return price

    def get_current_price(self, ticker, market='korea'):
        """현재가 조회. market='usa' 면 해외 시세 API 로 라우팅 (USD, float),
        국내는 기존과 동일 (KRW, int)."""
        if market == 'usa':
            return self.get_overseas_price(ticker)
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = self.get_headers("FHKST01010100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker
        }
        data = self._http('GET', url, retries=_HTTP_RETRIES,
                           headers=headers, params=params)

        if data and data.get('rt_cd') == '0':
            price = int(data['output']['stck_prpr'])
            logger.info(f"{ticker} 현재가: {price:,}원")
            time.sleep(0.3)
            return price
        else:
            msg = data.get('msg1') if data else '네트워크 실패'
            logger.error(f"현재가 조회 실패: {msg}")
            return None

    def _order_overseas(self, side, ticker, quantity, price=None):
        """미국 주식 주문 (지정가 — KIS 해외주식은 시장가 미지원, 모의 기준).
        side: 'buy' | 'sell'"""
        excd, excg, cur_price = self._resolve_us_exchange(ticker)
        if excg is None:
            self.last_error_msg = f'미국 거래소 식별 실패: {ticker}'
            self.last_error_code = None
            logger.error(self.last_error_msg)
            return False
        if price is None:
            price = cur_price
        if price is None or price <= 0:
            self.last_error_msg = f'해외 현재가 조회 실패: {ticker}'
            self.last_error_code = None
            logger.error(self.last_error_msg)
            return False

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        if side == 'buy':
            tr_id = "VTTT1002U" if self.mock else "TTTT1002U"
        else:
            tr_id = "VTTT1001U" if self.mock else "TTTT1006U"
        headers = self.get_headers(tr_id)
        body = {
            "CANO": self.account,
            "ACNT_PRDT_CD": "01",
            "OVRS_EXCG_CD": excg,
            "PDNO": ticker,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
        }
        if side == 'sell':
            body["SLL_TYPE"] = "00"   # 제도매도

        # 주문은 멱등하지 않음 → retries=0
        data = self._http('POST', url, retries=0, headers=headers, json=body)

        label = '매수' if side == 'buy' else '매도'
        if data and data.get('rt_cd') == '0':
            self.last_error_msg = None
            self.last_error_code = None
            logger.info(f"해외 {label} 완료: {ticker} {quantity}주 @ ${price:,.2f}")
            return True
        self.last_error_msg = data.get('msg1') if data else '네트워크 실패(주문 도달 불명)'
        self.last_error_code = data.get('msg_cd') if data else None
        logger.error(f"해외 {label} 실패: {self.last_error_msg}")
        return False

    def buy(self, ticker, quantity, price=None, market='korea'):
        """매수 주문 (국내: 지정가 / 미국: 지정가, market='usa' 라우팅)"""
        if market == 'usa':
            return self._order_overseas('buy', ticker, quantity, price)

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = "VTTC0802U" if self.mock else "TTTC0802U"
        headers = self.get_headers(tr_id)

        if price is None:
            price = self.get_current_price(ticker)
            if price is None:
                logger.error(f"현재가 조회 실패: {ticker}")
                return False

        body = {
            "CANO": self.account,
            "ACNT_PRDT_CD": "01",
            "PDNO": ticker,
            "ORD_DVSN": "00",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price)
        }
        # 주문은 멱등하지 않음 → retries=0 (timeout 만 적용).
        # 재시도하면 같은 주문이 두 번 체결될 수 있다.
        data = self._http('POST', url, retries=0, headers=headers, json=body)

        if data and data.get('rt_cd') == '0':
            self.last_error_msg = None
            self.last_error_code = None
            logger.info(f"매수 완료: {ticker} {quantity}주 @ {price:,}원")
            return True
        else:
            self.last_error_msg = data.get('msg1') if data else '네트워크 실패(주문 도달 불명)'
            self.last_error_code = data.get('msg_cd') if data else None
            logger.error(f"매수 실패: {self.last_error_msg}")
            return False

    def sell(self, ticker, quantity, price=None, market='korea', market_order=False):
        """매도 주문.

        market_order=True (국내 전용): 시장가(ORD_DVSN '01') 주문.
        손절/시간청산처럼 '반드시 나가야 하는' 매도는 지정가 미체결 위험을
        피하기 위해 시장가 사용을 권장. 미국은 KIS 가 시장가를 지원하지 않아
        지정가(현재가)로 동작하며 market_order 인자는 무시된다.
        """
        if market == 'usa':
            return self._order_overseas('sell', ticker, quantity, price)

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = "VTTC0801U" if self.mock else "TTTC0801U"
        headers = self.get_headers(tr_id)

        if market_order:
            ord_dvsn, ord_unpr = "01", "0"   # 시장가는 가격 0
        else:
            if price is None:
                price = self.get_current_price(ticker)
                if price is None:
                    logger.error(f"현재가 조회 실패: {ticker}")
                    return False
            ord_dvsn, ord_unpr = "00", str(price)

        body = {
            "CANO": self.account,
            "ACNT_PRDT_CD": "01",
            "PDNO": ticker,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": ord_unpr
        }
        # 주문은 멱등하지 않음 → retries=0 (timeout 만 적용).
        data = self._http('POST', url, retries=0, headers=headers, json=body)

        if data and data.get('rt_cd') == '0':
            self.last_error_msg = None
            self.last_error_code = None
            price_str = '시장가' if market_order else f"{price:,}원"
            logger.info(f"매도 완료: {ticker} {quantity}주 @ {price_str}")
            return True
        else:
            self.last_error_msg = data.get('msg1') if data else '네트워크 실패(주문 도달 불명)'
            self.last_error_code = data.get('msg_cd') if data else None
            logger.error(f"매도 실패: {self.last_error_msg}")
            return False

    def get_holdings(self, market='korea'):
        """KIS 계좌의 실제 보유종목 코드 집합 반환.
        positions 테이블과 KIS 잔고의 sync 검증에 사용.
        실패 시 None 반환 (sync 판정을 미루기 위함; 빈 set 와 구분)."""
        if market == 'usa':
            return self.get_overseas_holdings()
        data = self.get_balance()
        if data is None:
            return None
        try:
            output1 = data.get('output1', []) or []
            held = set()
            for h in output1:
                qty = int(h.get('hldg_qty', 0) or 0)
                code = h.get('pdno')
                if code and qty > 0:
                    held.add(code)
            return held
        except Exception as e:
            logger.warning(f"보유종목 파싱 실패: {e}")
            return None

    def get_overseas_holdings(self):
        """미국 계좌 보유종목 코드 집합. 전체 실패 시 None.
        모의투자는 OVRS_EXCG_CD='NASD' 가 미국 전체를 의미하고,
        실전은 거래소별로 분리 조회가 필요하다."""
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        tr_id = "VTTS3012R" if self.mock else "TTTS3012R"
        excgs = ["NASD"] if self.mock else ["NASD", "NYSE", "AMEX"]
        held = set()
        any_ok = False
        for excg in excgs:
            headers = self.get_headers(tr_id)
            params = {
                "CANO": self.account,
                "ACNT_PRDT_CD": "01",
                "OVRS_EXCG_CD": excg,
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            }
            data = self._http('GET', url, retries=_HTTP_RETRIES,
                               headers=headers, params=params)
            if data and data.get('rt_cd') == '0':
                any_ok = True
                for h in (data.get('output1') or []):
                    try:
                        qty = float(h.get('ovrs_cblc_qty', 0) or 0)
                    except (TypeError, ValueError):
                        qty = 0
                    code = h.get('ovrs_pdno')
                    if code and qty > 0:
                        held.add(code)
            else:
                msg = data.get('msg1') if data else '네트워크 실패'
                logger.warning(f"해외 잔고 조회 실패 ({excg}): {msg}")
            time.sleep(0.3)
        return held if any_ok else None

    def get_orderable_cash(self, ticker, price, market='korea'):
        """해당 종목/가격 기준 주문가능현금 조회. 실패 시 None.

        국내: 미수없는 매수금액(nrcvb_buy_amt, KRW).
        예수금(dnca_tot_amt)은 D+2 미정산 금액이 포함될 수 있어
        연속 매수 시 같은 현금을 중복으로 잡는 문제가 있음 → 이 TR 이 정확.
        미국: 외화 주문가능금액(ord_psbl_frcr_amt, USD).
        """
        if market == 'usa':
            _, excg, _ = self._resolve_us_exchange(ticker)
            if excg is None:
                logger.error(f"해외 매수가능금액: 거래소 식별 실패 {ticker}")
                return None
            url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
            tr_id = "VTTS3007R" if self.mock else "TTTS3007R"
            headers = self.get_headers(tr_id)
            params = {
                "CANO": self.account,
                "ACNT_PRDT_CD": "01",
                "OVRS_EXCG_CD": excg,
                "OVRS_ORD_UNPR": f"{price:.2f}",
                "ITEM_CD": ticker,
            }
            data = self._http('GET', url, retries=_HTTP_RETRIES,
                               headers=headers, params=params)
            if data and data.get('rt_cd') == '0':
                out = data.get('output') or {}
                try:
                    cash = float(out.get('ord_psbl_frcr_amt', '0') or 0)
                except (TypeError, ValueError):
                    return None
                logger.info(f"해외 주문가능금액: ${cash:,.2f}")
                return cash
            msg = data.get('msg1') if data else '네트워크 실패'
            logger.error(f"해외 매수가능금액 조회 실패 {ticker}: {msg}")
            return None

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        tr_id = "VTTC8908R" if self.mock else "TTTC8908R"
        headers = self.get_headers(tr_id)
        params = {
            "CANO": self.account,
            "ACNT_PRDT_CD": "01",
            "PDNO": ticker,
            "ORD_UNPR": str(int(price)),
            "ORD_DVSN": "00",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        data = self._http('GET', url, retries=_HTTP_RETRIES,
                           headers=headers, params=params)
        if data and data.get('rt_cd') == '0':
            out = data.get('output') or {}
            try:
                cash = int(out.get('nrcvb_buy_amt', '0') or 0)
            except (TypeError, ValueError):
                return None
            logger.info(f"주문가능현금(미수없음): {cash:,}원")
            return cash
        msg = data.get('msg1') if data else '네트워크 실패'
        logger.error(f"매수가능금액 조회 실패 {ticker}: {msg}")
        return None

    # ─────────────────────────────────────────────────────────────
    # 시장 스캐너용: 등락률 / 거래량 상위 종목 조회
    # 모의/실전 모두 지원되는 시세성 API (분당 호출 한도 안에서 사용).
    # ─────────────────────────────────────────────────────────────
    def get_top_change_rate(self, count=30, asc=False):
        """
        등락률 순위 조회.
        asc=False → 상승률 상위, True → 하락률 상위.
        반환: 리스트(dict) — 종목코드, 종목명, 현재가, 등락률, 누적거래량 등 포함.
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/ranking/fluctuation"
        headers = self.get_headers("FHPST01700000")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20170",
            "FID_INPUT_ISCD": "0000",
            "FID_RANK_SORT_CLS_CODE": "1" if asc else "0",
            "FID_INPUT_CNT_1": "0",
            "FID_PRC_CLS_CODE": "0",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXLS_CLS_CODE": "0",
            "FID_DIV_CLS_CODE": "0",
            "FID_RSFL_RATE1": "",
            "FID_RSFL_RATE2": "",
        }
        data = self._http('GET', url, retries=_HTTP_RETRIES,
                           headers=headers, params=params)
        if data and data.get('rt_cd') == '0':
            return data.get('output', [])[:count]
        if data:
            logger.error(f"등락률 상위 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
        return []

    def get_investor_daily(self, ticker):
        """
        종목별 일자별 투자자별 매매동향 (최근 30 거래일).
        TR_ID: FHKST01010900
        반환: 리스트(dict) — stck_bsop_date, frgn_ntby_tr_pbmn(외국인 순매수금액),
              orgn_ntby_tr_pbmn(기관 순매수금액) 등 포함.
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
        headers = self.get_headers("FHKST01010900")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        data = self._http('GET', url, retries=_HTTP_RETRIES,
                           headers=headers, params=params)
        if data and data.get('rt_cd') == '0':
            return data.get('output', [])
        if data:
            logger.error(f"투자자별 매매 조회 실패 {ticker}: {data.get('msg1')}")
        return []

    def get_top_volume(self, count=30):
        """거래량 상위. 반환: 리스트(dict)."""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"
        headers = self.get_headers("FHPST01710000")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0",
            "FID_INPUT_DATE_1": "0",
        }
        data = self._http('GET', url, retries=_HTTP_RETRIES,
                           headers=headers, params=params)
        if data and data.get('rt_cd') == '0':
            return data.get('output', [])[:count]
        if data:
            logger.error(f"거래량 상위 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
        return []


if __name__ == "__main__":
    trader = KISTrader(mock=True)
    trader.get_token()
    trader.get_balance()