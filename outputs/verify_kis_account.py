# -*- coding: utf-8 -*-
"""
verify_kis_account.py — KIS 모의계좌 자격증명 교체 후 '진짜 살아났는지' 1회 점검.
(2026-08-21 신설 — 모의계좌 재발급 대응)

배경
  2026-08-10~08-21, KIS 모의계좌가 msg_cd=40910000 으로 모든 주문을 거부했다.
  재발급 후에는 ① .env 값 ② 토큰 캐시 ③ 원장(kis_positions.csv) 세 곳이 서로
  맞아야 정상 동작하는데, 어느 하나가 어긋나면 증상이 전부 '인증/주문 실패'로
  똑같이 보여 오진하기 쉽다. 그래서 세 곳을 한 번에 대조한다.

점검 항목 (주문은 절대 넣지 않는다 — 조회만)
  1) .env 3개 키 존재·형식(계좌번호 자릿수 → CANO/상품코드 분해 결과 표시)
  2) 토큰 발급 (앱키 지문이 캐시와 다르면 자동 재발급되는지 포함)
  3) 잔고 조회 성공 여부
     ※ 조회가 된다고 주문까지 되는 것은 아니다 — 08-10~08-21 사고 때도 조회는
       내내 정상이었고 주문만 40910000 으로 거부됐다. 주문 가능 여부는 실제로
       주문을 넣어봐야 알 수 있고, 그 역할은 08:50 장전 탐침(preopen_probe)이 한다.
       이 스크립트는 주문을 넣지 않는다.
  4) 원장(kis_positions.csv) vs 실제 보유 대조 — 새 계좌면 원장을 비워야 한다
  5) 장전 탐침 기록(preopen_probe_result.json)의 낡은 fatal 표시 안내

실행: cd C:/fin/outputs ; .venv/Scripts/python.exe verify_kis_account.py
"""
from __future__ import annotations

import os
import sys
import csv
import json

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_OK, _NG, _WARN = "  [OK]", "  [XX]", "  [!!]"

# 새로 개설한 모의계좌의 초기 예수금(사용자 고지 2026-08-21).
# 조회값이 이와 크게 다르면 자격증명이 아직 옛 계좌를 가리키는 것이다.
EXPECTED_NEW_DEPOSIT = 10_000_000

_problems: list[str] = []
_todo: list[str] = []


def _mask(v: str, keep: int = 4) -> str:
    if not v:
        return "(빈값)"
    return f"{v[:keep]}...{v[-2:]}  [{len(v)}자]"


def step1_env():
    print("\n[1] .env 자격증명")
    import kis_trader as kx          # import 시점에 .env 를 읽고 없으면 종료한다

    print(f"{_OK} KIS_MOCK_APP_KEY    = {_mask(kx._APP_KEY)}")
    print(f"{_OK} KIS_MOCK_APP_SECRET = {_mask(kx._APP_SECRET)}")
    print(f"{_OK} KIS_MOCK_ACCOUNT    = {_mask(kx._ACCOUNT, 4)}")
    print(f"       → CANO(앞 8자리)   = {kx._CANO}")
    print(f"       → 상품코드         = {kx._ACNT_PRDT_CD}"
          f"{'  ※ 계좌번호가 8자리라 01 로 가정함' if len(kx._ACCOUNT) <= 8 else ''}")

    if len(kx._APP_KEY) != 36:
        _problems.append(f"APP_KEY 길이가 {len(kx._APP_KEY)}자 (보통 36자) — 복사 누락 의심")
    if len(kx._APP_SECRET) < 100:
        _problems.append(f"APP_SECRET 길이가 {len(kx._APP_SECRET)}자 (보통 180자) — 복사 누락 의심")
    return kx


def step2_token(kx):
    print("\n[2] 토큰 발급")
    cache = kx.TOKEN_CACHE
    if os.path.exists(cache):
        try:
            d = json.load(open(cache, encoding="utf-8"))
            same = d.get("key_fp") == kx.KISMockClient._key_fingerprint()
            print(f"       캐시 존재 — 앱키 지문 일치: {'예' if same else '아니오(자동 폐기됨)'}")
        except Exception:
            print("       캐시 파싱 불가 — 무시하고 재발급된다")
    else:
        print("       캐시 없음 — 새로 발급한다")

    try:
        cl = kx.KISMockClient()
        tok = cl.get_token()
        print(f"{_OK} 토큰 발급 성공 (len={len(tok)})")
        return cl
    except Exception as e:
        print(f"{_NG} 토큰 발급 실패: {type(e).__name__}: {str(e)[:160]}")
        _problems.append("토큰 발급 실패 — APP_KEY/APP_SECRET 을 다시 확인할 것")
        return None


def step3_balance(cl):
    print("\n[3] 잔고 조회 (읽기 권한 + 앱키-계좌 정합 확인)")
    if cl is None:
        print(f"{_NG} 토큰이 없어 건너뜀")
        return None
    try:
        deposit, positions = cl.get_balance()
    except Exception as e:
        msg = str(e)
        print(f"{_NG} 잔고 조회 실패: {type(e).__name__}: {msg[:160]}")
        # KIS 의 INVALID_CHECK_ACNO 는 문구만 봐선 '계좌번호 오타'로 읽히지만,
        # 실제로 흔한 원인은 **앱키가 다른 계좌에 묶여 있는 것**이다. 모의계좌를
        # 새로 만들면 그 계좌용 APP KEY/SECRET 을 따로 발급받아야 하는데,
        # 계좌번호만 바꾸면 정확히 이 오류가 난다(2026-08-21 실측).
        if "INVALID_CHECK_ACNO" in msg or "ACNO" in msg:
            import kis_trader as _kx
            if len(_kx._ACCOUNT) <= 8:
                print("       (참고: 계좌번호가 8자리라 상품코드를 01 로 가정 중)")
            _diagnose_acno_mismatch()
        else:
            _problems.append("잔고 조회 실패 — 계좌 상태 또는 네트워크 확인")
        return None

    s = getattr(cl, "last_summary", {}) or {}
    print(f"{_OK} 조회 성공")
    print(f"       예수금       : {deposit:,}원")
    if s:
        print(f"       총평가금액   : {kx_int(s.get('total_eval')):,}원")
        print(f"       주문가능     : {kx_int(s.get('orderable')):,}원")
    # 사용자 고지(2026-08-21): 새로 개설한 모의계좌 예수금은 1,000만원.
    # 조회된 예수금이 이 값 근처가 아니면 아직 옛 계좌를 보고 있다는 뜻이다.
    if abs(deposit - EXPECTED_NEW_DEPOSIT) > EXPECTED_NEW_DEPOSIT * 0.5:
        print(f"{_WARN} 기대 예수금({EXPECTED_NEW_DEPOSIT:,}원)과 크게 다르다 — "
              f"옛 계좌를 보고 있는 것은 아닌지 확인")
    print(f"       보유 종목    : {len(positions)}건")
    for c, v in positions.items():
        print(f"         {c} {v.get('name','')}  {v.get('qty')}주")
    import kis_trader as _kx
    if len(_kx._ACCOUNT) <= 8:
        print(f"       (상품코드 {_kx._ACNT_PRDT_CD} 로 조회 성공 — 가정이 맞았음이 실증됨)")
    print(f"{_WARN} 조회 성공 = 주문 가능 아님. 08-10~08-21 사고 때도 조회는 내내")
    print( "       정상이었고 주문만 거부됐다. 주문 가능 여부는 다음 거래일")
    print( "       08:50 장전 탐침이 실제 주문으로 확인한다.")
    return positions


def kx_int(v, default=0):
    try:
        return int(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return default



def _diagnose_acno_mismatch():
    """INVALID_CHECK_ACNO 를 '앱키-계좌 불일치'인지까지 좁혀 준다(조회 전용).

    상품코드 01~03 을 순서대로 시험해 본다. 전부 거부되면 계좌번호 자릿수 문제가
    아니라 앱키가 다른 계좌에 묶인 것이다 — 그 경우 안내 문구를 명확히 바꾼다.
    """
    import requests
    import kis_trader as kx

    print("       → 상품코드 01~03 을 시험해 원인을 좁힙니다...")
    try:
        tok = kx.KISMockClient().get_token()
    except Exception:
        _problems.append("잔고 조회 실패 — 토큰을 다시 받지 못해 원인 판별 중단")
        return

    cano = kx._CANO
    hit = None
    for prdt in ("01", "02", "03"):
        params = {
            "CANO": cano, "ACNT_PRDT_CD": prdt,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
            "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        h = {"Content-Type": "application/json; charset=utf-8",
             "authorization": f"Bearer {tok}",
             "appkey": kx._APP_KEY, "appsecret": kx._APP_SECRET,
             "tr_id": "VTTC8434R", "custtype": "P"}
        try:
            r = requests.get(
                f"{kx.MOCK_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
                headers=h, params=params, timeout=15)
            d = r.json()
        except Exception:
            continue
        if d.get("rt_cd") == "0":
            hit = prdt
            break
        print(f"         {cano}-{prdt}: [{d.get('msg_cd','')}] {d.get('msg1','')[:40]}")

    if hit:
        print(f"{_WARN} 상품코드 {hit} 로는 조회된다")
        _problems.append(
            f"상품코드가 01 이 아니다 — .env 의 KIS_MOCK_ACCOUNT 를 "
            f"'{cano}{hit}' (하이픈 없이 10자리)로 바꿀 것")
        return

    _problems.append(
        "앱키와 계좌번호가 서로 다른 계좌를 가리킨다. "
        "KIS 는 모의투자 APP KEY/SECRET 이 계좌에 묶여 있어서, 계좌를 새로 만들면 "
        "그 계좌용 APP KEY/SECRET 을 새로 발급받아야 한다 — 계좌번호만 바꾸면 "
        "정확히 INVALID_CHECK_ACNO 가 난다. "
        "KIS 개발자센터 > 모의투자 API 신청 에서 새 계좌를 선택해 키를 재발급한 뒤 "
        ".env 의 KIS_MOCK_APP_KEY / KIS_MOCK_APP_SECRET 도 함께 교체할 것.")


def step4_ledger(positions):
    print("\n[4] 원장(kis_positions.csv) vs 실제 보유")
    path = "./db/kis/kis_positions.csv"
    ledger = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    code = str(r.get("code", "")).zfill(6)
                    if code and code != "000000":
                        ledger[code] = r
        except Exception as e:
            print(f"{_WARN} 원장 읽기 실패: {e}")
            return
    print(f"       원장 {len(ledger)}건 / 실제 보유 "
          f"{'?' if positions is None else len(positions)}건")

    if positions is None:
        print(f"{_WARN} 잔고를 못 읽어 대조 생략")
        return

    orphan = [c for c in ledger if c not in positions]     # 원장에만 있음
    stale = [c for c in positions if c not in ledger]      # 실제에만 있음
    for c in orphan:
        r = ledger[c]
        print(f"{_NG} orphan {c} — 원장엔 있는데 계좌에 없음 "
              f"(진입가 {r.get('entry_px')}, 신호일 {r.get('signal_date')})")
    for c in stale:
        print(f"{_WARN} stale {c} {positions[c].get('name','')} — "
              f"계좌엔 있는데 원장에 없음(만기 추적 불가)")

    if orphan:
        _todo.append(
            f"원장에 {len(orphan)}건이 남아 있는데 새 계좌엔 없다({', '.join(orphan)}). "
            f"새 계좌라면 db/kis/kis_positions.csv 를 헤더만 남기고 비워야 한다 — "
            f"안 그러면 매일 orphan 경고가 뜨고 만기 판정이 헛돈다.")
    if not orphan and not stale:
        print(f"{_OK} 원장과 계좌가 일치")


def step5_probe():
    print("\n[5] 장전 탐침 기록")
    p = "./db/preopen_probe_result.json"
    if not os.path.exists(p):
        print("       기록 없음 — 다음 거래일 08:50 에 새로 측정된다")
        return
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print(f"{_WARN} 읽기 실패: {e}")
        return
    kis = d.get("kis", {}) or {}
    print(f"       측정 시각: {d.get('probed_at')}")
    print(f"       kis: accepted={kis.get('accepted')} fatal={kis.get('fatal')}")
    if kis.get("fatal"):
        print(f"{_WARN} 아직 '계좌 이상'으로 남아 있다 — 다음 거래일 08:50 에 "
              f"자동 재탐침되어 갱신된다(손댈 필요 없음)")


def main():
    print("=" * 68)
    print(" KIS 모의계좌 점검 — 조회만 수행, 주문은 넣지 않는다")
    print("=" * 68)
    kx = step1_env()
    cl = step2_token(kx)
    positions = step3_balance(cl)
    step4_ledger(positions)
    step5_probe()

    print("\n" + "=" * 68)
    if _problems:
        print(" 판정: 아직 정상이 아니다")
        for p in _problems:
            print(f"   - {p}")
    else:
        print(" 판정: 계좌 읽기 정상 (토큰 발급 + 잔고 조회 성공)")
        print("       ※ 주문 가능 여부는 미확인 — 다음 거래일 08:50 탐침 결과를 볼 것")
    if _todo:
        print("\n 남은 정리 작업")
        for t in _todo:
            print(f"   - {t}")
    print("=" * 68)
    return 1 if _problems else 0


if __name__ == "__main__":
    sys.exit(main())
