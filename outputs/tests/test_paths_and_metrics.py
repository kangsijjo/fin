# -*- coding: utf-8 -*-
"""
절대경로 단일 출처(P2) + 전략 비교표 지표 이름·재검증(P4) — 2026-09-04 외부 평가 후속

P2  'C:/fin/...' 가 12곳에 폴백 후보로 흩어져 있었다. 10/28 PC 이관에서 경로가 달라지면
    폴백이라 에러도 없이 **조용히** 깨진다. fin_paths.FIN_ROOT 하나로 모았다.
P4  sharpe / mdd_roll 은 매매 목록 기준 근사치인데 이름이 진짜 지표처럼 읽혔다.
    sharpe_proxy / mdd_proxy_10t 로 바꾸고, 상위 N 개는 --verify 가 capital_simulator
    (진짜 MDD/Sharpe) + 최근 N년 표본외로 재검증하게 했다 — 47개 중 1등 뽑기의
    다중검정 선택편향을 코드가 걸러내도록.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
ROOT = os.path.dirname(OUTPUTS)
sys.path.insert(0, OUTPUTS)

BS = chr(92)


# ─────────────────────────────────────────────────────────────────────────────
# P2 — 절대경로
# ─────────────────────────────────────────────────────────────────────────────

def _string_constants_excluding_docstrings(path):
    """모듈/클래스/함수 독스트링을 제외한 문자열 상수 목록 [(줄, 값)]."""
    import ast
    src = open(path, encoding="utf-8", errors="replace").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                v = getattr(body[0], "value", None)
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    doc_ids.add(id(v))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_ids:
            out.append((node.lineno, node.value))
    return out


def test_no_hardcoded_fin_root_in_code():
    """실행 코드의 문자열에 C:/fin 절대경로가 있으면 이관 시 조용히 깨진다."""
    import glob
    needles = ("C:/fin", "C:" + BS + "fin")
    files = glob.glob(os.path.join(OUTPUTS, "*.py"))
    files += [p for p in glob.glob(os.path.join(ROOT, "Stock_AI_Project", "**", "*.py"), recursive=True)
              if "venv" not in p.replace(ROOT, "")]
    bad = []
    for p in files:
        if os.path.basename(p) == "fin_paths.py" or (os.sep + "tests" + os.sep) in p:
            continue
        for ln, val in _string_constants_excluding_docstrings(p):
            if any(n in val for n in needles):
                bad.append(f"{os.path.relpath(p, ROOT)}:{ln}")
    assert not bad, f"절대경로가 코드에 남아 있다(fin_paths 를 쓸 것): {bad}"


def test_fin_paths_honors_env_override(monkeypatch):
    import importlib
    import fin_paths
    monkeypatch.setenv("FIN_ROOT", "D:/elsewhere/fin")
    fp = importlib.reload(fin_paths)
    assert fp.FIN_ROOT == Path("D:/elsewhere/fin")
    assert fp.STOCK_DB == Path("D:/elsewhere/fin") / "Stock_AI_Project" / "data" / "stock.db"
    monkeypatch.delenv("FIN_ROOT")
    fp = importlib.reload(fin_paths)
    assert fp.FIN_ROOT == Path(OUTPUTS).parent, "기본값은 outputs 의 부모여야 한다"


def test_fin_paths_default_points_at_real_files():
    import fin_paths as fp
    assert fp.STOCK_DB.exists(), fp.STOCK_DB
    assert fp.OUTPUTS.exists()


# ─────────────────────────────────────────────────────────────────────────────
# P4 — 지표 이름 / --verify
# ─────────────────────────────────────────────────────────────────────────────

def _fake_trades(n, start_year=2019, net=1.0):
    """6년에 걸쳐 분산된 합성 매매. 3건 중 1건은 손실."""
    out = []
    for i in range(n):
        y = start_year + (i * 6) // n
        m = (i % 12) + 1
        out.append(SimpleNamespace(
            entry_date=f"{y:04d}{m:02d}10", exit_date=f"{y:04d}{m:02d}20", code="000001",
            entry_price=1000.0, gross_pct=net + 0.3, net_pct=(-net if i % 3 == 0 else net),
            holding_days=10, score=0.0))
    return out


def test_stats_uses_proxy_names_not_real_metric_names():
    import strategy_engine as se
    st = se._stats(_fake_trades(30), "20190101", "20241231")
    assert "sharpe_proxy" in st and "mdd_proxy_10t" in st
    assert "sharpe" not in st and "mdd_roll" not in st, "근사치가 진짜 지표 이름으로 나간다"
    empty = se._stats([], None, None)
    assert set(empty) == set(st), "빈 결과와 정상 결과의 키가 다르다"


def test_verify_top_splits_in_sample_and_out_of_sample(monkeypatch):
    import strategy_engine as se
    import capital_simulator as cs
    calls = []

    def _stub(trades, **kw):
        calls.append(len(trades))
        return {"cagr_pct": 1.0, "real_mdd_pct": -5.0, "real_sharpe": 0.5}

    monkeypatch.setattr(cs, "simulate_capital", _stub)
    trades = _fake_trades(60)                              # 2019~2024 에 분산
    df_res = pd.DataFrame({"strategy": ["a", "b"], "ev_10slot": [9.0, 1.0]})
    out = se.verify_top(df_res, {"a": trades, "b": []}, top_n=2, oos_years=2.0, d_max="20241231")

    assert list(out["strategy"]) == ["a", "b"]
    row = out.iloc[0]
    expected_cutoff = (datetime(2024, 12, 31) - timedelta(days=int(2.0 * 365.25))).strftime("%Y%m%d")
    assert row["oos_cutoff"] == expected_cutoff
    assert 0 < row["oos_n"] < row["n_trades"], "표본외 분리가 안 됐다"
    assert row["real_mdd_pct"] == -5.0 and row["real_sharpe"] == 0.5
    assert {"is_avg_net", "oos_avg_net", "is_win_rate", "oos_win_rate"} <= set(out.columns)
    assert calls and calls[0] == 60, "전체 매매로 capital_simulator 를 부르지 않았다"


def test_verify_option_exists_in_cli():
    src = open(os.path.join(OUTPUTS, "strategy_engine.py"), encoding="utf-8").read()
    assert '"--verify"' in src and '"--oos-years"' in src
    assert "strategy_verify_" in src, "재검증표 저장 경로가 없다"
    assert "mdd_roll" not in src.replace("mdd_roll → mdd_proxy_10t", ""), "옛 이름이 코드에 남아 있다"
