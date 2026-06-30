"""
ci_gate.py — 파이프라인 회귀 테스트 게이트 + 실패 텔레그램 알림.

run_ai_pipeline.bat 가 학습 '전(preflight)'과 '후(postcheck)'에 호출한다.
  - gate 모드      : `pytest tests` 실행 → 실패 시 비0 종료(→ bat 의 errorlevel
                     게이팅이 학습/배포를 중단). '조용한 실패'를 막는 핵심.
  - notify-fail 모드: 단계 라벨을 받아 텔레그램 경보만 전송(bat 의 :fail 라벨에서 호출).

설계 의도: bat 은 ASCII 만 유지(cp949 파싱 사고 방지)하고, 한글 메시지는 이 .py(UTF-8)
          에서 만든다. 텔레그램은 notifier.safe_send 라 미설정/네트워크 문제에도 예외로
          죽지 않는다.

사용:
  python ci_gate.py gate                  # pytest tests, 실패 시 exit 1
  python ci_gate.py notify-fail preflight # 실패 알림 전송, exit 0
"""

import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

# bat 이 넘기는 ASCII 단계명 → 한글 라벨(텔레그램 메시지용)
STEP_KO = {
    "preflight": "학습 전 사전 회귀 테스트",
    "collect":   "데이터 수집",
    "dataset":   "학습셋 생성",
    "train":     "메타모델 학습",
    "postcheck": "학습 후 회귀 테스트(신규 모델 검증)",
}


def _notify(msg):
    """텔레그램 전송(예외 안전)."""
    try:
        from notifier import safe_send
        ok, info = safe_send(msg)
        print(f"[ci_gate] 텔레그램: {'전송됨' if ok else '스킵'} ({info})")
    except Exception as e:
        print(f"[ci_gate] 텔레그램 전송 실패(무시): {e}")


def run_gate():
    """pytest tests 실행. 통과=0, 실패=1.
    (텔레그램 경보는 bat 의 :fail 라벨이 notify-fail 로 일괄 전송하므로 여기선 종료코드만.)"""
    print("[ci_gate] pytest tests 실행 ...")
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], cwd=HERE)
        rc = r.returncode
    except Exception as e:
        print(f"[ci_gate] pytest 실행 불가: {e}")
        return 1
    print(f"[ci_gate] pytest 종료코드 {rc}")
    return 0 if rc == 0 else 1


def notify_fail(label):
    """파이프라인 abort 시 텔레그램 경보(항상 exit 0 — 알림은 부수효과)."""
    ko = STEP_KO.get(label, label or "알 수 없는 단계")
    _notify(
        "\U0001F6A8 AI 파이프라인 중단 — '" + ko + "' 단계 실패.\n"
        "학습/배포를 멈췄습니다. logs/ai_pipeline_*.log 와 "
        "`python -m pytest tests` 로 원인 확인 필요."
    )
    return 0


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "gate"
    if mode == "notify-fail":
        return notify_fail(args[1] if len(args) > 1 else "")
    return run_gate()


if __name__ == "__main__":
    sys.exit(main())
