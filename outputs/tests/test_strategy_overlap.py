# -*- coding: utf-8 -*-
"""전략 겹침 경고(§72) — 표 계산(보유 겹침)·경고 규칙·대시보드 연결·주간 파이프라인."""
import os

import numpy as np

import strategy_overlap as SO

M = {"same": {"a": {"b": 100.0, "c": 60.0}, "c": {"a": 40.0}, "d": {"a": 55.0}},
     "held": {"a": {"b": 100.0}}, "generated": "x", "window": ["20230101", "20261023"]}


def test_same_strategy_in_both_accounts_is_warning():
    w = SO.warnings({"kiwoom": ["a"], "kis": ["a"]}, M)
    assert w and w[0]["level"] == "warn" and "두 계좌" in w[0]["msg"]


def test_cross_account_signal_overlap_is_warning_and_within_account_by_strength():
    w = SO.warnings({"kiwoom": ["c"], "kis": ["a"]}, M)
    assert [x["level"] for x in w] == ["warn"] and "60%" in w[0]["msg"]
    w = SO.warnings({"kis": ["a", "b"]}, M)                          # 같은 계좌 100% → 사실상 같은 종목군
    assert w[0]["level"] == "warn" and "사실상" in w[0]["msg"]
    w = SO.warnings({"kis": ["a", "c"]}, M)                          # 같은 계좌 60% → 참고
    assert w[0]["level"] == "info"
    assert SO.warnings({"kiwoom": ["a"], "kis": ["e"]}, M) == [], "겹침 표에 없으면 경고 없음"


def test_virtual_duplicates_are_info_and_grouped():
    w = SO.warnings({"kis": ["a", "d"], "ai": ["a", "d"], "strength": ["a"]}, M)
    infos = [x for x in w if x["places"] == ["ai"]]
    assert len(infos) == 1 and "2개" in infos[0]["msg"], "가상 포트폴리오별 한 줄로"
    assert all(x["level"] == "info" for x in w if set(x["places"]) & {"ai", "strength"})
    w = SO.warnings({"kis": ["b"], "strength": ["a"]}, M)              # 가상 전략이 계좌 전략과 신호 겹침
    assert any(x["level"] == "info" and "새 정보가 적음" in x["msg"] for x in w)


def test_held_pct_counts_overlapping_holdings():
    A = {"X": (np.array([20260105, 20260201]), np.array([20260110, 20260210]))}
    eb = np.array([20260108]); xb = np.array([20260120])
    B = {"X": (eb, np.maximum.accumulate(xb))}
    assert SO._held_pct(A, B) == 50.0                                 # 첫 매매만 기간이 겹침


def test_dashboard_and_pipeline_wired():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "integrated_dashboard_server.py"), encoding="utf-8").read()
    assert "/api/strategy_overlap/check" in src and 'id="lc-ov"' in src and 'id="vc-ov"' in src
    assert "겹침 경고" in src and '"overlap": ov' in src
    bat = open(os.path.join(here, "run_ai_pipeline.bat"), "rb").read().decode("ascii")
    assert "strategy_overlap.py --fresh" in bat
