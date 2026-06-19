"""공용 진행률 표시 — 오래 걸리는 배치 작업용 (퍼센트 + 게이지 + ETA).

사용 (인라인 — 터미널 1줄 유지, 권장):
    from progress import print_bar, print_event
    import time
    t0 = time.time()
    for i, item in enumerate(items, 1):
        ...
        print_bar(i, len(items), t0, extra=f"저장 {n_ok}")
    print()  # 완료 후 줄바꿈 1회

    # 경고·오류는 print_event 로 (현재 줄 지우고 새 줄 출력)
    print_event(f"[warn] {ticker} 실패")

사용 (문자열 반환 — 로그 파일용):
    s = bar(i, total, t0)
    print(s)  # 줄바꿈 포함
"""

import sys
import time


def _fmt_sec(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}시간{(sec % 3600) // 60}분"
    if sec >= 60:
        return f"{sec // 60}분{sec % 60:02d}초"
    return f"{sec}초"


def bar(done, total, t0, width=20, extra=""):
    """진행률 문자열 반환 (줄바꿈 없음). 로그 파일·비TTY 환경용."""
    if total <= 0:
        return ""
    pct = done / total
    filled = int(width * pct)
    gauge = "█" * filled + "░" * (width - filled)
    elapsed = time.time() - t0
    eta = (elapsed / done) * (total - done) if done else 0
    s = f"[{gauge}] {pct * 100:5.1f}% ({done:,}/{total:,}) 경과 {_fmt_sec(elapsed)} · 남은 ~{_fmt_sec(eta)}"
    if extra:
        s += f" | {extra}"
    return s


def print_bar(done, total, t0, width=20, extra=""):
    """TTY: \r 인라인 덮어씀 (터미널 1줄 유지).
    비TTY(스케줄러·리다이렉트): 10개마다 또는 완료 시 줄 출력.
    완료(done==total) 시 줄바꿈 자동 처리.
    """
    if total <= 0:
        return
    pct = done / total
    filled = int(width * pct)
    gauge = "█" * filled + "░" * (width - filled)
    elapsed = time.time() - t0
    eta = (elapsed / done) * (total - done) if done else 0
    s = f"[{gauge}] {pct * 100:5.1f}% {done:,}/{total:,} · 남은 ~{_fmt_sec(eta)}"
    if extra:
        s += f" | {extra}"

    if sys.stdout.isatty():
        # 터미널: 한 줄 덮어씀
        print(f"\r{s}", end="", flush=True)
        if done == total:
            print()  # 완료 시 줄 내림
    else:
        # 비TTY: 10개마다 또는 완료 시 줄 출력
        if done % 10 == 0 or done == total:
            print(s, flush=True)


def print_event(msg):
    """경고·오류·중요 이벤트 출력.
    TTY: 현재 인라인 줄 지우고 새 줄 → 다음 print_bar 가 다시 인라인 복원.
    비TTY: 그냥 print.
    """
    if sys.stdout.isatty():
        # 현재 줄 지우기 (ANSI 커서 맨 앞 + 줄 지우기)
        print(f"\r\033[K{msg}", flush=True)
    else:
        print(msg, flush=True)
