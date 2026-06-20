"""
회귀 테스트 — 2026-06-20 세션에서 고친 '조용히 깨졌던' 버그들이 재발하지 않게 고정.

실행: cd C:/fin/outputs ; python -m pytest tests -q
"""
import os
import sys
import json

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
sys.path.insert(0, OUTPUTS)


def test_loader_coalesces_korean_english_columns(monkeypatch, tmp_path):
    """옛 한글(거래대금/등락률) + 최근 영문(trading_value/change_pct) 혼재 시
    최신일 값이 NaN 이 되면 안 된다 (신호 2개월 0건 버그 회귀 방지)."""
    import strategies.daily_loader as dl

    d = tmp_path / "daily"
    d.mkdir()
    pd.DataFrame({
        "code": ["000010", "000020"], "open": [100, 200], "high": [110, 210],
        "low": [90, 190], "close": [105, 205], "volume": [1000, 2000],
        "거래대금": [1e9, 2e9], "등락률": [1.0, -1.0],
        "시가총액": [1e10, 2e10],
    }).to_csv(d / "20260101.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame({
        "code": ["000010", "000020"], "open": [120, 220], "high": [130, 230],
        "low": [110, 210], "close": [125, 225], "volume": [1500, 2500],
        "trading_value": [3e9, 4e9], "change_pct": [2.0, -2.0],
        "시가총액": [1.1e10, 2.1e10],
    }).to_csv(d / "20260102.csv", index=False, encoding="utf-8-sig")

    monkeypatch.setattr(dl, "DATA_DIR", str(d))
    monkeypatch.setattr(dl, "CACHE_PATH", str(tmp_path / "_c.pkl"))
    monkeypatch.setattr(dl, "SIG_PATH", str(tmp_path / "_c.sig.json"))

    full = dl.load_macro_daily()
    last = full["date"].astype(str).max()
    t = full[full["date"].astype(str) == last]
    assert t["trading_value"].notna().all(), "trading_value NaN - coalesce 회귀!"
    assert t["change_pct"].notna().all(), "change_pct NaN - coalesce 회귀!"
    assert float(t[t["code"] == "000010"]["trading_value"].iloc[0]) == 3e9


def test_rsi_wilder_basic():
    """손실 섞인 상승 우위 series 는 RSI 높고, 하락 우위는 낮다."""
    from strategies.rsi_reversal import _rsi
    up = pd.Series([100 + sum((-1 if k % 5 == 0 else 2) for k in range(i + 1)) for i in range(60)])
    r_up = _rsi(up).iloc[-1]
    assert not np.isnan(r_up) and 0 <= r_up <= 100
    assert r_up > 70, r_up
    down = pd.Series([300 + sum((1 if k % 5 == 0 else -2) for k in range(i + 1)) for i in range(60)])
    assert _rsi(down).iloc[-1] < 30


def test_json_safe_removes_nan():
    """_json_safe: NaN/Inf -> None, 표준 JSON 직렬화 가능 (대시보드 공백 버그 회귀 방지)."""
    sys.argv = [sys.argv[0]]   # 서버 모듈이 import 시 sys.argv[1] 을 PORT 로 파싱 -> 차단
    import integrated_dashboard_server as srv
    payload = {"sc": {"rows": [{"x": float("nan"), "y": 1.5}]},
               "inf": float("inf"), "s": "ok", "n": 3}
    clean = srv._json_safe(payload)
    json.dumps(clean, allow_nan=False)
    assert clean["sc"]["rows"][0]["x"] is None
    assert clean["inf"] is None
    assert clean["s"] == "ok" and clean["n"] == 3
