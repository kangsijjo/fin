# -*- coding: utf-8 -*-
"""
fin_paths.py — 프로젝트 루트 경로의 단일 출처. (2026-09-04 신설)

배경
  'C:/fin/...' 절대경로가 12곳에 흩어져 있었다 — 대부분 stock.db / .kiwoom.lock 의
  **폴백 후보**로. 지금은 1인 1머신이라 문제가 없지만, 2026-10-28 PC 이관에서 경로가
  달라지면 여러 파일이 **조용히** 깨진다. 폴백이라 에러조차 안 난다.
  (외부 코드 평가 P2 지적 → 실측 12곳)

우선순위
  1. 환경변수 FIN_ROOT      — 이관·테스트에서 덮어쓸 때
  2. 이 파일의 위치에서 추론 — outputs/ 의 부모. 저장소를 어디에 두든 따라간다.

쓰는 법
  from fin_paths import STOCK_DB, KIWOOM_LOCK
  경로는 문자열이 필요한 곳에서 str() 로 감싼다.
"""
import os
from pathlib import Path

FIN_ROOT    = Path(os.environ.get("FIN_ROOT") or Path(__file__).resolve().parent.parent)
OUTPUTS     = FIN_ROOT / "outputs"
STOCK_AI    = FIN_ROOT / "Stock_AI_Project"
STOCK_DB    = STOCK_AI / "data" / "stock.db"
KIWOOM_LOCK = STOCK_AI / "data" / ".kiwoom.lock"
LOGS_ROOT   = FIN_ROOT / "logs"
