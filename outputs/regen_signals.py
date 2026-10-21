# -*- coding: utf-8 -*-
"""
regen_signals.py — 전략 설정을 바꾼 뒤 '오늘(최근 거래일) 신호'를 새 설정으로 다시 만든다. (2026-10-21)

대시보드 '모의매매 전략 설정' 저장 시, 정기 신호(18:30)가 이미 지났거나 휴장일이면 이 스크립트를 띄운다 →
다음 거래일 09:00 매수가 새 전략으로 나간다. 옛 전략으로 이미 만든 오늘 신호는 CSV 에 남지만 트레이더가
'운용 중이 아닌 전략'으로 건너뛴다(kiwoom_trader / kis_trader). 같은 (날짜·종목·전략) 신호는 중복 저장 안 됨.
"""
import os
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    os.chdir(HERE)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    py = sys.executable
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    res = []
    # [2026-10-22] 강도 v2 를 쓰는데 모델이 없는 전략은 먼저 학습(신호 기록 때 v2 점수가 같이 남도록)
    try:
        import live_config as LC
        cfg = LC.load()
        if any(cfg["accounts"][a].get("strength_version") == "v2" for a in LC.ACCOUNTS):
            subprocess.run([py, "-u", "strength_model.py", "--train", "--missing-only"], cwd=HERE, env=env)
    except Exception as e:
        print(f"[regen] 강도 v2 학습 건너뜀: {e}")
    for script, label in (("live_signal.py", "키움"), ("kis_live_signal.py", "KIS")):
        print(f"[regen] {datetime.now():%H:%M:%S} {script} 시작", flush=True)
        r = subprocess.run([py, "-u", script], cwd=HERE, env=env)
        res.append(f"{label} {'✅' if r.returncode == 0 else '❌ rc=' + str(r.returncode)}")
        print(f"[regen] {script} 종료 rc={r.returncode}", flush=True)
    try:
        from notifier import safe_send
        safe_send("🔁 [전략 설정] 새 설정으로 오늘 신호를 다시 만들었습니다 — " + " / ".join(res)
                  + "\n다음 거래일 09:00 매수부터 새 전략으로 나갑니다.")
    except Exception:
        pass


if __name__ == "__main__":
    main()
