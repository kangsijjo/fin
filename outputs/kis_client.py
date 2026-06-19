"""
kis_client.py — 한국투자증권 OpenAPI REST 클라이언트

기능:
  - 접근토큰 발급 + .kis_token.json 캐싱 (만료 전 자동 재발급)
  - 거래량순위          : volume_rank()         → TR FHPST01710000
  - 기관/외국인 가집계  : inst_foreign_total()  → TR FHPTJ04400000
  - 체결강도 상위       : exec_strength_rank()  → TR FHPST01890000
  - 현재가(단건)        : get_price()           → TR FHKST01010100

공식 API 포털: https://apiportal.koreainvestment.com
  - [국내주식] 순위분석 > 거래량순위           (volume-rank)
  - [국내주식] 시세분석 > 국내기관_외국인 매매종목가집계 (foreign-institution-total)
  - [국내주식] 순위분석 > 국내주식 체결강도 상위   (chk-invest-opinion)

⚠️  TR ID / 파라미터는 KIS 포털 최신 문서 기준으로 검증 필요.
    실제 발급된 앱키로 첫 호출 시 응답 구조를 확인하고 필드명을 맞추세요.

.env 필수 항목:
  # 실전 (KIS_ENV=prod)
  KIS_PROD_APP_KEY=...
  KIS_PROD_APP_SECRET=...
  KIS_PROD_ACCOUNT=...

  # 모의 (KIS_ENV=vps, 기본값)
  KIS_MOCK_APP_KEY=...
  KIS_MOCK_APP_SECRET=...
  KIS_MOCK_ACCOUNT=...
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# ── 토큰 캐시 경로 (.gitignore 처리됨) ─────────────────────────────────────
_BASE = Path(__file__).resolve().parent
TOKEN_FILE = _BASE / ".kis_token.json"


class KISClient:
    """
    한국투자증권 OpenAPI REST 클라이언트.

    사용 예:
        from kis_client import KISClient
        kis = KISClient()            # config.py 환경변수 자동 읽기
        df = kis.volume_rank()       # 거래량순위 DataFrame
    """

    # ── 환경별 BASE URL ─────────────────────────────────────────────────────
    BASE_URL_PROD = "https://openapi.koreainvestment.com:9443"
    BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        env: Optional[str] = None,  # "prod" | "vps"
    ):
        # config.py 에서 읽거나 직접 주입
        try:
            sys.path.insert(0, str(_BASE))
            import config as cfg
            _env = env or getattr(cfg, "KIS_ENV", "vps")
            self.app_key    = app_key    or getattr(cfg, "KIS_APP_KEY", "")
            self.app_secret = app_secret or getattr(cfg, "KIS_APP_SECRET", "")
        except Exception:
            _env = env or os.getenv("KIS_ENV", "vps")
            self.app_key    = app_key    or os.getenv("KIS_MOCK_APP_KEY", "")
            self.app_secret = app_secret or os.getenv("KIS_MOCK_APP_SECRET", "")

        self.env     = _env.lower().strip()
        self.is_prod = (self.env == "prod")
        self.base    = self.BASE_URL_PROD if self.is_prod else self.BASE_URL_MOCK

        self._token: Optional[str]  = None
        self._token_exp: float      = 0.0   # Unix timestamp

        if not self.app_key or not self.app_secret:
            print(f"[KIS] ⚠️  앱키/시크릿 미설정 (KIS_ENV={self.env}). "
                  ".env 파일을 확인하세요.")

    # ══════════════════════════════════════════════════════════════════════════
    # 토큰 관리
    # ══════════════════════════════════════════════════════════════════════════

    def _load_cached_token(self) -> bool:
        """TOKEN_FILE 에서 유효한 토큰을 로드. 성공 시 True."""
        if not TOKEN_FILE.exists():
            return False
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if data.get("env") != self.env:
                return False
            exp = float(data.get("expires_at", 0))
            if exp - time.time() > 300:   # 5분 여유
                self._token     = data["access_token"]
                self._token_exp = exp
                return True
        except Exception:
            pass
        return False

    def _save_token(self, token: str, expires_in: int = 86400):
        exp = time.time() + expires_in - 60
        TOKEN_FILE.write_text(
            json.dumps({"env": self.env, "access_token": token, "expires_at": exp},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_token(self) -> str:
        """유효한 접근토큰 반환 (필요 시 신규 발급)."""
        if self._token and time.time() < self._token_exp - 300:
            return self._token
        if self._load_cached_token():
            return self._token  # type: ignore[return-value]

        url = f"{self.base}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey":     self.app_key,
            "appsecret":  self.app_secret,
        }
        try:
            resp = requests.post(url, json=body, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            token = data.get("access_token") or data.get("token")
            if not token:
                raise ValueError(f"토큰 없음: {data}")
            expires_in = int(data.get("expires_in", 86400))
            self._token     = token
            self._token_exp = time.time() + expires_in - 60
            self._save_token(token, expires_in)
            print(f"[KIS] ✓ 토큰 발급 완료 ({self.env})")
            return self._token  # type: ignore[return-value]
        except Exception as e:
            print(f"[KIS] ✗ 토큰 발급 실패: {e}")
            raise

    # ══════════════════════════════════════════════════════════════════════════
    # 공통 요청 헬퍼
    # ══════════════════════════════════════════════════════════════════════════

    def _get(self, path: str, tr_id: str, params: dict, timeout: int = 10) -> dict:
        """
        KIS REST GET 요청.

        공통 헤더:
          content-type, authorization, appkey, appsecret, tr_id
        """
        token = self.get_token()
        headers = {
            "content-type":  "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey":        self.app_key,
            "appsecret":     self.app_secret,
            "tr_id":         tr_id,
            "custtype":      "P",           # 개인 (법인은 B)
        }
        url = f"{self.base}{path}"
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            print(f"[KIS] HTTP {resp.status_code}: {e}")
            # 401 → 토큰 만료 가능성, 한 번 갱신 후 재시도
            if resp.status_code == 401:
                TOKEN_FILE.unlink(missing_ok=True)
                self._token = None
                token = self.get_token()
                headers["authorization"] = f"Bearer {token}"
                resp = requests.get(url, headers=headers, params=params, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
            else:
                raise
        except Exception as e:
            print(f"[KIS] 요청 실패 ({tr_id}): {e}")
            raise

        rt_cd = data.get("rt_cd", "")
        if rt_cd != "0":
            msg = data.get("msg1", data.get("msg", ""))
            raise ValueError(f"KIS API 에러 [{rt_cd}]: {msg}")

        return data

    # ══════════════════════════════════════════════════════════════════════════
    # ① 거래량순위
    # ══════════════════════════════════════════════════════════════════════════

    def volume_rank(self, top_n: int = 20) -> list[dict]:
        """
        거래량순위 상위 N개 조회.

        TR_ID  : FHPST01710000
        URL    : /uapi/domestic-stock/v1/quotations/volume-rank
        포털   : [국내주식] 순위분석 > 거래량순위

        반환 키: rank, code, name, close, change_pct, volume, trading_value
        """
        path  = "/uapi/domestic-stock/v1/quotations/volume-rank"
        tr_id = "FHPST01710000"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",     # J=주식
            "FID_COND_SCR_DIV_CODE":  "20171",
            "FID_INPUT_ISCD":         "0000",   # 전체
            "FID_DIV_CLS_CODE":       "0",      # 0=전체
            "FID_BLNG_CLS_CODE":      "0",      # 0=평균거래량
            "FID_TRGT_CLS_CODE":      "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1":      "",
            "FID_INPUT_PRICE_2":      "",
            "FID_VOL_CNT":            "",
            "FID_INPUT_DATE_1":       "",
        }
        data = self._get(path, tr_id, params)
        output = data.get("output", [])
        if not isinstance(output, list):
            output = []

        result = []
        for i, r in enumerate(output[:top_n], 1):
            # 포털 응답 필드명 (실제 응답에서 다를 수 있음 → 주석으로 원본명 병기)
            result.append({
                "rank":          i,
                "code":          r.get("mksc_shrn_iscd", "").zfill(6),   # 단축종목코드
                "name":          r.get("hts_kor_isnm", ""),               # 종목명
                "close":         _to_int(r.get("stck_prpr", 0)),          # 현재가
                "change_pct":    _to_float(r.get("prdy_ctrt", 0)),        # 전일대비율
                "volume":        _to_int(r.get("acml_vol", 0)),           # 누적거래량
                "trading_value": _to_int(r.get("acml_tr_pbmn", 0)),       # 누적거래대금
            })
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # ② 기관/외국인 매매종목 가집계 (장중 추정)
    # ══════════════════════════════════════════════════════════════════════════

    def inst_foreign_total(self, top_n: int = 20) -> tuple[list[dict], list[dict]]:
        """
        국내기관·외국인 매매종목 가집계 조회.

        TR_ID  : FHPTJ04400000
        URL    : /uapi/domestic-stock/v1/quotations/foreign-institution-total
        포털   : [국내주식] 시세분석 > 국내기관_외국인 매매종목가집계

        반환: (inst_top20, foreign_top20)
          각 항목 키: rank, code, name, close, change_pct, inst_net / foreign_net

        ※ 가집계(추정치): 장중 15분 지연, 확정치는 15:30 이후
        ※ KIS 응답은 output / output1 / output2 혼재 — 모두 확인
        """
        path  = "/uapi/domestic-stock/v1/quotations/foreign-institution-total"
        tr_id = "FHPTJ04400000"

        # 한 번만 호출 (FID_ETC_CLS_CODE=0 → 전체, 기관+외국인 한 번에)
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE":  "20424",
            "FID_INPUT_ISCD":         "0000",
            "FID_DIV_CLS_CODE":       "0",
            "FID_BLNG_CLS_CODE":      "0",
            "FID_RANK_SORT_CLS_CODE": "1",   # 1=순매수금액 기준 정렬
            "FID_ETC_CLS_CODE":       "0",   # 0=전체(기관+외국인)
        }
        try:
            data = self._get(path, tr_id, params)
        except Exception as e:
            print(f"[KIS] inst_foreign_total 오류: {e}")
            return [], []

        # output1 → output → output2 순으로 비어있지 않은 첫 리스트 사용
        # ※ or 연산자 미사용: [] 는 falsy라 채워진 output 을 건너뛸 수 있음
        output = []
        for _key in ("output1", "output", "output2"):
            _val = data.get(_key)
            if isinstance(_val, list) and len(_val) > 0:
                output = _val
                break

        if not output:
            # 장 마감 후 가집계 데이터 없음은 정상 (09:00~15:30 장중만 채워짐)
            all_keys = {k: len(v) if isinstance(v, list) else type(v).__name__
                        for k, v in data.items()}
            print(f"  [KIS] inst_foreign 응답 (빈 결과): {all_keys}")

        records = []
        for r in output:
            code = r.get("mksc_shrn_iscd", "").zfill(6)
            # 기관 순매수: orgn_ntby_qty / inst_ntby_qty / orgn_ntby_pbmn
            inst_net = (
                _to_int(r.get("orgn_ntby_qty"))
                or _to_int(r.get("inst_ntby_qty"))
                or _to_int(r.get("orgn_ntby_pbmn"))
            )
            # 외국인 순매수: frgn_ntby_qty / frgn_ntby_pbmn
            frn_net = (
                _to_int(r.get("frgn_ntby_qty"))
                or _to_int(r.get("frgn_ntby_pbmn"))
            )
            records.append({
                "code":       code,
                "name":       r.get("hts_kor_isnm", ""),
                "close":      _to_int(r.get("stck_prpr", 0)),
                "change_pct": _to_float(r.get("prdy_ctrt", 0)),
                "inst_net":   inst_net,
                "foreign_net": frn_net,
            })

        # 기관 상위 N
        inst_sorted = sorted(
            [r for r in records if r["inst_net"] > 0],
            key=lambda x: x["inst_net"], reverse=True
        )[:top_n]
        inst_result = [
            {"rank": i + 1, **{k: v for k, v in r.items() if k != "foreign_net"}}
            for i, r in enumerate(inst_sorted)
        ]

        # 외국인 상위 N
        frn_sorted = sorted(
            [r for r in records if r["foreign_net"] > 0],
            key=lambda x: x["foreign_net"], reverse=True
        )[:top_n]
        foreign_result = [
            {"rank": i + 1, **{k: v for k, v in r.items() if k != "inst_net"}}
            for i, r in enumerate(frn_sorted)
        ]

        return inst_result, foreign_result

    # ══════════════════════════════════════════════════════════════════════════
    # ③ 체결강도 상위
    # ══════════════════════════════════════════════════════════════════════════

    def exec_strength_rank(self, top_n: int = 20) -> list[dict]:
        """
        체결강도 상위 조회.

        KIS REST API에는 체결강도 순위 전용 endpoint가 없음.
        대신 거래량순위(volume-rank) 응답에 포함된 'ccld_rate' 필드를 활용.
          ccld_rate = 매수체결량 / 매도체결량 × 100 (KIS 제공 체결강도)

        volume_rank 에서 넉넉히 n=200 종목을 가져온 뒤 ccld_rate 내림차순 정렬.
        ccld_rate 가 없으면 빈 리스트 반환 → 호출측에서 pykrx fallback 처리.

        반환 키: rank, code, name, close, change_pct, exec_strength
        """
        path  = "/uapi/domestic-stock/v1/quotations/volume-rank"
        tr_id = "FHPST01710000"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE":  "20171",
            "FID_INPUT_ISCD":         "0000",
            "FID_DIV_CLS_CODE":       "0",
            "FID_BLNG_CLS_CODE":      "0",
            "FID_TRGT_CLS_CODE":      "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1":      "",
            "FID_INPUT_PRICE_2":      "",
            "FID_VOL_CNT":            "",
            "FID_INPUT_DATE_1":       "",
        }
        try:
            data   = self._get(path, tr_id, params)
            output = data.get("output", [])
            if not isinstance(output, list):
                output = []
        except Exception as e:
            print(f"[KIS] exec_strength_rank(volume-rank) 오류: {e}")
            return []

        # ccld_rate 필드 확인
        has_ccld = any("ccld_rate" in r for r in output[:3])
        if not has_ccld:
            print("  [KIS] ccld_rate 필드 없음 → exec_strength는 pykrx fallback 사용")
            return []

        candidates = []
        for r in output:
            strength = _to_float(r.get("ccld_rate", 0))
            if strength > 0:
                candidates.append({
                    "code":          r.get("mksc_shrn_iscd", "").zfill(6),
                    "name":          r.get("hts_kor_isnm", ""),
                    "close":         _to_int(r.get("stck_prpr", 0)),
                    "change_pct":    _to_float(r.get("prdy_ctrt", 0)),
                    "exec_strength": round(strength, 1),
                })

        candidates.sort(key=lambda x: x["exec_strength"], reverse=True)
        return [{"rank": i + 1, **c} for i, c in enumerate(candidates[:top_n])]

    # ══════════════════════════════════════════════════════════════════════════
    # ④ 현재가 단건 (보유 포지션 P&L 계산용)
    # ══════════════════════════════════════════════════════════════════════════

    def get_price(self, code: str) -> dict:
        """
        단일 종목 현재가 조회.

        TR_ID : FHKST01010100
        URL   : /uapi/domestic-stock/v1/quotations/inquire-price
        """
        path  = "/uapi/domestic-stock/v1/quotations/inquire-price"
        tr_id = "FHKST01010100"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         code.zfill(6),
        }
        data   = self._get(path, tr_id, params)
        output = data.get("output", {})
        if isinstance(output, list):
            output = output[0] if output else {}
        return {
            "code":        code.zfill(6),
            "close":       _to_int(output.get("stck_prpr", 0)),
            "change_pct":  _to_float(output.get("prdy_ctrt", 0)),
            "volume":      _to_int(output.get("acml_vol", 0)),
        }

    # ══════════════════════════════════════════════════════════════════════════
    # 헬스체크
    # ══════════════════════════════════════════════════════════════════════════

    def ping(self) -> bool:
        """토큰 발급 성공 여부만 확인. True = 정상."""
        try:
            self.get_token()
            return True
        except Exception:
            return False


# ── 형변환 헬퍼 ─────────────────────────────────────────────────────────────
def _to_int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


def _to_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


# ── 단독 실행 시 헬스체크 ───────────────────────────────────────────────────
if __name__ == "__main__":
    kis = KISClient()
    print(f"환경: {kis.env}  /  BASE: {kis.base}")

    if not kis.ping():
        print("❌ 토큰 발급 실패. .env 파일의 앱키/시크릿을 확인하세요.")
        sys.exit(1)

    print("\n▶ 거래량순위 Top5")
    for r in kis.volume_rank(5):
        print(f"  {r['rank']:2}. [{r['code']}] {r['name']:<20} "
              f"{r['close']:>8,}원  {r['change_pct']:+.2f}%  "
              f"거래대금 {r['trading_value']//100_000_000:,}억")

    print("\n▶ 기관/외국인 Top5")
    inst, frn = kis.inst_foreign_total(5)
    for r in inst:
        print(f"  기관 {r['rank']:2}. [{r['code']}] {r['name']:<20}  "
              f"순매수 {r.get('inst_net', 0):,}주")
    for r in frn:
        print(f"  외인 {r['rank']:2}. [{r['code']}] {r['name']:<20}  "
              f"순매수 {r.get('foreign_net', 0):,}주")

    print("\n▶ 체결강도 Top5")
    for r in kis.exec_strength_rank(5):
        print(f"  {r['rank']:2}. [{r['code']}] {r['name']:<20}  "
              f"체결강도 {r['exec_strength']:.1f}")
