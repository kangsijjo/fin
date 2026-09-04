# -*- coding: utf-8 -*-
"""
strength_logger.py — 신호 강도(IC가중 팩터점수) 백데이터 로거 (append-only)

목적
  실거래는 슬롯당 균등 분배(강도 무관)로 유지하면서, "이 강도였으면 이렇게
  매매했을 것"을 나중에 직접 데이터화/검증할 수 있도록 모든 신호의 강도
  '원천값'만 별도 파일에 누적 기록한다.

특징
  - 실거래/신호생성에 영향 0. 절대 예외를 위로 던지지 않는다(전부 내부 흡수).
  - 기존 신호 CSV(paper_signals / kis_paper_signals) 스키마는 건드리지 않음.
  - FactorScorer(IC 가중 팩터점수)를 1회만 prep 후 모든 신호를 채점.
  - 가중 사이징 방식을 '굳이지 않음' — 원천값(score_ic, score_tv, 팩터 원시값)만
    저장하므로 사용자가 나중에 임의 가중식을 적용해 재계산 가능.

출력
  db/signal_strength_log.csv  (append-only, 헤더 자동)
  컬럼:
    logged_at      기록시각
    signal_date    신호 기준일(YYYYMMDD 또는 YYYY-MM-DD, 원본 그대로)
    account        kiwoom_안C / KIS_안D
    strategy       전략명
    code, name     종목
    entry_close    신호 종가
    market_strong  시장강세 게이트
    score_ic       IC가중 통합 강도점수(0~10, 높을수록 강함). scorer 불가 시 공란
    n_factors      score_ic 계산에 쓰인 유효 팩터 수
    score_tv       당일 거래대금 전체순위(1=최대). 강도 보조지표
    slot_rank      같은 (account,strategy,date) 내 강도순위(1=가장 강함)
    factors_json   build_feature_vec 원시 팩터값 전체(JSON). 가장 raw한 백데이터

사용 (호출측 1줄)
    import strength_logger
    strength_logger.log_strength("kiwoom_안C", df, last_date, new)
"""

from __future__ import annotations

import os
from fin_paths import STOCK_DB as _FIN_STOCK_DB   # 절대경로 단일 출처(2026-09-04)
import csv
import json
import math
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_HERE, "db", "signal_strength_log.csv")

_FIELDS = [
    "logged_at", "signal_date", "account", "strategy", "code", "name",
    "entry_close", "market_strong", "score_ic", "n_factors", "score_tv",
    "ai_prob", "slot_rank", "factors_json",
]

# stock.db 후보 경로 (live_signal._open_stock_db 와 동일 정책)
_STOCK_DB_PATHS = [
    os.path.join(_HERE, "..", "Stock_AI_Project", "data", "stock.db"),
    "../Stock_AI_Project/data/stock.db",
    str(_FIN_STOCK_DB),                       # FIN_ROOT 기반(fin_paths) — 이관 시 자동 추적
]


def _open_stock_db():
    """읽기전용 stock.db 연결(가능하면). 실패 시 None."""
    try:
        import sqlite3
    except Exception:
        return None
    for p in _STOCK_DB_PATHS:
        try:
            if os.path.exists(p):
                return sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
        except Exception:
            continue
    return None


def _clean(v):
    """JSON/CSV 안전한 스칼라로 정규화. NaN/Inf → None."""
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 6)
    except (TypeError, ValueError):
        return str(v)


def _entry_close(sig):
    for k in ("entry_price_close", "entry_close", "close"):
        if k in sig and sig[k] not in (None, ""):
            return _clean(sig[k])
    return None


def log_strength(account, df, last_date, signals, *, verbose=True):
    """
    신호 리스트의 강도 원천값을 append-only CSV에 기록한다.
    어떤 이유로도 예외를 위로 전파하지 않는다(신호생성 보호).

    account  : "kiwoom_안C" | "KIS_안D" 등 식별 라벨
    df       : 전체 일봉 DataFrame(FactorScorer.prepare_price_features 입력)
    last_date: 신호 기준일(문자열)
    signals  : 신호 dict 리스트(각 dict는 code/name/strategy/entry_price_close 등)
    """
    try:
        if not signals:
            return
        rows = _build_rows(account, df, last_date, signals, verbose=verbose)
        if not rows:
            return
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        _migrate_header(_LOG_PATH, _FIELDS)   # 컬럼 추가(ai_prob 등) 시 기존파일 헤더 정합화
        new_file = not os.path.exists(_LOG_PATH) or os.path.getsize(_LOG_PATH) == 0
        # utf-8-sig: 엑셀에서 한글 안 깨지게
        with open(_LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=_FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
            for r in rows:
                w.writerow(r)
        if verbose:
            print(f"[strength_logger] {account}: {len(rows)}건 강도기록 → "
                  f"db/signal_strength_log.csv")
    except Exception as e:  # 절대 신호생성을 막지 않는다
        if verbose:
            print(f"[strength_logger][warn] 강도기록 건너뜀: {e}")


def _migrate_header(path, fields):
    """기존 CSV 헤더가 fields 와 다르면(컬럼 추가 등) 새 헤더로 재작성.
    누락 컬럼은 공란으로 채워 과거 행 보존 → DictWriter append 시 정렬붕괴 방지."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return
        with open(path, encoding="utf-8-sig") as f:
            header_line = f.readline().rstrip("\r\n")
        if header_line == ",".join(fields):
            return   # 이미 최신 헤더
        with open(path, encoding="utf-8-sig", newline="") as f:
            old_rows = list(csv.DictReader(f))
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in old_rows:
                w.writerow({k: r.get(k, "") for k in fields})
    except Exception as e:
        print(f"[strength_logger][warn] 헤더 마이그레이션 실패(무시): {e}")


def _build_rows(account, df, last_date, signals, *, verbose=True):
    """FactorScorer 로 채점해 기록용 row dict 리스트 생성."""
    logged_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str = str(last_date).replace("-", "")

    scorer = None
    price_feats = macro_feats = db_feats = None
    try:
        from factor_scorer import FactorScorer
        scorer = FactorScorer()
        price_feats = scorer.prepare_price_features(df)
        try:
            macro_feats = scorer.prepare_macro_features()
        except Exception:
            macro_feats = {}
        con = _open_stock_db()
        try:
            db_feats = scorer.prepare_db_features(con, date_str) if con else {}
        except Exception:
            db_feats = {}
        finally:
            if con:
                try:
                    con.close()
                except Exception:
                    pass
    except Exception as e:
        if verbose:
            print(f"[strength_logger][warn] FactorScorer 미가동({e}) — "
                  f"score_ic 없이 원천값만 기록")
        scorer = None

    rows = []
    for sig in signals:
        code = str(sig.get("code", "")).zfill(6)
        name = sig.get("name", "")
        strat = sig.get("strategy", "")
        score_ic = None
        n_factors = None
        score_tv = None
        ai_prob = None   # meta_model_v4 forward 예측확률(big-win)
        feats = {}
        if scorer is not None:
            try:
                # [2026-08-20 버그수정] strategy 를 넘기지 않아 build_feature_vec 이
                # 기본값(h500_40_MKT) 원-핫을 찍었다 — rsi_vol 신호인데 factors_json 에
                # strat_h500_40_MKT=1 이 들어가 model_proba(ai_prob)가 항상 '엉뚱한
                # 전략'으로 추론됐다. score_ic 는 IC_FEATURES 만 쓰므로 강도는 무관하고,
                # ai_prob(포워드 분석용 기록)만 오염돼 있었다.
                feats = scorer.build_feature_vec(
                    code, price_feats, macro_feats, db_feats,
                    strategy=strat or "h500_40_MKT") or {}
                result = scorer.score(code, name, feats)
                ic = result.get("ic", {}) or {}
                score_ic = _clean(ic.get("total"))
                n_factors = ic.get("n_available")
                score_tv = _clean(feats.get("score_tv"))
                # forward AI 누적: 신호시점 모델 확률(없으면 None) — 나중에 실현수익과 조인
                try:
                    ai_prob = scorer.model_proba(feats)
                except Exception:
                    ai_prob = None
            except Exception:
                feats = {}
        factors_clean = {k: _clean(v) for k, v in feats.items()}
        rows.append({
            "logged_at": logged_at,
            "signal_date": str(sig.get("signal_date", last_date)),
            "account": account,
            "strategy": strat,
            "code": code,
            "name": name,
            "entry_close": _entry_close(sig),
            "market_strong": sig.get("market_strong", ""),
            "score_ic": score_ic if score_ic is not None else "",
            "n_factors": n_factors if n_factors is not None else "",
            "score_tv": score_tv if score_tv is not None else "",
            "ai_prob": ai_prob if ai_prob is not None else "",
            "slot_rank": None,  # 아래서 채움
            "factors_json": json.dumps(factors_clean, ensure_ascii=False),
        })

    _assign_slot_rank(rows)
    return rows


def _assign_slot_rank(rows):
    """같은 strategy 내 강도순위(1=강함). score_ic desc, 동률은 score_tv asc."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        groups[r["strategy"]].append(r)

    def _key(r):
        sic = r["score_ic"]
        stv = r["score_tv"]
        sic_v = sic if isinstance(sic, (int, float)) else -1.0      # 강할수록 큼
        stv_v = stv if isinstance(stv, (int, float)) else 1e9       # 작을수록 강함
        return (-sic_v, stv_v)

    for _strat, grp in groups.items():
        for i, r in enumerate(sorted(grp, key=_key), start=1):
            r["slot_rank"] = i


if __name__ == "__main__":
    # 간이 스모크 테스트 (FactorScorer 없이도 동작 확인)
    import pandas as pd
    demo_df = pd.DataFrame()
    demo_sigs = [
        {"signal_date": "20260625", "code": "005930", "name": "삼성전자",
         "strategy": "high_52w_filt", "entry_price_close": 71000,
         "market_strong": True},
        {"signal_date": "20260625", "code": "000660", "name": "SK하이닉스",
         "strategy": "high_52w_filt", "entry_price_close": 150000,
         "market_strong": True},
    ]
    log_strength("TEST", demo_df, "20260625", demo_sigs)
    print("smoke test done →", _LOG_PATH)
