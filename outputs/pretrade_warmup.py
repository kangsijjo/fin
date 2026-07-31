# -*- coding: utf-8 -*-
"""
pretrade_warmup.py — 장 시작 전(08:50) 매매 사전 예열.  (2026-07-21 신설)

목적: 09:01/09:03 매매가 '즉시 발사'되도록, 시간이 걸리거나 실패할 수 있는 준비 작업을
      장 열기 전에 미리 끝낸다. 실측상 주문은 프로세스 시작 후 2~3초에 나가지만,
      아래 두 경우엔 09시에 지연·차단이 생긴다 — 그걸 08:50 으로 옮긴다.

  ① 강도 사전점검: 전날 18:30 강도 로깅이 실패했으면 09시에 즉석 재계산(6~10초)이
     돌거나, 그마저 실패하면 fail-closed 로 당일 매수가 통째로 차단된다.
     → 08:50 에 미리 점검·재계산해 두면 09시는 항상 조회만(0초), 문제가 있어도
       장 열기 전에 텔레그램으로 알아 손 쓸 시간이 생긴다.
  ② API 연결 예열: 토큰 발급/네트워크 이상을 장 전에 발견(09시 첫 실패보다 10분 빠름).
     KIS 는 토큰이 파일 캐시(.kis_mock_token.json)라 09시 프로세스가 재사용까지 한다.

주의: 잔고(예수금)는 여기서 확정하지 않는다 — 오전 매매는 '손절매도 → 매도대금 반영
      → 매수' 순서라 08:50 잔고를 쓰면 예산을 과소평가해 매수가 축소된다.
      잔고 재확인은 09시 매수 직전(cmd_buy 내부)이 정답.

실행: python pretrade_warmup.py         (양 계좌)
      python pretrade_warmup.py kiwoom  /  kis
"""
import sys
import traceback
from datetime import datetime

# 콘솔 인코딩 방탄(타 파일과 동일 규약)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _notify(msg):
    try:
        import notifier
        notifier.safe_send(msg)
    except Exception:
        pass


def warm_kiwoom():
    """키움: 강도 사전점검 + 토큰 예열. 반환: 문제 요약 문자열(없으면 None)."""
    import kiwoom_trader as kt
    problems = []

    sigs = kt.todays_signals()
    if not sigs:
        print("[warmup][키움] 오늘 매수 신호 없음 — 강도 점검 생략")
    else:
        strength = kt._strength_map(sigs)
        strength, missing = kt.verify_strength(sigs, strength)
        ok = [v for k, v in strength.items() if v >= kt.MIN_STRENGTH_SCORE]
        print(f"[warmup][키움] 신호 {len(sigs)}건 / 강도확보 {len(strength)}건 / "
              f"임계 {kt.MIN_STRENGTH_SCORE} 통과 {len(ok)}건 / 미확보 {len(missing)}건")
        if missing:
            problems.append(f"키움 강도 미확보 {len(missing)}건(해당 후보 매수 차단 예정)")

    # 토큰/연결 예열 — 실패 시 장 전에 알림(09시 첫 실패보다 10분 빠르다)
    try:
        kt.get_api()
        print("[warmup][키움] API 토큰 예열 OK")
    except Exception as e:
        print(f"[warmup][키움] API 예열 실패: {e}")
        problems.append(f"키움 API 예열 실패({type(e).__name__})")

    return " / ".join(problems) if problems else None


def warm_kis():
    """KIS: 강도 사전점검 + 토큰 예열(파일 캐시라 09시 프로세스가 재사용)."""
    import kis_trader as kx
    problems = []

    sigs = kx.todays_signals()
    if not sigs:
        print("[warmup][KIS] 오늘 매수 신호 없음 — 강도 점검 생략")
    else:
        strength = kx._strength_map(sigs)
        strength, missing = kx.verify_strength(sigs, strength)
        ok = [v for k, v in strength.items() if v >= kx.MIN_STRENGTH_SCORE]
        print(f"[warmup][KIS] 신호 {len(sigs)}건 / 강도확보 {len(strength)}건 / "
              f"임계 {kx.MIN_STRENGTH_SCORE} 통과 {len(ok)}건 / 미확보 {len(missing)}건")
        if missing:
            problems.append(f"KIS 강도 미확보 {len(missing)}건(해당 후보 매수 차단 예정)")

    try:
        cl = kx.KISMockClient()
        cl.get_token()
        print("[warmup][KIS] API 토큰 예열 OK(파일 캐시 → 09:01 재사용)")
    except Exception as e:
        print(f"[warmup][KIS] API 예열 실패: {e}")
        problems.append(f"KIS API 예열 실패({type(e).__name__})")

    return " / ".join(problems) if problems else None


def main():
    which = (sys.argv[1].lower() if len(sys.argv) > 1 else "both")
    print(f"=== 매매 사전 예열 {datetime.now():%Y-%m-%d %H:%M:%S} ({which}) ===")
    problems = []

    if which in ("both", "kiwoom"):
        try:
            p = warm_kiwoom()
            if p:
                problems.append(p)
        except Exception as e:
            traceback.print_exc()
            problems.append(f"키움 예열 예외({type(e).__name__})")

    if which in ("both", "kis"):
        try:
            p = warm_kis()
            if p:
                problems.append(p)
        except Exception as e:
            traceback.print_exc()
            problems.append(f"KIS 예열 예외({type(e).__name__})")

    if problems:
        msg = "⚠ [사전예열 08:50] " + " / ".join(problems) + " — 장 시작 전 점검 요망"
        print(msg)
        _notify(msg)
        # 예열 실패는 '매매 실패'가 아니다(09시 본 실행이 재시도한다) — 종료코드 0 유지해
        # bat 재시도 루프가 예열을 반복하지 않게 한다. 알림으로 사람이 판단.
    else:
        print("[warmup] 사전 예열 정상 — 09시 매매 준비 완료")


if __name__ == "__main__":
    main()
