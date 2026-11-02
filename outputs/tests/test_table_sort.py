# -*- coding: utf-8 -*-
"""§95 — 표 정렬을 '한 번 클릭'으로 통일(첫 클릭 오름차순 ▲, 다시 클릭 내림차순 ▼) + 정렬 없던 표 4곳 추가.

요청: "강도매매 탭 매매완료내역과 백테스트 내역에서도 오름차순 정렬이 가능하게" (사용자 결정: 완료 매매내역·기간 백테스트 결과·
전체 전략 과거 백테스트·지난 계산 기록 + 한 번 클릭으로 통일).
"""
import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)


def _html():
    src = open(os.path.join(OUTPUTS, "integrated_dashboard_server.py"), encoding="utf-8").read()
    t = ast.parse(src)
    return [ast.literal_eval(n.value) for n in t.body
            if isinstance(n, ast.Assign) and any(getattr(x, "id", None) == "HTML" for x in n.targets)][0]


def test_single_click_sort_replaces_double_click():
    h = _html()
    assert "dblclick" not in h, "더블클릭 정렬은 한 번 클릭으로 통일"
    assert "document.addEventListener('click'" in h and "function _tblClick(th)" in h
    assert "{idx:i,asc:true}" in h, "첫 클릭 = 오름차순"
    assert "onclick=\"sortSt(" not in h and "function sortSt(" not in h, \
        "강도 기록 표의 별도 정렬(첫 클릭 내림차순)을 범용 정렬로 합침"
    assert "t.closest('a,button,input,select')" in h, "머리글 안 링크·버튼은 정렬로 가로채지 않음"


def test_new_tables_are_sortable():
    h = _html()
    m = re.search(r"const SORTABLE_TBODIES = \[(.*?)\];", h, re.S)
    assert m and "'sfull-rows'" in m.group(1), "전체 전략 과거 백테스트 표"
    assert "const DYN_SORT_TBODIES = ['ap-trades-ai','ap-trades-strength','pbt-res-rows'];" in h
    assert '<tbody id="ap-trades-${k}">' in h and "_tblReapply(['ap-trades-ai','ap-trades-strength']);" in h, \
        "강도매매 완료 매매내역(AI·강도) — 5분마다 다시 그려도 정렬 유지"
    assert '<tbody id="pbt-res-rows">' in h and "_tblReapply(['pbt-res-rows']);" in h, "기간 백테스트 결과 표"
    assert "data-pin=\"1\"" in h, "시장 기준 줄은 맨 위 고정"
    assert "function pbtHistOrder()" in h and "_pbtHist.slice().reverse()" in h, "지난 계산 기록 최신순 ↔ 오래된순"


def test_sort_values_and_markers():
    h = _html()
    assert "td.dataset.v" in h, "칸의 data-v(정렬값)를 우선 — '120 (미완료 3)' 같은 칸도 숫자로"
    assert "else if(A.n!==null) return -1;" in h and "else if(B.n!==null) return 1;" in h, \
        "빈칸은 오름·내림 모두 맨 뒤(종전엔 내림차순에서 맨 위로 왔음)"
    assert "m.className='sort-mark'" in h, "▲▼ 는 span 으로 — 머리글 안 설명(마우스오버)을 지우지 않음"
    assert "delete _tblSort['pbt-res-rows']" in h, "다른 계산을 열면 정렬 초기화(열 구성이 다를 수 있음)"
    assert "data-k=\"${k}\" ${_apOpen.has(k)?'open':''}" in h, "다시 그려도 펼친 상태 유지"
