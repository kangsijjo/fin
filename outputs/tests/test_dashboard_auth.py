# -*- coding: utf-8 -*-
"""
대시보드 원격 접속 인증 — 2026-08-30 신설

배경
  대시보드는 host="0.0.0.0" 으로 떠 있는데 **인증이 하나도 없었다**.
  잔고, 보유종목, 매매이력, 로그가 접근만 하면 그대로 보였다.
  Tailscale 로 집 밖에서 보게 되면서 인증 계층을 넣었다.

여기서 지키려는 것
  ① 로컬(같은 PC)은 종전과 똑같이 동작한다 — 버튼과 자동화가 깨지면 안 된다.
  ② 비밀번호가 없으면 원격은 **차단**된다(fail-closed). 실수로 열리면 안 된다.
  ③ 인증을 통과해도 **원격에서 실행(/api/run)은 못 한다** — 읽기와 실행을 분리한다.
  ④ 터널(Cloudflare/ngrok)이 만들어내는 '가짜 로컬'에 속지 않는다.
     터널은 PC 안에서 127.0.0.1 로 프록시하므로 remote_addr 이 로컬로 찍힌다.
     이걸 안 막으면 '로컬만 허용' 가드가 통째로 무력화된다.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
TAILNET = {"REMOTE_ADDR": "100.64.1.5"}        # Tailscale CGNAT 대역
PROXY_H = {"X-Forwarded-For": "203.0.113.9"}   # 터널이 붙이는 헤더
PW = "unit-test-password-9f2c"


@pytest.fixture(scope="module")
def dash():
    import integrated_dashboard_server as d
    d.app.config["TESTING"] = True
    return d


@pytest.fixture
def with_pw(dash):
    orig = dash.DASH_PASSWORD
    dash.DASH_PASSWORD = PW
    yield dash
    dash.DASH_PASSWORD = orig


@pytest.fixture
def no_pw(dash):
    orig = dash.DASH_PASSWORD
    dash.DASH_PASSWORD = ""
    yield dash
    dash.DASH_PASSWORD = orig


def test_local_access_needs_no_password(no_pw):
    """같은 PC 는 종전과 동일 — 비밀번호가 없어도 전부 열린다."""
    with no_pw.app.test_client() as c:
        assert c.get("/", environ_base=LOCAL).status_code == 200
        assert c.get("/api/all", environ_base=LOCAL).status_code == 200


def test_remote_is_blocked_when_no_password_set(no_pw):
    """비밀번호 미설정 = 원격 차단(fail-closed). 실수로 열리는 쪽이면 안 된다."""
    with no_pw.app.test_client() as c:
        assert c.get("/", environ_base=TAILNET).status_code == 403
        assert c.get("/api/all", environ_base=TAILNET).status_code == 403


def test_remote_requires_login_then_allows_read(with_pw):
    with with_pw.app.test_client() as c:
        r = c.get("/api/all", environ_base=TAILNET)
        assert r.status_code == 302, "미인증 원격이 데이터를 읽었다"

        r = c.post("/login", data={"pw": "wrong"}, environ_base=TAILNET)
        assert r.status_code == 200 and "맞지 않습니다" in r.get_data(as_text=True)
        assert c.get("/api/all", environ_base=TAILNET).status_code == 302

        r = c.post("/login", data={"pw": PW}, environ_base=TAILNET)
        assert r.status_code == 302
        assert c.get("/api/all", environ_base=TAILNET).status_code == 200


def test_remote_can_never_execute_even_when_authenticated(with_pw):
    """읽기와 실행의 분리 — 원격에서 파이프라인/스케줄러를 돌릴 수 있으면 안 된다."""
    with with_pw.app.test_client() as c:
        c.post("/login", data={"pw": PW}, environ_base=TAILNET)
        for task in ("ai_train", "scheduler_restart"):
            r = c.post(f"/api/run/{task}", environ_base=TAILNET)
            assert r.status_code == 403, f"{task} 가 원격에서 실행됐다"


def test_proxy_headers_defeat_the_fake_localhost(with_pw):
    """터널의 '가짜 로컬'에 속지 않는다 — 이 프로젝트의 조용한 실패 유형.

    remote_addr 은 127.0.0.1 이지만 프록시 헤더가 있으면 원격으로 취급한다.
    """
    with with_pw.app.test_client() as c:
        # 인증까지 통과시킨 뒤에도 실행은 거부돼야 한다
        c.post("/login", data={"pw": PW}, environ_base=LOCAL, headers=PROXY_H)
        r = c.post("/api/run/ai_train", environ_base=LOCAL, headers=PROXY_H)
        assert r.status_code == 403, "터널 경유 실행이 뚫렸다"
    for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP"):
        with with_pw.app.test_client() as c:
            r = c.get("/api/all", environ_base=LOCAL, headers={h: "203.0.113.9"})
            assert r.status_code in (302, 403), f"{h} 헤더가 무시됐다"


def test_local_execution_still_works(with_pw):
    """회귀 방지 — 진짜 로컬 실행 경로를 막아버리면 대시보드 버튼이 전부 죽는다."""
    with with_pw.app.test_client() as c:
        r = c.post("/api/run/__nonexistent_task__", environ_base=LOCAL,
                   headers={"X-Dashboard-Action": "1"})   # 페이지 JS 가 붙이는 CSRF 헤더
        assert r.status_code == 400, "로컬 실행이 가드에 걸렸다(400=작업명 오류가 정상)"


def test_password_never_lives_in_source():
    """비밀번호는 .env 에서만 온다 — 코드나 git 에 값이 남으면 안 된다."""
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"),
               encoding="utf-8").read()
    i = src.find("DASH_PASSWORD")
    assert 'DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "").strip()' in src
    assert 'DASH_PASSWORD = "' not in src, "소스에 비밀번호가 박혔다"
