# -*- coding: utf-8 -*-
"""
대시보드 CSRF 방어 — 2026-09-04 (외부 코드 평가 P0b 지적 → 테스트 클라이언트로 재현 확인)

무엇이 뚫려 있었나
  /api/run/<task> 는 remote_addr 이 127.0.0.1 이면 통과했다. 사용자가 악성 페이지를 열어두면
  그 페이지의 JS 가
      fetch("http://127.0.0.1:5050/api/run/kiwoom_buy", {method:"POST", mode:"no-cors"})
  를 보낼 수 있고, 브라우저 입장에서 이건 **내 PC 에서 나가는 요청**이라 remote_addr 이
  127.0.0.1 로 찍힌다. 가드를 통과해 kiwoom_trader.py buy 가 실행된다. 응답은 못 읽어도
  주문은 이미 나갔다. 같은 날 붙인 인증 계층도 로컬은 면제라 소용이 없었다.
  Access-Control-Allow-Origin: * 는 여기에 더해 잔고·보유 응답을 타 사이트가 읽게 해줬다.

무엇으로 막나 (각각 단독으로도 충분한 세 겹 + 쿠키 한 겹)
  ① 상태변경 POST 에 커스텀 헤더 X-Dashboard-Action 필수 — 타 출처는 붙일 수 없다.
     값은 비밀이 아니어도 된다. 방어의 핵심은 '커스텀 헤더가 존재한다'는 사실이다.
  ② Origin 이 있으면 우리 호스트와 일치
  ③ Sec-Fetch-Site 가 있으면 same-origin
  ④ 세션 쿠키 SameSite=Lax — 타 사이트발 요청에 쿠키가 실리지 않는다
  그리고 CORS 와일드카드 제거 — / 가 HTML 을 직접 서빙하므로 file:// 근거는 사라졐다.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
ACTION = {"X-Dashboard-Action": "1"}                       # 대시보드 페이지 JS 가 붙이는 헤더
EVIL = {"Origin": "https://evil.example", "Referer": "https://evil.example/p"}


@pytest.fixture(scope="module")
def dash():
    import integrated_dashboard_server as d
    d.app.config["TESTING"] = True
    return d


@pytest.fixture
def with_pw(dash):
    orig = dash.DASH_PASSWORD
    dash.DASH_PASSWORD = "csrf-test-pw"
    yield dash
    dash.DASH_PASSWORD = orig


def test_cross_site_post_cannot_reach_the_executor(with_pw):
    """타 사이트발 POST 는 403. 실행기(400 '알 수 없는 작업')에 닿으면 뚫린 것이다."""
    with with_pw.app.test_client() as c:
        r = c.post("/api/run/__probe__", environ_base=LOCAL, headers=EVIL)
        assert r.status_code == 403, f"타 사이트발 POST 가 실행기에 도달했다: {r.status_code}"
        r = c.post("/api/intraday/refresh", environ_base=LOCAL, headers=EVIL)
        assert r.status_code == 403, "intraday/refresh 에 가드가 없다"
        # 헤더를 위조해 붙였더라도 Origin 이 다르면 거부 (2겹)
        r = c.post("/api/run/__probe__", environ_base=LOCAL, headers={**EVIL, **ACTION})
        assert r.status_code == 403, "Origin 불일치가 통과됐다"
        # Sec-Fetch-Site: cross-site 도 거부 (3겹)
        r = c.post("/api/run/__probe__", environ_base=LOCAL,
                   headers={**ACTION, "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403, "Sec-Fetch-Site cross-site 가 통과됐다"


def test_same_origin_action_passes_the_guard(with_pw):
    """대시보드 페이지 자신(같은 호스트 Origin + 헤더)은 실행기에 닿아야 한다."""
    with with_pw.app.test_client() as c:
        r = c.post("/api/run/__probe__", environ_base=LOCAL,
                   headers={**ACTION, "Origin": "http://localhost",
                            "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 400 and "알 수 없는 작업" in r.get_json()["msg"]
        # Origin 을 아예 안 보내는 클라이언트(curl 류)는 CSRF 벡터가 아니다 — 허용
        r = c.post("/api/run/__probe__", environ_base=LOCAL, headers=ACTION)
        assert r.status_code == 400


def test_no_cors_wildcard_anywhere(with_pw):
    """Access-Control-Allow-Origin 이 붙으면 타 사이트가 잔고 응답을 읽을 수 있다."""
    with with_pw.app.test_client() as c:
        for path in ("/", "/api/all", "/login"):
            r = c.get(path, environ_base=LOCAL, headers=EVIL)
            assert "Access-Control-Allow-Origin" not in r.headers, f"{path} 에 CORS 헤더가 남아 있다"
        r = c.options("/api/run/x", environ_base=LOCAL, headers=EVIL)
        assert "Access-Control-Allow-Origin" not in r.headers


def test_session_cookie_is_samesite_and_httponly(dash):
    assert dash.app.config.get("SESSION_COOKIE_SAMESITE") == "Lax"
    assert dash.app.config.get("SESSION_COOKIE_HTTPONLY") is True


def test_dashboard_js_sends_the_action_header():
    """서버가 헤더를 요구하는데 페이지가 안 보내면 버튼이 전부 죽는다 — 둘을 함께 묶는다."""
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    for call in ("fetch('/api/run/'+task,", "fetch('/api/intraday/refresh',"):
        i = src.find(call)
        assert i != -1, f"{call} 호출을 찾지 못했다"
        assert "X-Dashboard-Action" in src[i:i + 120], f"{call} 에 액션 헤더가 없다"
