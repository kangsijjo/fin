# -*- coding: utf-8 -*-
"""
fresh_fix.py — 데이터 정체 경고에 '어느 버튼을 누르면 되나'를 붙인다. (2026-11-02, 설명서 §92)

왜: 경고가 표 이름(korea_indicators)으로만 떠서 대시보드 버튼(기술지표)과 연결이 안 됐다.
쓰는 곳: 대시보드 상단 경고(get_health — 누르면 그 버튼으로 이동·강조), 텔레그램 감시(watchdog), 하루 점검(daily_audit).
버튼 = 대시보드 데이터+로그 탭 '수동 실행'의 data-fresh 버튼(그 표가 정체되면 빨갛게 깜빡임). 이름이 HTML 과 같은지는 테스트가 확인.
"""
TAB = "데이터+로그"
KEY_HOURS = "거래일 08:40~09:25·15:00~15:45 엔 막힘"     # 증권사 키를 쓰는 수집(자동매매와 키 충돌 방지)

# 표 → (한글 이름, 버튼 이름, 덧붙임)
FIX = {
    "korea_stocks":     ("국내 주가", "전체 수집", KEY_HOURS),
    "usa_stocks":       ("미국 주가", "전체 수집", KEY_HOURS),
    "supply_demand":    ("수급", "수급", KEY_HOURS),
    "macro_indicators": ("거시지표", "거시지표", ""),
    "credit_balance":   ("신용잔고", "신용/대차", KEY_HOURS),
    "program_trading":  ("프로그램매매", "프로그램매매", KEY_HOURS),
    "korea_indicators": ("기술지표", "기술지표", "45~90분"),
    "foreign_ratio":    ("외국인 지분율", "외국인 지분율", ""),
    "news":             ("뉴스/공시", "뉴스/공시", ""),
}
SIGNAL_BUTTONS = ("키움 신호", "KIS 신호")


def label(table):
    """표 → '기술지표(korea_indicators)'. 모르는 표는 이름 그대로."""
    f = FIX.get(table)
    return f"{f[0]}({table})" if f else str(table)


def fix(table):
    """대시보드용 {table, button, note} — 모르는 표는 None."""
    f = FIX.get(table)
    return {"table": table, "button": f[1], "note": f[2]} if f else None


def hint(table):
    """'데이터+로그 탭 [기술지표] 버튼(45~90분)' — 모르는 표는 ''."""
    f = FIX.get(table)
    if not f:
        return ""
    return f"{TAB} 탭 [{f[1]}] 버튼" + (f"({f[2]})" if f[2] else "")


def signal_hint():
    return f"{TAB} 탭 " + "·".join(f"[{b}]" for b in SIGNAL_BUTTONS) + " 버튼"
