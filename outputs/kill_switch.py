# -*- coding: utf-8 -*-
"""
kill_switch.py — 계좌 단위 신규매수 차단 스위치 (3층 안전장치의 3층).
(2026-08-27 신설)

배경
  1·2층은 '시장이 나쁜 것'을 다루고, 이 층은 **우리 쪽이 고장난 것**을 다룬다.
  2026-08 한 달에만 이런 일이 있었다 — 전부 사람이 눈치채기 전까지 방치됐다.
    · bat 손상으로 KIS 트레이더가 3일간 로그도 없이 무실행
    · KIS 계좌가 12일간 전 주문 거부(40910000) — 만기 초과 보유 방치
    · 재시도 안전장치가 7월 도입 이후 한 번도 작동하지 않음
  watchdog 은 **알리기만 했고 멈추지는 않았다.** 이 모듈이 그 공백을 메운다.

설계 원칙
  ① **매도는 절대 막지 않는다.** 만기청산·손절·서킷브레이커는 그대로 집행한다.
     손실이 커지는 국면에서 청산까지 멈추면 위험이 오히려 커진다. 막는 것은 신규매수뿐.
  ② **해제는 사람만.** 자동 해제가 있으면 '고장난 채로 재개'가 가능해진다.
     원인을 사람이 확인했다는 사실 자체가 해제 조건이다.
  ③ **임계 최적화가 필요 없는 조건만 넣는다.** 무실행·주문불가·청산실패는
     "몇 %가 최적인가"를 물을 필요가 없다 — 발생 자체가 사고다.
  ④ **읽기 실패는 fail-open(매매 진행) + 시끄럽게 알림.** 파일 글리치로 매매가
     조용히 멈추면 이 모듈이 막으려던 바로 그 실패 유형을 새로 만드는 셈이다.
     단, 침묵하지 않도록 반드시 경보한다.

사용
  트레이더:  from kill_switch import guard_buy;  guard_buy("키움 안C")  # 차단 시 SystemExit
  watchdog:  from kill_switch import engage;     engage("no_run", "...")
  사람:      python kill_switch.py status
             python kill_switch.py release "원인 확인함 - bat 수정 완료"
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_HERE, "db", "kill_switch.json")

# 임계 최적화가 필요 없는 조건만. "발생 자체가 사고"인 것들.
REASONS = {
    "no_run":        "트레이더가 여러 거래일 연속 실행되지 않음",
    "order_blocked": "계좌가 주문을 거부(계좌 단위 오류)",
    "exit_failed":   "만기/손절 청산이 반복 실패",
    "manual":        "사람이 직접 정지",
}


def _read():
    """상태 로드. (state|None, error|None) — 파일 없음은 정상(미발동)."""
    if not os.path.exists(STATE_PATH):
        return None, None
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _write(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)      # 원자적 교체 — 반쯤 쓰인 상태 파일 방지


def status():
    """(engaged: bool, state: dict|None, read_error: str|None)"""
    st, err = _read()
    if err:
        return False, None, err          # 읽기 실패 = fail-open
    if not st or not st.get("engaged"):
        return False, st, None
    return True, st, None


def engage(reason: str, detail: str = "", notify: bool = True) -> bool:
    """스위치를 건다. 이미 걸려 있으면 아무것도 하지 않는다(알림 스팸 방지).
    반환: 이번 호출로 새로 걸렸으면 True."""
    on, st, _ = status()
    if on:
        return False
    state = {
        "engaged": True,
        "reason": reason,
        "reason_text": REASONS.get(reason, reason),
        "detail": detail,
        "engaged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write(state)
    msg = (f"🚨 [신규매수 정지] {state['reason_text']}\n"
           f"  {detail}\n"
           f"  · 매도·만기청산·손절은 계속 집행됩니다(신규 매수만 차단).\n"
           f"  · 원인 확인 후 해제: python kill_switch.py release \"확인 내용\"")
    print(msg)
    if notify:
        try:
            import notifier
            notifier.safe_send(msg)
        except Exception:
            pass
    return True


def release(note: str = "", notify: bool = True) -> bool:
    """사람이 해제. 반환: 실제로 해제했으면 True."""
    on, st, _ = status()
    if not on:
        print("[kill_switch] 이미 해제 상태입니다.")
        return False
    prev = dict(st or {})
    _write({
        "engaged": False,
        "released_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "release_note": note,
        "prev": prev,
    })
    msg = (f"✅ [신규매수 재개] 정지 해제\n"
           f"  정지 사유였던 것: {prev.get('reason_text','?')} ({prev.get('engaged_at','?')})\n"
           f"  해제 메모: {note or '(없음)'}")
    print(msg)
    if notify:
        try:
            import notifier
            notifier.safe_send(msg)
        except Exception:
            pass
    return True


def guard_buy(account_label: str = ""):
    """매수 진입점에서 호출. 걸려 있으면 프로세스를 정상 종료(exit 0)한다.

    exit 0 인 이유: 이것은 '실패'가 아니라 **의도된 정지**다. exit 1 로 끝내면
    bat 재시도 루프가 5분 뒤 또 시도하고, 작업 스케줄러도 실패로 기록해
    '고장'과 '의도된 정지'가 로그에서 구분되지 않는다.
    """
    on, st, err = status()
    if err:
        # fail-open: 상태를 못 읽었다고 매매를 멈추지는 않는다. 다만 침묵하지 않는다.
        warn = (f"⚠ [kill_switch] 상태 파일을 읽지 못했습니다({err}) — "
                f"매매는 그대로 진행합니다. db/kill_switch.json 확인 요망.")
        print(warn)
        try:
            import notifier
            notifier.safe_send(warn)
        except Exception:
            pass
        return
    if not on:
        return
    print(f"\n[kill_switch] 신규매수 정지 상태 — {account_label} 매수를 건너뜁니다.")
    print(f"  사유: {st.get('reason_text','?')} / {st.get('detail','')}")
    print(f"  발동: {st.get('engaged_at','?')}")
    print(f"  해제: python kill_switch.py release \"확인 내용\"")
    sys.exit(0)


def _cli():
    argv = sys.argv[1:]
    cmd = (argv[0] if argv else "status").lower()
    if cmd == "status":
        on, st, err = status()
        if err:
            print(f"[kill_switch] 상태 파일 읽기 실패: {err} (매매는 진행됨 — fail-open)")
            return 2
        if not on:
            print("[kill_switch] 정상 — 신규매수 허용")
            if st and st.get("released_at"):
                print(f"  마지막 해제: {st['released_at']}  메모: {st.get('release_note','')}")
            return 0
        print("[kill_switch] 🚨 신규매수 정지 중")
        print(f"  사유  : {st.get('reason_text')} ({st.get('reason')})")
        print(f"  상세  : {st.get('detail')}")
        print(f"  발동  : {st.get('engaged_at')}")
        return 1
    if cmd == "engage":
        reason = argv[1] if len(argv) > 1 else "manual"
        detail = argv[2] if len(argv) > 2 else "수동 정지"
        engage(reason, detail)
        return 0
    if cmd == "release":
        release(argv[1] if len(argv) > 1 else "")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
