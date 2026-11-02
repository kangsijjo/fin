# -*- coding: utf-8 -*-
"""
real_account.py — 키움 본계좌(실계좌, 실제 돈) 자동매매의 상태 파일 단일 출처. (2026-11-02, 설명서 §96)

모드  db/kiwoom_real/mode.json
      dryrun(기본) — 실제 주문을 내지 않는다. 계좌 조회·후보 선정·주문 수량 계산까지 다 하고 '냈을 주문'만 기록한다.
      live         — 실제 주문. **사람만 켠다**(대시보드에서 두 번 확인). 프로그램·작업 스케줄러는 이 값을 바꾸지 않는다.
      off          — 본계좌 매매·신호를 아예 돌리지 않는다.
      파일이 없거나 깨졌으면 dryrun — 실주문이 나가지 않는 쪽으로 넘어진다.
정지  db/kiwoom_real/halt.json — 신규 매수만 막는다(만기·손절·익절 매도는 계속). 일일 손실 한도가 걸거나 사람이 건다.
      until(YYYYMMDD) 이 있으면 그날까지만(다음 거래일 자동 해제), 없으면 사람이 풀 때까지.
      파일이 깨졌으면 '정지'로 본다(실제 돈 — 모르면 사지 않는다) + 알림. 전체 정지 스위치(kill_switch, 모의 포함)와는 별개.
손실  day_pnl — 프로그램이 산 몫의 오늘 손익(전일 종가 대비, 오늘 산 종목은 매입가 대비). 순수 함수.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_DIR = os.path.join(HERE, "db", "kiwoom_real")
MODES = {"dryrun": "드라이런(주문 안 냄 — 냈을 주문만 기록)", "live": "실주문", "off": "끔"}
LABEL = "키움 본계좌"


def _path(name):
    return os.path.join(REAL_DIR, name)


def read_json(name):
    """(dict|None, 오류문구|None) — 파일 없음은 (None, None)."""
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def write_json(name, d):
    """원자 저장(tmp → replace) — 반쯤 쓰인 상태 파일 방지."""
    os.makedirs(REAL_DIR, exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(name))


def _notify(msg):
    print(msg, flush=True)
    try:
        import notifier
        notifier.safe_send(msg)
    except Exception:
        pass


# ── 모드 ─────────────────────────────────────────────────────────────────────
def get_mode():
    d, _err = read_json("mode.json")
    m = (d or {}).get("mode")
    return m if m in MODES else "dryrun"


def mode_info():
    d, err = read_json("mode.json")
    d = d or {}
    return {"mode": get_mode(), "label": MODES[get_mode()], "changed_at": d.get("changed_at"),
            "note": d.get("note", ""), "prev": d.get("prev"), "error": err}


def mode_history(n=10):
    try:
        with open(_path("mode_history.jsonl"), encoding="utf-8") as f:
            return [json.loads(x) for x in f.readlines()[-n:] if x.strip()][::-1]
    except Exception:
        return []


def set_mode(mode, note="", who="dashboard", notify=True):
    """모드 변경(사람이 대시보드에서 두 번 확인한 뒤에만 부른다) — 기록 + 이력 1줄 + 텔레그램."""
    if mode not in MODES:
        raise ValueError(f"모드는 {', '.join(MODES)} 중 하나")
    prev = get_mode()
    rec = {"mode": mode, "changed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "note": str(note or "")[:200], "by": who, "prev": prev}
    write_json("mode.json", rec)
    try:
        with open(_path("mode_history.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    if notify:
        head = ("💰🔴 [키움 본계좌] 실주문 켜짐 — 다음 매매 시각부터 실제 돈으로 주문합니다" if mode == "live"
                else f"💰 [키움 본계좌] 모드 변경: {MODES.get(prev, prev)} → {MODES[mode]}")
        _notify(head + (f"\n  메모: {rec['note']}" if rec["note"] else ""))
    return rec


# ── 신규 매수 정지(손실 한도·수동) ─────────────────────────────────────────────
HALT_REASONS = {"loss_limit": "일일 손실 한도 도달", "manual": "사람이 직접 정지", "config": "설정 파일 문제"}


def halt_status(today=None):
    """(정지중?, 상태 dict|None). until 이 오늘보다 앞이면(그날만 정지였음) 풀린 것으로 본다."""
    d, err = read_json("halt.json")
    if err:
        return True, {"engaged": True, "reason": "state_error",
                      "reason_text": f"정지 상태 파일을 읽지 못함({err}) — 안전하게 신규 매수 정지", "error": err}
    if not d or not d.get("engaged"):
        return False, d
    until = d.get("until")
    today = today or datetime.now().strftime("%Y%m%d")
    if until and today > str(until):
        return False, d
    return True, d


def halt_engage(reason, detail="", until=None, notify=True):
    """정지 건다(이미 정지 중이면 아무것도 안 함). 반환: 새로 걸었으면 True."""
    on, _st = halt_status()
    if on:
        return False
    rec = {"engaged": True, "reason": reason, "reason_text": HALT_REASONS.get(reason, reason),
           "detail": str(detail or "")[:300], "engaged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "until": until}
    write_json("halt.json", rec)
    if notify:
        _notify(f"🛑 [키움 본계좌] 신규 매수 정지 — {rec['reason_text']}\n  {rec['detail']}\n"
                f"  · 만기·손절·익절 매도는 그대로 나갑니다(새로 사는 것만 멈춤).\n"
                + ("  · 다음 거래일에 자동으로 다시 삽니다." if until else
                   "  · 다시 사려면 대시보드 '본계좌' 탭에서 [신규 매수 재개]를 누르세요."))
    return True


def halt_release(note="", notify=True):
    """사람이 푼다(파일이 깨졌어도 새로 써서 푼다). 반환: 실제로 풀었으면 True."""
    on, st = halt_status()
    d, err = read_json("halt.json")
    if not on and not err:
        return False
    write_json("halt.json", {"engaged": False, "released_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "release_note": str(note or "")[:200], "prev": st or {}})
    if notify:
        _notify(f"✅ [키움 본계좌] 신규 매수 재개 — 정지 사유였던 것: {(st or {}).get('reason_text', '?')}"
                + (f"\n  메모: {note}" if note else ""))
    return True


# ── 일일 손익(손실 한도 판정) ──────────────────────────────────────────────────
def day_pnl(own, bought_today=(), prev_close=None):
    """프로그램 몫의 오늘 손익 → (원, 반영 종목 수).

    own: {code: {qty, price(현재가), avg_price, pred_close(전일종가, 없으면 0)}} — 원장 수량으로 자른 '프로그램 몫'
    기준가: 오늘 산 종목은 매입가, 그 외는 전일 종가(브로커 값 → 일봉 → 매입가 순으로 대체).
    오늘 이미 판(체결된) 종목은 잔고에 없어 빠진다 — 익절 체결은 대개 이익이라 빼면 손실 쪽으로 보수적.
    """
    tot, n = 0.0, 0
    for code, p in (own or {}).items():
        try:
            q = int(float(p.get("qty") or 0))
            cur = float(p.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if q <= 0 or cur <= 0:
            continue
        if code in bought_today:
            base = float(p.get("avg_price") or 0)
        else:
            base = (float(p.get("pred_close") or 0) or float((prev_close or {}).get(code) or 0)
                    or float(p.get("avg_price") or 0))
        if base != base or base <= 0:          # NaN·0 은 판정 불가 → 뺀다
            continue
        tot += (cur - base) * q
        n += 1
    return int(round(tot)), n


def loss_limit_hit(pnl, limit):
    """한도(원, 양수)가 있고 오늘 손익이 -한도 이하면 True."""
    try:
        return limit is not None and float(limit) > 0 and float(pnl) <= -abs(float(limit))
    except (TypeError, ValueError):
        return False
