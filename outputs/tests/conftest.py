# -*- coding: utf-8 -*-
"""
tests/conftest.py — 테스트가 바깥 세상에 영향을 주지 않도록 봉인한다.
(2026-08-29 신설)

배경 — 실사고
  대시보드에서 AI 파이프라인을 돌렸더니 텔레그램으로 경보 3건이 날아왔다.
    · "[키움] 매수 기록이 없는 보유 종목 발견: ['444444']"
    · "[kiwoom_안C] 강도 확인 안 된 후보 1건은 안전하게 매수 제외"
    · "[kill_switch] 상태 파일을 읽지 못했습니다(JSONDecodeError...)"
  전부 **테스트가 보낸 것**이었다. 444444 는 테스트용 가짜 종목코드고,
  JSONDecodeError 는 fail-open 을 검증하려고 테스트가 일부러 만든 깨진 파일이다.
  ai_pipeline 이 ci_gate(pytest)를 preflight/post-train 두 번 돌리므로 매번 발송됐다.

왜 나쁜가
  ① 사용자가 진짜 사고와 구분할 수 없다 — 이 시스템은 알림으로 상태를 판단한다.
  ② 알림 피로를 만든다. 양치기 소년이 되면 진짜 경보를 놓친다.
  ③ 테스트는 '관찰만' 해야 한다. 바깥으로 나가는 부수효과가 있으면 그건 테스트가 아니다.

봉인 범위
  notifier.safe_send / notifier.send 를 세션 전체에서 무력화한다.
  검증이 필요한 테스트는 fixture `captured_notifications` 로 호출 내역을 받는다.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_sent = []


@pytest.fixture(autouse=True, scope="session")
def _block_outbound_notifications():
    """세션 전체에서 텔레그램 발송을 차단한다.

    autouse + session 스코프 — 개별 테스트가 깜빡해도 자동 적용된다.
    (이 사고의 교훈: '테스트마다 막기'는 반드시 하나를 빠뜨린다.)
    """
    try:
        import notifier
    except Exception:
        yield
        return

    orig_safe, orig_send = notifier.safe_send, notifier.send
    notifier.safe_send = lambda text: _sent.append(str(text))
    notifier.send = lambda text: _sent.append(str(text))
    try:
        yield
    finally:
        notifier.safe_send, notifier.send = orig_safe, orig_send


@pytest.fixture
def captured_notifications():
    """이 테스트 동안 발생한 알림 목록. 발송은 되지 않는다."""
    start = len(_sent)
    yield _sent
    del _sent[start:]
