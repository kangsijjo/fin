# -*- coding: utf-8 -*-
"""
프로그램매매 피처(prm_net_5d_ratio) 복구 회귀 — 2026-09-08

배경: factor_scorer 의 prm_net_5d_ratio(IC +0.141, OOS +0.111)는 program_trading 테이블 부재로
라이브 89% 결측이었다. 키움 ka90013 수집기(kiwoom_program.py)와 factor_scorer 배선으로 복구.
학습된 정의는 kiwoom_hist_features.csv 역산으로 복원(079960/20260129 → 978 정확 재현):
  raw   = 신호일 포함 5거래일 프로그램 순매수 금액(백만원) 합, **음수 날은 0 절단**
  ratio = raw / 당일 거래대금(원)

검증하는 것
  ① _kw_num: 키움 '--153'(음수) → -153 / '+267000' → 267000 (공용 _fnum 은 '--' 를 0 으로 만듦)
  ② prepare_db_features: 절단합(raw)·부호합(signed) 정의, 5거래일 미만이면 결측 유지, 신호일 이후 행 제외
  ③ build_feature_vec: ratio = raw / trading_value, 거래대금 없거나 0 이면 ratio 미생성
"""
import os
import sys
import sqlite3

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.dirname(HERE)
FIN = os.path.dirname(OUTPUTS)
sys.path.insert(0, OUTPUTS)
sys.path.insert(0, os.path.join(FIN, "Stock_AI_Project"))


def test_kw_num_parses_kiwoom_double_minus_and_sign_prefix():
    from src.collector.kiwoom_program import _kw_num
    assert _kw_num("--153") == -153.0, "키움 음수 표기 '--' 는 음수여야 한다(공용 _fnum 은 0 으로 만듦)"
    assert _kw_num("+267000") == 267000.0
    assert _kw_num("-26650") == 26650.0, "단일 '-' 는 등락 표시(가격) — 크기만 취한다"
    assert _kw_num("978") == 978.0
    assert _kw_num("") == 0.0 and _kw_num("--") == 0.0 and _kw_num(None) == 0.0
    assert _kw_num("abc") == 0.0


def _scorer():
    from factor_scorer import FactorScorer
    return FactorScorer(trades_csv="__no_such_csv__")   # IC 로드 생략(경량)


def _db_with_program(rows):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE program_trading (date TEXT, ticker TEXT, prm_net_amt REAL, UNIQUE(date,ticker))")
    con.executemany("INSERT INTO program_trading VALUES (?,?,?)", rows)
    # prepare_db_features 가 다른 테이블도 읽으므로 빈 테이블로 존재시켜 예외 경로 대신 정상 경로를 탄다
    con.execute("CREATE TABLE supply_demand (date TEXT, ticker TEXT, foreign_net_value REAL, institution_net_value REAL)")
    con.execute("CREATE TABLE foreign_ratio (date TEXT, ticker TEXT, holding_ratio REAL)")
    con.execute("CREATE TABLE korea_indicators (date TEXT, ticker TEXT, rsi REAL, macd_hist REAL, bb_upper REAL, bb_lower REAL, Close REAL)")
    con.execute("CREATE TABLE credit_balance (date TEXT, ticker TEXT, credit_remain_rt REAL)")
    con.commit()
    return con


# 079960 실측(2026-01): 부호 보존 저장값. 절단합(23~29)=978, 부호합=458 이 학습 CSV 와 일치했던 사례.
_ROWS_079960 = [
    ("2026-01-22", "079960", -102.0),
    ("2026-01-23", "079960", 241.0),
    ("2026-01-26", "079960", 310.0),
    ("2026-01-27", "079960", 223.0),
    ("2026-01-28", "079960", -520.0),
    ("2026-01-29", "079960", 204.0),
    ("2026-01-30", "079960", 999.0),     # 신호일 이후 — 제외돼야 함
]


def test_prepare_db_features_reproduces_trained_clipped_definition():
    con = _db_with_program(_ROWS_079960)
    db = _scorer().prepare_db_features(con, "20260129")
    f = db["079960"]
    assert f["prm_net_5d_raw"] == pytest.approx(978.0), "음수 날 0 절단 5일합 = 학습 정의(978)"
    assert f["prm_net_5d_signed"] == pytest.approx(458.0), "부호 보존 합(후보 피처)"


def test_prepare_db_features_requires_five_trading_days():
    con = _db_with_program(_ROWS_079960[1:4])           # 3거래일만
    db = _scorer().prepare_db_features(con, "20260129")
    assert "prm_net_5d_raw" not in db.get("079960", {}), "5거래일 미만이면 결측 유지"


def test_build_feature_vec_ratio_uses_same_day_trading_value():
    fs = _scorer()
    db = {"079960": {"prm_net_5d_raw": 978.0}}
    fv = fs.build_feature_vec("079960", {"079960": {"trading_value": 3_487_616_350.0}}, {}, db)
    assert fv["prm_net_5d_ratio"] == pytest.approx(978.0 / 3_487_616_350.0)
    # 거래대금 없음 / 0 → ratio 미생성 (0 나눗셈·가짜 값 방지)
    assert "prm_net_5d_ratio" not in fs.build_feature_vec("079960", {"079960": {}}, {}, db)
    assert "prm_net_5d_ratio" not in fs.build_feature_vec("079960", {"079960": {"trading_value": 0.0}}, {}, db)
    assert "prm_net_5d_ratio" not in fs.build_feature_vec("079960", {"079960": {"trading_value": np.nan}}, {}, db)
