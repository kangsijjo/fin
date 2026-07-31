# -*- coding: utf-8 -*-
"""
sitecustomize.py — 이 venv 로 실행되는 모든 파이썬 프로세스의 콘솔 인코딩을 UTF-8 로 고정.
(2026-07-21 신설)

배경: 한국어 Windows 콘솔 기본값이 cp949 라, 한글/이모지/em-dash 를 print 하는 스크립트가
      bat 래퍼(PYTHONIOENCODING=utf-8) 없이 실행되면 UnicodeEncodeError 로 즉사한다.
      이번 세션에서만 실제 사고가 4건 — make_trades_history_v3(데이터셋 재생성이 CSV 쓰기
      직전 사망), factor_scorer(초기화 자체가 죽어 강도 재계산 경로 위험), progress.py,
      트레이더 가드 print. 전수 조사 결과 비ASCII 를 print 하는 파일이 55개.

해결: 파일 55개를 각각 고치는 대신, 파이썬이 기동 시 자동 import 하는 sitecustomize 를
      venv 에 두어 한 번에 처리한다. 새로 추가되는 파일도 자동으로 보호된다.
      (bat 의 PYTHONIOENCODING 은 그대로 유지 — 이중 안전망이고, 다른 venv/시스템
       파이썬으로 실행될 때를 위한 방어이기도 하다.)

설치: 이 파일을 .venv/Lib/site-packages/sitecustomize.py 로 복사한다.
      venv 를 다시 만들면 사라지므로 원본을 outputs/ 에 두고 git 으로 추적한다.
      복사: copy outputs\\sitecustomize.py outputs\\.venv\\Lib\\site-packages\\
"""
import sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            # errors="replace": 표현 불가 문자는 대체문자로 — print 때문에 프로세스가
            # 죽는 일은 없어야 한다(로그 가독성보다 실행 지속이 우선).
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
