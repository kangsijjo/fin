"""
한국투자증권 KIS API 클라이언트 (백테스트용 데이터 조회 전용)

주요 기능:
- 액세스 토큰 발급 (24시간 캐시)
- 거래대금 상위 종목 조회 (실시간 순위)
- 일자별/분봉 시세 조회
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta

from config import KIS_APP_KEY, KIS_APP_SECRET, KIS_ENV

URLS = {
    "prod": "https://openapi.koreainvestment.com:9443",
    "vps": "https://openapivts.koreainvestment.com:29443",
}
BASE_URL = URLS[KIS_ENV]

TOKEN_CACHE_FILE = ".kis_token.json"

def _request_with_retry(method, url, *, headers=None, params=None, data=None,
                        timeout=10, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, headers=headers,
                                    params=params, data=data, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                raise
            if attempt < max_retries:
                time.sleep(1.0 * (2 ** attempt))
                continue
            raise

def get_access_token():
    if os.path.exists(TOKEN_CACHE_FILE):
        try:
            with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            expires_at = datetime.fromisoformat(cache["expires_at"])
            if datetime.now() < expires_at - timedelta(hours=1):
                return cache["access_token"]
        except Exception:
            pass

    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
    }
    data = _request_with_retry("POST", url, headers=headers, data=json.dumps(body))

    token = data["access_token"]
    expires_in = int(data.get("expires_in", 86400))
    expires_at = datetime.now() + timedelta(seconds=expires_in)

    with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"access_token": token, "expires_at": expires_at.isoformat()},
            f,
        )
    print(f"[KIS] 새 토큰 발급. 만료: {expires_at}")
    return token

def _default_headers(tr_id):
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {get_access_token()}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }

def get_volume_ranking(market="ALL", by="trading_value", max_retries=2):
    market_map = {"ALL": "0000", "KOSPI": "0001", "KOSDAQ": "1001"}
    by_map = {"trading_value": "3", "volume": "0"}

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/volume-rank"
    headers = _default_headers("FHPST01710000")
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": market_map.get(market, "0000"),
        "FID_DIV_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": by_map.get(by, "3"),
        "FID_TRGT_CLS_CODE": "111111111",
        "FID_TRGT_EXLS_CLS_CODE": "0000000000",
        "FID_INPUT_PRICE_1": "",
        "FID_INPUT_PRICE_2": "",
        "FID_VOL_CNT": "",
        "FID_INPUT_DATE_1": "",
    }
    data = _request_with_retry("GET", url, headers=headers, params=params,
                               max_retries=max_retries)

    if data.get("rt_cd") != "0":
        raise RuntimeError(f"volume-rank API 오류: {data.get('msg1')}")

    results = []
    for row in data.get("output", []):
        results.append({
            "code": row.get("mksc_shrn_iscd"),
            "name": row.get("hts_kor_isnm"),
            "current_price": int(row.get("stck_prpr", 0)),
            "change_pct": float(row.get("prdy_ctrt", 0)),
            "volume": int(row.get("acml_vol", 0)),
            "trading_value": int(row.get("acml_tr_pbmn", 0)),
            "rank": int(row.get("data_rank", 0)),
        })
    return results

def get_foreign_institution_trading(market="ALL", div="0", top_n=100, max_retries=2):
    market_map = {"ALL": "0000", "KOSPI": "0001", "KOSDAQ": "1001"}

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/foreign-institution-total"
    headers = _default_headers("FHPTJ04400000")
    params = {
        "FID_COND_MRKT_DIV_CODE": "V",
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": market_map.get(market, "0000"),
        "FID_DIV_CLS_CODE": div,
        "FID_RANK_SORT_CLS_CODE": "0",
        "FID_ETC_CLS_CODE": "0",
    }
    data = _request_with_retry("GET", url, headers=headers, params=params,
                               max_retries=max_retries)

    if data.get("rt_cd") != "0":
        raise RuntimeError(
            f"외인/기관 API 오류: {data.get('msg1')} (msg_cd={data.get('msg_cd')})"
        )

    rows = data.get("output", [])
    return [_parse_investor_row(r) for r in rows[:top_n]]

def _parse_investor_row(row):
    def i(key, default=0):
        try:
            return int(row.get(key, default) or 0)
        except (ValueError, TypeError):
            return 0

    return {
        "code": row.get("mksc_shrn_iscd", ""),
        "name": row.get("hts_kor_isnm", ""),
        "foreign_net_qty":   i("frgn_ntby_qty"),
        "foreign_net_value": i("frgn_ntby_tr_pbmn"),
        "institution_net_qty":   i("orgn_ntby_qty"),
        "institution_net_value": i("orgn_ntby_tr_pbmn"),
    }

def get_stock_investor(stock_code, target_date=None, max_retries=2):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor"
    headers = _default_headers("FHKST01010900")
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
    }
    data = _request_with_retry("GET", url, headers=headers, params=params,
                               max_retries=max_retries)

    if data.get("rt_cd") != "0":
        raise RuntimeError(
            f"종목별 투자자 API 오류 ({stock_code}): {data.get('msg1')} (msg_cd={data.get('msg_cd')})"
        )

    rows = data.get("output", [])
    if not rows:
        return None

    target = None
    if target_date:
        for r in rows:
            if r.get("stck_bsop_date") == target_date:
                target = r
                break
    if target is None:
        return None

    def i(key):
        try:
            return int(target.get(key, 0) or 0)
        except (ValueError, TypeError):
            return 0

    return {
        "matched_date":          target.get("stck_bsop_date", ""),
        "foreign_net_qty":       i("frgn_ntby_qty"),
        "foreign_net_value":     i("frgn_ntby_tr_pbmn"),
        "institution_net_qty":   i("orgn_ntby_qty"),
        "institution_net_value": i("orgn_ntby_tr_pbmn"),
        "personal_net_qty":      i("prsn_ntby_qty"),
        "personal_net_value":    i("prsn_ntby_tr_pbmn"),
    }

def get_daily_chart(stock_code, period_days=60, max_retries=2):
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=period_days * 2 + 10)).strftime("%Y%m%d")

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = _default_headers("FHKST03010100")
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_DATE_1": start_date,
        "FID_INPUT_DATE_2": end_date,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    data = _request_with_retry("GET", url, headers=headers, params=params,
                               max_retries=max_retries)

    if data.get("rt_cd") != "0":
        raise RuntimeError(
            f"일봉 API 오류 ({stock_code}): {data.get('msg1')} (msg_cd={data.get('msg_cd')})"
        )

    def i(d, key, default=0):
        try:
            return int(d.get(key, default) or 0)
        except (ValueError, TypeError):
            return 0
    def f(d, key, default=0.0):
        try:
            return float(d.get(key, default) or 0)
        except (ValueError, TypeError):
            return 0.0

    bars = []
    for row in data.get("output2", []):
        date = row.get("stck_bsop_date")
        if not date:
            continue
        bars.append({
            "date": date,
            "open":  i(row, "stck_oprc"),
            "high":  i(row, "stck_hgpr"),
            "low":   i(row, "stck_lwpr"),
            "close": i(row, "stck_clpr"),
            "volume":        i(row, "acml_vol"),
            "trading_value": i(row, "acml_tr_pbmn"),
            "change_pct":    f(row, "prdy_ctrt"),
        })

    bars.sort(key=lambda x: x["date"])
    return bars[-period_days:] if len(bars) > period_days else bars

def get_minute_chart(stock_code, target_time=None, max_retries=2):
    if target_time is None:
        target_time = datetime.now().strftime("%H%M%S")

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    headers = _default_headers("FHKST03010200")
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_HOUR_1": target_time,
        "FID_PW_DATA_INCU_YN": "Y",
    }
    data = _request_with_retry("GET", url, headers=headers, params=params,
                               max_retries=max_retries)

    if data.get("rt_cd") != "0":
        raise RuntimeError(
            f"분봉 API 오류 ({stock_code}, {target_time}): {data.get('msg1')}"
        )

    bars = []
    for row in data.get("output2", []):
        if not row.get("stck_cntg_hour"):
            continue
        bars.append({
            "date": row["stck_bsop_date"],
            "time": row["stck_cntg_hour"],
            "open": int(row["stck_oprc"]),
            "high": int(row["stck_hgpr"]),
            "low": int(row["stck_lwpr"]),
            "close": int(row["stck_prpr"]),
            "volume": int(row["cntg_vol"]),
        })
    bars.sort(key=lambda x: (x["date"], x["time"]))
    return bars

def get_minute_chart_historical(stock_code, target_date, target_time=None, max_retries=2):
    """과거 종목 분봉 (KIS 주식일별분봉조회, TR_ID FHKST03010230).

    한 호출 최대 120건. 1년치 보관.
    ⚠️ 모의(vps) 환경 미지원 — 실전 키만 가능.

    target_date: YYYYMMDD (과거 일자 가능)
    target_time: HHMMSS (해당 일자의 종료 시각, 그 이전 분봉을 역순으로 받음)
    """
    if target_time is None:
        target_time = "153000"
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
    headers = _default_headers("FHKST03010230")
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_DATE_1": target_date,
        "FID_INPUT_HOUR_1": target_time,
        "FID_PW_DATA_INCU_YN": "N",
        "FID_FAKE_TICK_INCU_YN": "",
    }
    data = _request_with_retry("GET", url, headers=headers, params=params,
                               max_retries=max_retries)
    if data.get("rt_cd") != "0":
        # vps 미지원이면 여기서 에러
        raise RuntimeError(
            f"과거 분봉 API 오류 ({stock_code}, {target_date}): "
            f"{data.get('msg1')} (실전 키 필요할 수 있음)"
        )
    bars = []
    for row in data.get("output2", []):
        t = row.get("stck_cntg_hour", "")
        if not t or t == "999999":
            continue
        bars.append({
            "date": row.get("stck_bsop_date", ""),
            "time": t,
            "open":  int(row.get("stck_oprc", 0) or 0),
            "high":  int(row.get("stck_hgpr", 0) or 0),
            "low":   int(row.get("stck_lwpr", 0) or 0),
            "close": int(row.get("stck_prpr", 0) or 0),
            "volume": int(row.get("cntg_vol", 0) or 0),
        })
    bars.sort(key=lambda x: (x["date"], x["time"]))
    return bars


def get_full_day_historical_minutes(stock_code, date_str, sleep=None):
    """과거 특정 영업일의 1분봉 전체를 수집 (실전 키 + FHKST03010230).

    한 호출 120건 한계라 시각 역순으로 약 4번 호출 (15:30, 13:30, 11:30, 09:30).
    """
    if sleep is None:
        sleep = 0.3 if KIS_ENV == "vps" else 0.1
    seen = set()
    all_bars = []

    # 시각 역순 (15:30 → 13:30 → 11:30 → 09:30 → 09:00)
    times = ["153000", "133000", "113000", "093000", "090000"]
    for et in times:
        try:
            bars = get_minute_chart_historical(stock_code, date_str, target_time=et)
            for b in bars:
                if b["date"] != date_str:
                    continue
                key = (b["date"], b["time"])
                if key in seen:
                    continue
                seen.add(key)
                all_bars.append(b)
            time.sleep(sleep)
        except Exception as e:
            print(f"  [warn] {stock_code} {date_str} {et}: {e}")
            time.sleep(0.5)

    all_bars.sort(key=lambda x: (x["date"], x["time"]))
    return all_bars


def get_full_day_minutes(stock_code, date_str, sleep=None):
    if sleep is None:
        sleep = 0.5 if KIS_ENV == "vps" else 0.1
    seen = set()
    all_bars = []

    times = []
    h, m = 15, 30
    while h > 9 or (h == 9 and m >= 30):
        times.append(f"{h:02d}{m:02d}00")
        if m == 30:
            m = 0
        else:
            m = 30
            h -= 1
            
    # [수정된 부분] 09:30 중복 방지 및 09:00 정각 캔들 누락 방지
    times.append("090000")

    for et in times:
        try:
            bars = get_minute_chart(stock_code, target_time=et)
            for b in bars:
                if b["date"] != date_str:
                    continue
                key = (b["date"], b["time"])
                if key in seen:
                    continue
                seen.add(key)
                all_bars.append(b)
            time.sleep(sleep)
        except Exception as e:
            print(f"  [warn] {stock_code} {date_str} {et}: {e}")
            time.sleep(0.5)

    all_bars.sort(key=lambda x: (x["date"], x["time"]))
    return all_bars


# ============================================================
# 시장지수 (KOSPI / KOSDAQ) — 현재가 + 분봉
# ============================================================

INDEX_CODE_MAP = {"KOSPI": "0001", "KOSDAQ": "1001"}


def get_index_current(market="KOSPI", max_retries=2):
    """현재 시점 지수값. Returns: dict(value, change, change_pct, volume, trading_value)."""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-index-price"
    headers = _default_headers("FHPUP02100000")
    params = {
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": INDEX_CODE_MAP.get(market, "0001"),
    }
    data = _request_with_retry("GET", url, headers=headers, params=params,
                               max_retries=max_retries)
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"지수 현재가 API 오류 ({market}): {data.get('msg1')}")
    o = data.get("output", {})
    # KIS prdy_vrss_sign : 1=상한, 2=상승, 3=보합, 4=하한, 5=하락
    # bstp_nmix_prdy_ctrt / bstp_nmix_prdy_vrss 는 절댓값일 수 있어 부호 적용.
    sign = str(o.get("prdy_vrss_sign", "3"))
    multiplier = -1.0 if sign in ("4", "5") else 1.0
    change_abs = float(o.get("bstp_nmix_prdy_vrss", 0) or 0)
    change_pct_abs = float(o.get("bstp_nmix_prdy_ctrt", 0) or 0)
    return {
        "market": market,
        "value": float(o.get("bstp_nmix_prpr", 0) or 0),
        "change": multiplier * abs(change_abs),
        "change_pct": multiplier * abs(change_pct_abs),
        "volume": int(o.get("acml_vol", 0) or 0),
        "trading_value": int(o.get("acml_tr_pbmn", 0) or 0),
        # 오늘 OHLC (당일 누적치, snapshot 시각까지)
        "open": float(o.get("bstp_nmix_oprc", 0) or 0),
        "high": float(o.get("bstp_nmix_hgpr", 0) or 0),
        "low": float(o.get("bstp_nmix_lwpr", 0) or 0),
        # 시장 breadth — 단타 룰의 시장 컨텍스트 필터에 직결
        "up_count":     int(o.get("ascn_issu_cnt", 0) or 0),
        "down_count":   int(o.get("down_issu_cnt", 0) or 0),
        "stnr_count":   int(o.get("stnr_issu_cnt", 0) or 0),
        "upper_limit":  int(o.get("uplm_issu_cnt", 0) or 0),
        "lower_limit":  int(o.get("lslm_issu_cnt", 0) or 0),
    }


def get_index_minute_chart(market="KOSPI", target_time=None, max_retries=2):
    """KOSPI/KOSDAQ 지수 분봉 시계열.

    endpoint: inquire-index-tickprice
    TR_ID: FHKUP03500200
    한 호출당 약 100개 반환 → 종목 분봉처럼 30분 단위 역순 호출로 하루 누적.

    Returns: list[dict(date, time, open, high, low, close, volume, trading_value)]
    """
    if target_time is None:
        target_time = datetime.now().strftime("%H%M%S")
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-index-tickprice"
    headers = _default_headers("FHKUP03500200")
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": INDEX_CODE_MAP.get(market, "0001"),
        "FID_INPUT_HOUR_1": target_time,
        "FID_PW_DATA_INCU_YN": "Y",
    }
    data = _request_with_retry("GET", url, headers=headers, params=params,
                               max_retries=max_retries)
    if data.get("rt_cd") != "0":
        print(f"  [warn] 지수 분봉 API 오류 ({market}): {data.get('msg1')}")
        return []
    bars = []
    for row in data.get("output2", []):
        # KIS 가 어제 일별 누적치를 stck_cntg_hour="999999" 로 끼워서 줌 — 필터
        t = row.get("stck_cntg_hour", "")
        if not t or t == "999999":
            continue
        bars.append({
            "date": row.get("stck_bsop_date", ""),
            "time": t,
            "open":  float(row.get("bstp_nmix_oprc", 0) or 0),
            "high":  float(row.get("bstp_nmix_hgpr", 0) or 0),
            "low":   float(row.get("bstp_nmix_lwpr", 0) or 0),
            "close": float(row.get("bstp_nmix_prpr", 0) or 0),
            "volume":        int(row.get("cntg_vol", 0) or 0),
            "trading_value": int(row.get("acml_tr_pbmn", 0) or 0),
        })
    bars.sort(key=lambda x: (x["date"], x["time"]))
    return bars
