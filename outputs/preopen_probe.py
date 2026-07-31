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
PROBE_CODE = "005930"          # 삼성전자 — 유동성 최상, 가격밴드 안정
PROBE_DISCOUNT = 0.71          # 전일종가 대비 −29% ≈ 하한가 근처(체결 불가)


def _tick(price):
    """호가단위 내림 정렬 — 잘못된 호가는 '가격 오류'로 거부돼 탐침 결과가 오염된다."""
    p = int(price)
    for lim, unit in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                      (200000, 100), (500000, 500)):
        if p < lim:
            return p - (p % unit)
    return p - (p % 1000)


def _prev_close(code):
    """최근 일봉 CSV 에서 전일 종가 — 하한가 근처 지정가 산출용."""
    import pandas as pd
    import glob
    files = sorted(glob.glob("./macro_data/daily/*.csv"))
    if not files:
        return None
    d = pd.read_csv(files[-1], encoding="utf-8-sig", dtype={"code": str})
    d["code"] = d["code"].astype(str).str.zfill(6)
    row = d[d["code"] == code]
    if not len(row):
        return None
    for c in ("close", "종가", "Close"):
        if c in d.columns:
            try:
                return float(row.iloc[0][c])
            except Exception:
                pass
    return None


def probe_kiwoom(price):
    """키움: 지정가(trde_tp=0) 1주 매수 시도 → (수용여부, 메시지)."""
    import kiwoom_trader as kt
    api = kt.get_api()
    try:
        r = api.order.stock_buy_order_request_kt10000(
            dmst_stex_tp="KRX", stk_cd=PROBE_CODE, ord_qty="1",
            trde_tp="0", ord_uv=str(int(price)),
        )
        kt._assert_order_ok(r, "탐침매수")
        ono = kt._pick(r, "ord_no", "odno", default="")
        return True, f"접수됨(주문번호 {ono})"
    except Exception as e:
        return False, str(e)[:200]


def probe_kis(price):
    """KIS: 지정가(ORD_DVSN=00) 1주 매수 시도 → (수용여부, 메시지)."""
    import requests
    import kis_trader as kx
    cl = kx.KISMockClient()
    try:
        # order_buy 와 동일 경로·TR, 다만 지정가(ORD_DVSN=00)+하한가 근처로 체결 차단
        body = {"CANO": kx._CANO, "ACNT_PRDT_CD": kx._ACNT_PRDT_CD,
                "PDNO": PROBE_CODE, "ORD_DVSN": "00",
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
        print(f"[probe] 이미 실측됨({prev.get('probed_at')}) — 재탐침 생략. "
              f"결과: {prev.get('verdict')}")
        return

    now = datetime.now()
    hm = now.strftime("%H:%M")
    if not ("08:30" <= hm < "09:00"):
        print(f"[probe] 현재 {hm} — 장 시작 동시호가(08:30~09:00) 구간이 아니라 탐침 무의미. 종료")
        return

    close = _prev_close(PROBE_CODE)
    if not close:
        print("[probe] 전일 종가 조회 실패 — 탐침 중단"); return
    price = _tick(close * PROBE_DISCOUNT)
    print(f"[probe] {PROBE_CODE} 전일종가 {close:,.0f} → 지정가 {price:,}원(약 −29%, 체결불가) 1주로 탐침")

    res = {"probed_at": now.strftime("%Y-%m-%d %H:%M:%S"), "price": price}
    for label, fn in (("kiwoom", probe_kiwoom), ("kis", probe_kis)):
        try:
            ok, msg = fn(price)
        except Exception as e:
            ok, msg = False, f"탐침 예외: {type(e).__name__}: {e}"[:200]
        res[label] = {"accepted": ok, "msg": msg}
        print(f"[probe][{label}] {'수용' if ok else '거부'} — {msg}")

    accepted = [k for k in ("kiwoom", "kis") if res.get(k, {}).get("accepted")]
    res["verdict"] = (f"장전 동시호가 주문 수용: {', '.join(accepted)}" if accepted
                      else "양 계좌 모두 장전 주문 거부 — 09:00 트리거 유지가 정답")
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"[probe] 판정: {res['verdict']}  → {RESULT_PATH}")

    try:
        import notifier
        notifier.safe_send(
            f"🔬 [장전 동시호가 탐침] {res['verdict']}\n"
            f"  키움: {res['kiwoom']['msg'][:60]}\n  KIS: {res['kis']['msg'][:60]}\n"
            f"  (체결불가 지정가 1주 — 미체결분은 장 마감 시 자동 실효)")
    except Exception:
        pass


if __name__ == "__main__":
    main()
