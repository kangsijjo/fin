# -*- coding: utf-8 -*-
"""강도 기준 모니터 v2(§82) — 계좌 설정 방식의 점수·전략별 기준±·면제·두 가지 집계."""
import pandas as pd

import strength_cut_monitor as M


def test_load_scores_uses_v2_with_v1_fallback(tmp_path, monkeypatch):
    p = tmp_path / "log.csv"
    pd.DataFrame([
        {"signal_date": "20261023", "account": "KIS_안D", "strategy": "a", "code": "1", "name": "x",
         "score_ic": 3.0, "score_v2": 6.0},
        {"signal_date": "20261023", "account": "kiwoom_안C", "strategy": "b", "code": "2", "name": "y",
         "score_ic": 4.0, "score_v2": None},
        {"signal_date": "20261020", "account": "KIS_안D", "strategy": "a", "code": "3", "name": "z",
         "score_ic": 9.0, "score_v2": 9.0},
        {"signal_date": "20261023", "account": "virtual", "strategy": "a", "code": "4", "name": "w",
         "score_ic": 9.0, "score_v2": 9.0}]).to_csv(p, index=False)
    monkeypatch.setattr(M, "VERSION", {"kiwoom": "v2", "kis": "v2"})
    L = M.load_scores(str(p), "20261022")
    got = dict(zip(L["code"], L["sc"]))
    assert got == {"000001": 6.0, "000002": 4.0}, "v2 우선·없으면 v1, v2 기록 전·가상 계좌 제외"


def test_rule_offsets_and_exempt(monkeypatch):
    monkeypatch.setattr(M, "CUR_TH_BY", {"kis": {"a": 5.0}, "kiwoom": {}})
    monkeypatch.setattr(M, "CUR_TH", {"kis": 5.0, "kiwoom": 5.0})
    monkeypatch.setattr(M, "EXEMPT_BY", {"kis": {"ex"}, "kiwoom": set()})
    assert M._passes("kis", "a", 5.2, 0.0) and not M._passes("kis", "a", 5.2, 0.5)
    assert M._passes("kis", "a", 4.6, -0.5) and M._passes("kis", "a", 1.0, None)
    assert M._passes("kis", "ex", 0.0, 0.5), "면제는 기준과 무관"
    assert not M._passes("kis", "a", float("nan"), 0.0)
    assert list(M.RULES) == ["현행(설정)", "기준 −0.5", "기준 +0.5", "강도 필터 없음"]
