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
      [2026-08-21] 접수된 키움 탐침 주문은 **즉시 취소**한다(_cancel_kiwoom, kt10003).
      종전엔 '장 마감 자동 실효'에 맡겼는데 **그 전제 자체가 틀렸다** — 08-21 22시
      실측에서 08:50 에 넣은 주문(0001240 알테오젠 241,000원)이 `ord_stt=접수` 로
      그대로 살아 있었다. 즉 미체결분은 마감 후에도 남아 예수금을 계속 묶고,
      다음 거래일 매수 예산까지 깎는다.
      탐침이 1회성일 땐 눈에 안 띄었지만 계좌 이상 시 매일 재탐침하도록 바꾸면서
      매일 24.1만원이 하루 종일 잠기는 문제가 됐다(실측 08-21: 9,158,011 -> 8,916,171).
      KIS 는 취소 경로를 안 쓴다 — 애초에 접수가 안 되는 상태라 취소할 주문이 없다.

결과는 db/preopen_probe_result.json 에 기록되며, 기록이 있으면 다시 탐침하지 않는다.
단, 계좌 단위 이상(fatal)이 기록된 계좌만 매일 재탐침한다 — 정상 계좌는 다시 찌르지
않는다(불필요한 주문 = 예수금 묶임).
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



PROBE_ORDER_LOG = "./db/preopen_probe_orders.json"


def _remember_probe_order(code, ono):
    """탐침이 넣은 주문번호를 남긴다 — 취소 실패/프로세스 사망 시 회수 근거."""
    import json as _j
    try:
        rec = []
        if os.path.exists(PROBE_ORDER_LOG):
            rec = _j.load(open(PROBE_ORDER_LOG, encoding="utf-8"))
        rec.append({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "code": code, "ord_no": str(ono)})
        os.makedirs(os.path.dirname(PROBE_ORDER_LOG), exist_ok=True)
        with open(PROBE_ORDER_LOG, "w", encoding="utf-8") as f:
            _j.dump(rec[-20:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def cleanup_leftover_probe_orders(verbose=True):
    """살아남은 탐침 주문을 찾아 취소한다. 반환: 취소한 건수.

    [2026-08-21 신설] '체결 불가 지정가라 장 마감에 자동 실효된다'는 최초 설계
    전제가 실측으로 깨졌다 — 08-21 08:50 주문이 그날 밤까지 `접수` 상태로 남아
    키움 예수금 241,840원을 계속 묶었다(9,158,011 -> 8,916,171). 미체결이 남으면
    ① 다음 거래일 매수 예산이 그만큼 깎이고 ② 급락 시 체결될 위험도 0 이 아니다.
    그래서 탐침 실행 때마다 먼저 잔재를 회수한다.

    판별: 미체결 목록에서 '탐침이 기록한 주문번호'와 일치하는 건만 취소한다.
    사용자가 낸 주문이나 트레이더의 익절 지정가를 건드리지 않기 위함이다.
    """
    import json as _j
    try:
        rec = _j.load(open(PROBE_ORDER_LOG, encoding="utf-8"))
    except Exception:
        rec = []
    mine = {str(r.get("ord_no")): r for r in rec if r.get("ord_no")}
    if not mine:
        return 0

    try:
        import kiwoom_trader as kt
        api = kt.get_api()
        r = api.acct.unfilled_orders_request_ka10075(
            all_stk_tp="0", trde_tp="0", stex_tp="0")
        rows = r.get("oso") or r.get("output") or []
    except Exception as e:
        if verbose:
            print(f"[probe][cleanup] 미체결 조회 실패({str(e)[:60]}) — 생략")
        return 0

    n = 0
    for x in rows:
        ono = str(x.get("ord_no", ""))
        if ono not in mine:
            continue                      # 탐침이 낸 주문이 아니면 절대 건드리지 않는다
        code = str(x.get("stk_cd", "")).zfill(6)
        try:
            kt._assert_order_ok(api.order.stock_cancel_order_request_kt10003(
                dmst_stex_tp="KRX", orig_ord_no=ono, stk_cd=code, cncl_qty="0"),
                "탐침잔재취소")
            print(f"[probe][cleanup] 남아 있던 탐침 주문 {ono} ({code} "
                  f"{x.get('stk_nm','')}) 취소 — 예수금 회수")
            n += 1
        except Exception as e:
            print(f"[probe][cleanup][warn] {ono} 취소 실패: {str(e)[:80]}")
    if n:
        try:
            keep = [r for r in rec if str(r.get("ord_no")) not in
                    {str(x.get("ord_no")) for x in rows}]
            with open(PROBE_ORDER_LOG, "w", encoding="utf-8") as f:
                _j.dump(keep, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return n


def _cancel_kiwoom(msg, code):
    """접수된 키움 탐침 주문을 즉시 취소한다. 반환: 취소 결과 문자열.

    [2026-08-21 신설] 탐침은 체결 불가능한 −29% 지정가지만, **미체결분은 장 마감
    후에도 사라지지 않는다** — 08-21 22시 실측에서 08:50 주문이 `접수` 상태로
    그대로 살아 키움 예수금 241,840원을 계속 묶고 있었다(9,158,011 -> 8,916,171).
    최초 설계의 '마감 자동 실효' 전제가 틀렸던 것이다. 키움은 취소 API(kt10003)가
    있으니 바로 회수한다. 취소가 실패해도 주문번호를 남겨
    cleanup_leftover_probe_orders 가 다음 실행에서 회수하므로 예외는 던지지 않는다.
    """
    import re as _re
    m = _re.search(r"주문번호\s*(\S+)", msg or "")
    if not m:
        return "주문번호 미상 — 취소 생략"
    ono = m.group(1)
    _remember_probe_order(code, ono)      # 취소가 실패해도 나중에 회수할 수 있게
    try:
        import kiwoom_trader as kt
        api = kt.get_api()
        kt._assert_order_ok(api.order.stock_cancel_order_request_kt10003(
            dmst_stex_tp="KRX", orig_ord_no=str(ono), stk_cd=code, cncl_qty="0"),
            "탐침취소")
        print(f"[probe][kiwoom] 탐침 주문 {ono} 취소 완료 — 예수금 즉시 회수")
        return f"취소됨({ono})"
    except Exception as e:
        print(f"[probe][kiwoom][warn] 탐침 주문 {ono} 취소 실패({str(e)[:80]}) "
              f"— 주문번호를 남겼으니 다음 탐침 실행이 회수한다(그때까지 예수금 묶임)")
        return f"취소실패: {str(e)[:80]}"


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


def probe_targets(prev):
    """직전 결과를 보고 '이번에 다시 찔러야 할 계좌' 목록을 정한다.

    계좌 단위 사고가 기록돼 있으면 캐시를 신뢰하지 않는다 — 해소 여부를 매일 다시
    확인해야 하고, 해소 전까지 매일 알려야 한다. 다만 **이상이 있는 계좌만** 찌른다:
    탐침은 실주문이라, 정상 계좌에 매일 넣으면 그 금액이 장중 내내 예수금에서
    묶인다(2026-08-21 실측 — 키움 24.1만원). fatal 키는 2026-08-20 신설이라
    그 전 기록은 msg 로 다시 판정해 준다.
    반환: [] 이면 재탐침 불필요.
    """
    return [k for k in ("kiwoom", "kis")
            if (prev.get(k, {}) or {}).get("fatal")
            or _is_fatal(k, (prev.get(k, {}) or {}).get("msg", ""))]


def main():
    if "--cleanup" in sys.argv:
        n = cleanup_leftover_probe_orders()
        print(f"[probe] 잔재 정리 완료 — {n}건 취소")
        return

    # 매 실행 첫 순서로 지난 탐침의 잔재부터 회수한다(예수금 묶임 방지).
    try:
        cleanup_leftover_probe_orders(verbose=False)
    except Exception:
        pass

    force = "--force" in sys.argv
    targets = ["kiwoom", "kis"]          # 첫 탐침(또는 --force)은 양 계좌
    if os.path.exists(RESULT_PATH) and not force:
        with open(RESULT_PATH, encoding="utf-8") as f:
            prev = json.load(f)
        stale_fatal = probe_targets(prev)
        if not stale_fatal:
            print(f"[probe] 이미 실측됨({prev.get('probed_at')}) — 재탐침 생략. "
                  f"결과: {prev.get('verdict')}")
            return
        # [2026-08-21] 재탐침 대상을 '이상이 있는 계좌'로 한정한다.
        #   종전엔 fatal 이 하나라도 있으면 양 계좌를 매일 다시 찔렀는데, 키움은
        #   이미 08-10 에 '수용' 으로 확정된 상태라 다시 찌를 이유가 없었다.
        #   그런데 탐침 주문은 취소 API 를 안 쓰고 장 마감 실효에 맡기는 구조여서,
        #   접수된 1주(지정가 24.1만원)가 **장중 내내 예수금을 묶었다**
        #   (실측 08-21: 키움 예수금 9,158,011 -> 8,916,171, 미체결 1건).
        #   슬롯당 예산이 89만원 수준이라 매수가 나가는 날엔 그대로 손실이다.
        targets = list(stale_fatal)
        print(f"[probe] 직전 기록에 계좌 이상({', '.join(targets)}) — 해당 계좌만 재탐침"
              f"(정상 계좌는 재주문하지 않는다 — 예수금 묶임 방지)")

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
    prev_res = {}
    if os.path.exists(RESULT_PATH):
        try:
            with open(RESULT_PATH, encoding="utf-8") as f:
                prev_res = json.load(f)
        except Exception:
            prev_res = {}

    fatal = []
    for label, fn, canceller in (("kiwoom", probe_kiwoom, _cancel_kiwoom),
                                 ("kis", probe_kis, None)):
        if label not in targets:
            # 이번엔 찌르지 않는다 — 직전 실측을 그대로 승계(주문을 아끼는 것이 목적)
            keep = prev_res.get(label)
            if keep:
                res[label] = dict(keep)
                res[label]["carried_from"] = prev_res.get("probed_at", "")
                print(f"[probe][{label}] 재탐침 생략(직전 결과 승계) — {keep.get('msg','')[:60]}")
                if keep.get("fatal"):
                    fatal.append(label)
            continue
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
        # 접수됐으면 즉시 취소 — 미체결이 장중 내내 예수금을 묶는 것을 막는다.
        if ok and canceller:
            res[label]["cancelled"] = canceller(msg, code)

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
