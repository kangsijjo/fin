# -*- coding: utf-8 -*-
"""
preopen_probe.py — '장 시작 동시호가(08:30~09:00) 주문 수용 여부' 1회성 실측 탐침.
(2026-07-21 신설)

배경: 백테스트 진입가 가정은 '다음날 시가'인데 실제 체결은 개장 후라 계통 오차가 난다.
      08:5X 에 주문하면 09:00 시가에 체결돼 정합이 맞는다. 다만 모의서버가 장전 주문을
      받는지가 불확실 — 실측 근거:
        · 06:33 매도 시도 → 거부 "return_code=20 [2000](RC4057:모의투자 장시작전)"
        · 15:21 매도(마감 동시호가) → 정상 접수(주문번호 발급)
      즉 '동시호가' 자체는 수용하나 장 시작 전 기준선(08:30 vs 09:00)이 불명.

방식(안전): 체결 불가능한 '하한가 근처 지정가 1주 매수'를 넣어 응답만 본다.
      · 접수되면 → 장전 주문 수용 = 08:5X 진입 가능(설계 진행)
      · "장시작전" 류 거부면 → 미수용 = 09:00 트리거 유지가 정답
      주문 취소 API 가 없어 미체결 주문은 남지만, 당일 주문은 장 마감 시 자동 실효되고
      가격이 하한가라 체결 가능성이 사실상 없다(모의계좌, 1주).

결과는 db/preopen_probe_result.json 에 기록되며, 기록이 있으면 다시 탐침하지 않는다.
실행: python preopen_probe.py            (08:30~09:00 사이에만 의미 있음)
      python preopen_probe.py --force    (기록 무시하고 재탐침)
"""
import os
import sys
import json
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESULT_PATH = "./db/preopen_probe_result.json"
# 탐침 종목은 실행 시점에 유니버스 거래대금 1위로 자동 선택(_pick_probe_stock).
# 하드코딩(005930)은 코스닥 전용 유니버스에서 항상 실패했음(2026-08-09 수정).
PROBE_DISCOUNT = 0.71          # 전일종가 대비 −29% ≈ 하한가 근처(체결 불가)


def _tick(price):
    """호가단위 내림 정렬 — 잘못된 호가는 '가격 오류'로 거부돼 탐침 결과가 오염된다."""
    p = int(price)
    for lim, unit in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                      (200000, 100), (500000, 500)):
        if p < lim:
            return p - (p % unit)
    return p - (p % 1000)


def _pick_probe_stock():
    """탐침 종목·전일종가 선택 — 최근 일봉 CSV 의 '거래대금 최상위' 종목을 쓴다.

    [2026-08-09 수정] 종전엔 005930(삼성전자)을 하드코딩했는데, 이 시스템의 일봉
    유니버스는 **코스닥 전용**(1,820종목)이라 조회가 항상 실패해 탐침이 매번
    '전일 종가 조회 실패'로 중단됐다(08-06 실측). 유니버스 안에서 가장 유동성 높은
    종목을 고르면 환경이 바뀌어도 깨지지 않는다.
    반환: (code, prev_close) 또는 (None, None)
    """
    import pandas as pd
    import glob
    files = sorted(glob.glob("./macro_data/daily/*.csv"))
    if not files:
        return None, None
    try:
        d = pd.read_csv(files[-1], encoding="utf-8-sig", dtype={"code": str})
    except Exception:
        return None, None
    if "close" not in d.columns or "trading_value" not in d.columns or not len(d):
        return None, None
    d = d[(d["close"] > 1000) & (d["trading_value"] > 0)]      # 저가주 제외(호가단위 안정)
    if not len(d):
        return None, None
    top = d.nlargest(1, "trading_value").iloc[0]
    return str(top["code"]).zfill(6), float(top["close"])


def probe_kiwoom(price, code):
    """키움: 지정가(trde_tp=0) 1주 매수 시도 → (수용여부, 메시지)."""
    import kiwoom_trader as kt
    api = kt.get_api()
    try:
        r = api.order.stock_buy_order_request_kt10000(
            dmst_stex_tp="KRX", stk_cd=code, ord_qty="1",
            trde_tp="0", ord_uv=str(int(price)),
        )
        kt._assert_order_ok(r, "탐침매수")
        ono = kt._pick(r, "ord_no", "odno", default="")
        return True, f"접수됨(주문번호 {ono})"
    except Exception as e:
        return False, str(e)[:200]


def _is_fatal(label, msg):
    """'시간대라서 거부'(정상 결과)와 '계좌/인증이 죽어서 거부'(사고)를 구분한다.

    [2026-08-20 신설] 08-10 탐침에서 KIS 가
      "거부: 40910000 모의투자 주문이 불가한 계좌입니다"
    를 돌려줬는데, 탐침은 애초에 '거부되는 것이 정상'인 주문을 넣기 때문에 이걸
    평범한 거부로 취급해 판정문을 '장전 동시호가 주문 수용: kiwoom' 으로 적고
    끝냈다. 실제로는 그날부터 KIS 계좌가 매수·매도 전부를 거부하는 상태였고,
    만기가 지난 보유 1종목이 열흘 동안 청산되지 못했다. 계좌 단위 사유는 별도로
    분류해 긴급 경보로 올리고, 캐시도 무효화해 매일 다시 확인한다.
    """
    m = str(msg or "")
    if label == "kis":
        try:
            import kis_trader as kx
            return kx._is_account_blocked("", m)
        except Exception:
            pass
    # 아래 문구 목록은 kis_trader._BLOCKED_MSG_WORDS 와 의도적으로 중복이다 —
    # 탐침은 kis_trader 를 못 읽는 상황(자격증명 누락 등)에서도 판정해야 한다.
    return any(w in m for w in ("주문이 불가한 계좌", "사용할 수 없는 계좌",
                                "해지된 계좌", "정지된 계좌", "계좌가 없습니다"))


def probe_kis(price, code):
    """KIS: 지정가(ORD_DVSN=00) 1주 매수 시도 → (수용여부, 메시지)."""
    import requests
    import kis_trader as kx
    cl = kx.KISMockClient()
    try:
        # order_buy 와 동일 경로·TR, 다만 지정가(ORD_DVSN=00)+하한가 근처로 체결 차단
        body = {"CANO": kx._CANO, "ACNT_PRDT_CD": kx._ACNT_PRDT_CD,
                "PDNO": code, "ORD_DVSN": "00",
                "ORD_QTY": "1", "ORD_UNPR": str(int(price))}
        hk = cl._hashkey(body)
        r = requests.post(
            f"{kx.MOCK_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers=cl._hdrs("VTTC0802U", hk), json=body, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("rt_cd") != "0":
            return False, f"거부: {data.get('msg_cd','')} {data.get('msg1','')}"[:200]
        return True, f"접수됨(주문번호 {data.get('output', {}).get('odno', '')})"
    except Exception as e:
        return False, str(e)[:200]


def main():
    force = "--force" in sys.argv
    if os.path.exists(RESULT_PATH) and not force:
        with open(RESULT_PATH, encoding="utf-8") as f:
            prev = json.load(f)
        # 계좌 단위 사고가 기록돼 있으면 캐시를 신뢰하지 않는다 — 해소 여부를
        # 매일 다시 확인해야 하고, 해소 전까지는 매일 다시 알려야 한다.
        # fatal 키는 2026-08-20 신설 — 그 전 기록은 msg 를 다시 판정해 준다.
        stale_fatal = [k for k in ("kiwoom", "kis")
                       if prev.get(k, {}).get("fatal")
                       or _is_fatal(k, prev.get(k, {}).get("msg", ""))]
        if not stale_fatal:
            print(f"[probe] 이미 실측됨({prev.get('probed_at')}) — 재탐침 생략. "
                  f"결과: {prev.get('verdict')}")
            return
        print(f"[probe] 직전 기록에 계좌 이상({', '.join(stale_fatal)}) — 캐시 무시하고 재탐침")

    now = datetime.now()
    hm = now.strftime("%H:%M")
    if not ("08:30" <= hm < "09:00"):
        print(f"[probe] 현재 {hm} — 장 시작 동시호가(08:30~09:00) 구간이 아니라 탐침 무의미. 종료")
        return

    code, close = _pick_probe_stock()
    if not code or not close:
        print("[probe] 탐침 종목 선정 실패(일봉 데이터 없음) — 중단"); return
    price = _tick(close * PROBE_DISCOUNT)
    print(f"[probe] {code} 전일종가 {close:,.0f} → 지정가 {price:,}원(약 −29%, 체결불가) 1주로 탐침")

    res = {"probed_at": now.strftime("%Y-%m-%d %H:%M:%S"), "code": code, "price": price}
    fatal = []
    for label, fn in (("kiwoom", probe_kiwoom), ("kis", probe_kis)):
        try:
            ok, msg = fn(price, code)
        except Exception as e:
            ok, msg = False, f"탐침 예외: {type(e).__name__}: {e}"[:200]
        is_fatal = (not ok) and _is_fatal(label, msg)
        res[label] = {"accepted": ok, "msg": msg, "fatal": is_fatal}
        if is_fatal:
            fatal.append(label)
        mark = "계좌이상" if is_fatal else ("수용" if ok else "거부")
        print(f"[probe][{label}] {mark} — {msg}")

    accepted = [k for k in ("kiwoom", "kis") if res.get(k, {}).get("accepted")]
    res["verdict"] = (f"장전 동시호가 주문 수용: {', '.join(accepted)}" if accepted
                      else "양 계좌 모두 장전 주문 거부 — 09:00 트리거 유지가 정답")
    if fatal:
        res["verdict"] += f"  ※ 계좌 주문불가: {', '.join(fatal)}"
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"[probe] 판정: {res['verdict']}  → {RESULT_PATH}")

    try:
        import notifier
        if fatal:
            det = "\n".join(f"  {k}: {res[k]['msg'][:80]}" for k in fatal)
            notifier.safe_send(
                f"🚨 [탐침] 계좌가 주문을 받지 못합니다 — {', '.join(fatal)}\n{det}\n"
                f"  이 계좌는 매수·매도 전부 거부됩니다(만기 청산 포함).\n"
                f"  증권사 오픈API 포털에서 모의투자 계좌 기간만료/재발급을 확인해 주세요.")
        else:
            notifier.safe_send(
                f"🔬 [장전 동시호가 탐침] {res['verdict']}\n"
                f"  키움: {res['kiwoom']['msg'][:60]}\n  KIS: {res['kis']['msg'][:60]}\n"
                f"  (체결불가 지정가 1주 — 미체결분은 장 마감 시 자동 실효)")
    except Exception:
        pass


if __name__ == "__main__":
    main()
