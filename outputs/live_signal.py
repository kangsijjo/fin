"""
live_signal.py — v3: 안C 포트폴리오 신호 감지 (4:4:2)

매일 평일 장 마감 후 실행:
  python live_signal.py

전략별 슬롯 구조 — 안C (2026-06-17, 백테스트 CAGR +11.6%):
  슬롯 1-4  →  high_52w_filt   (52주 신고가 + 거래량 + 시장 강세, 20일 보유)
  슬롯 5-8  →  rsi_reversal    (RSI(14) < 30 과매도 반전,          5일 보유)
  슬롯 9-10 →  rsi_vol         (RSI(14) < 30 + 거래량 2배 급증,    7일 보유)

저장:
  paper_signals.csv : 누적 신호
  컬럼: signal_date, code, name, entry_price_close, target_exit_date,
         strategy, holding_days, lookback_high, market_strong
"""

import os
import sys
import csv
from datetime import datetime

import pandas as pd
import numpy as np

import config  # .env

from strategies.daily_loader import load_macro_daily

try:
    from build_name_cache import load_name_cache
except ImportError:
    def load_name_cache():
        return {}

try:
    from factor_scorer import FactorScorer
    _SCORER_AVAILABLE = True
except ImportError:
    _SCORER_AVAILABLE = False

# ─── stock.db 연결 ────────────────────────────────────────────────────────────
_STOCK_DB_PATHS = [
    os.getenv("STOCK_DB", ""),
    "../Stock_AI_Project/data/stock.db",
    "C:/fin/Stock_AI_Project/data/stock.db",
]

def _open_stock_db():
    import sqlite3
    for p in _STOCK_DB_PATHS:
        if p and os.path.exists(p):
            try:
                con = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
                con.execute("SELECT 1")
                return con
            except Exception:
                pass
    return None


# ─── 상수 ─────────────────────────────────────────────────────────────────────
SIGNALS_CSV = "./paper_signals.csv"

# ※ 순서는 기존 paper_signals.csv 헤더와 반드시 일치해야 한다(append 시 컬럼 어긋남 방지).
CSV_FIELDS = [
    "signal_date", "code", "name", "entry_price_close",
    "target_exit_date", "lookback_high", "market_strong",
    "strategy", "holding_days",
]

# ETF/우선주/스팩 제외
ETF_PREFIXES = ("KODEX", "TIGER", "KBSTAR", "KOSEF", "ARIRANG", "HANARO",
                "SOL ", "ACE ", "PLUS ", "RISE ", "1Q ", "WON ")

# 전략별 파라미터 (백테스트 최적값과 일치)
_HIGH_52W = dict(lookback=252, holding=20, min_tv=3_000_000_000,
                 vol_mult=1.5, mkt_ma=60)
_RSI_REV   = dict(rsi_period=14, rsi_th=30, holding=5,
                  min_tv=1_000_000_000)
_RSI_VOL   = dict(rsi_period=14, rsi_th=30, vol_ma_days=20, vol_mult=2.0,
                  holding=7, min_tv=1_000_000_000)


# ─── 공통 헬퍼 ───────────────────────────────────────────────────────────────
def is_excluded(name, code):
    if not name or not code:
        return True
    if name.startswith(ETF_PREFIXES):
        return True
    if name.endswith(("우", "우B")) or "우선주" in name or "스팩" in name:
        return True
    if len(code) == 6 and code.startswith("5"):
        return True
    return False


def _resolve_name(r, name_cache):
    nm = str(r.get("name", "") or "").strip()
    if not nm:
        nm = name_cache.get(str(r["code"]).zfill(6), "")
    return nm


def _compute_mkt_strong(df, ma_days=60):
    """시장강세 게이트: 전체 종목 일일수익률 평균의 N일 MA > 0 → {date: bool}.

    [fix 2026-06-23] 기존엔 change_pct 컬럼 평균을 썼는데, change_pct 가 최근 일부
      macro_daily 파일에만 있어(과거 파일엔 컬럼 자체가 없음) 60일 MA 가 영구 NaN →
      mkt_strong 이 항상 False → 시장게이트 쓰는 모든 전략이 영구 0건이던 버그.
      모든 파일에 존재하는 close 로 일일수익률을 직접 산출해 견고화.
    """
    d = df.sort_values(["code", "date"])
    ret = d.groupby("code")["close"].pct_change() * 100.0
    mkt = ret.groupby(d["date"]).mean()
    mkt_ma = mkt.rolling(ma_days, min_periods=ma_days).mean()
    return (mkt_ma > 0).to_dict()


def _offset_date(all_dates, last_date, n_days):
    """last_date 기준 n_days 영업일 후 날짜.

    [2026-07-11] 라이브에선 last_date=달력 마지막이라 항상 ''(target_exit_date 영구
    공란) → 대시보드 슬롯현황 0 고정 / 장중모니터가 'nan' 문자열비교로 전 이력을
    활성 오판하던 원인. 달력 밖이면 주말만 건너뛴 근사일 반환(공휴일 미반영 —
    표시·근사용. 실제 만기판정은 트레이더가 원장+실제 달력 인덱스로 계산)."""
    try:
        idx = all_dates.index(str(last_date))
        tgt = idx + n_days
        if tgt < len(all_dates):
            return all_dates[tgt]
        remain = tgt - (len(all_dates) - 1)
        dr = pd.bdate_range(str(all_dates[-1]), periods=remain + 1)
        return dr[-1].strftime("%Y%m%d")
    except (ValueError, IndexError):
        return ""


def _rsi(series, period=14):
    """Welles Wilder RSI (HTS/MTS 정통 방식)."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ─── CSV 관리 ─────────────────────────────────────────────────────────────────
def _migrate_signals_csv():
    """구버전 CSV(strategy/holding_days 컬럼 없음) → 신버전으로 마이그레이션."""
    if not os.path.exists(SIGNALS_CSV):
        return
    try:
        df = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
    except Exception:
        return
    changed = False
    if "strategy" not in df.columns:
        df["strategy"] = "high_52w_filt"
        changed = True
    if "holding_days" not in df.columns:
        df["holding_days"] = 40
        changed = True
    if changed:
        df.to_csv(SIGNALS_CSV, index=False, encoding="utf-8-sig")
        print(f"[migrate] paper_signals.csv — strategy/holding_days 컬럼 추가 완료")


def load_existing_signals():
    """(signal_date, code, strategy) 집합 → 멱등성 중복 방지."""
    if not os.path.exists(SIGNALS_CSV):
        return set()
    try:
        df = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
        df["code"] = df["code"].astype(str).str.zfill(6)
        if "strategy" not in df.columns:
            df["strategy"] = "high_52w_filt"
        return set(zip(
            df["signal_date"].astype(str),
            df["code"].astype(str),
            df["strategy"].astype(str),
        ))
    except Exception:
        return set()


def append_signals(new_rows):
    """신호 행을 paper_signals.csv에 append (헤더 없으면 생성)."""
    file_exists = os.path.exists(SIGNALS_CSV)
    if file_exists:
        _migrate_signals_csv()
    # 기존 파일이 있으면 그 헤더 순서대로 쓴다 — CSV_FIELDS 와 헤더 순서가 달라도
    # 행 컬럼이 어긋나지 않게(과거 회귀 재발 방지). 컬럼 집합이 같을 때만 헤더 순서 채택.
    fields = CSV_FIELDS
    if file_exists:
        try:
            with open(SIGNALS_CSV, encoding="utf-8-sig") as hf:
                cols = [c.strip() for c in hf.readline().lstrip("﻿").strip().split(",") if c.strip()]
            if set(cols) == set(CSV_FIELDS):
                fields = cols
        except Exception:
            pass
    with open(SIGNALS_CSV, "a" if file_exists else "w",
              newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for r in new_rows:
            writer.writerow(r)


# ─── 전략별 신호 감지 ──────────────────────────────────────────────────────────
def detect_high_52w_filt(df, last_date, all_dates, name_cache, mkt_dict):
    """Strategy 1 (슬롯 1-6): 52주 신고가 + 거래량 1.5x + 시장 강세 게이트.

    HighWithFiltersStrategy(lookback_days=252, holding_days=20,
        use_market_filter=True, use_volume_filter=True, vol_mult=1.5) 와 동일 조건.
    """
    p = _HIGH_52W
    df = df.sort_values(["code", "date"]).copy()

    df["prev_high"] = df.groupby("code")["high"].transform(
        lambda x: x.shift(1).rolling(p["lookback"], min_periods=p["lookback"]).max()
    )
    df["tv_mean_30"] = df.groupby("code")["trading_value"].transform(
        lambda x: x.rolling(30, min_periods=30).mean()
    )
    df["mkt_strong"] = df["date"].map(mkt_dict).fillna(False)

    today = df[df["date"] == last_date].copy()
    today["_name"] = today.apply(lambda r: _resolve_name(r, name_cache), axis=1)

    signaled = today[
        (today["close"] > today["prev_high"]) &
        today["mkt_strong"] &
        (today["trading_value"] >= today["tv_mean_30"] * p["vol_mult"]) &
        (today["trading_value"] >= p["min_tv"]) &
        today["prev_high"].notna() &
        today["tv_mean_30"].notna() &
        ~today.apply(lambda r: is_excluded(r["_name"], r["code"]), axis=1)
    ]

    tgt = _offset_date(all_dates, last_date, p["holding"])
    rows = []
    for _, r in signaled.iterrows():
        rows.append({
            "signal_date":       last_date,
            "code":              str(r["code"]).zfill(6),
            "name":              r["_name"],
            "entry_price_close": float(r["close"]),
            "target_exit_date":  tgt,
            "strategy":          "high_52w_filt",
            "holding_days":      p["holding"],
            "lookback_high":     float(r.get("prev_high", 0) or 0),
            "market_strong":     True,
        })
    return rows


def detect_rsi_vol(df, last_date, all_dates, name_cache):
    """Strategy 3 (슬롯 9-10): RSI(14) < 30 과매도 + 거래량 2배 급증.

    RsiVolumeStrategy(rsi_period=14, rsi_threshold=30,
        vol_ma_days=20, vol_mult=2.0, holding_days=7) 와 동일 조건.
    조건:
      1. RSI(14) < 30
      2. 당일 거래대금 > 20일 평균 거래대금 × 2.0 배
      3. 거래대금 ≥ 10억 (절대 유동성)
    """
    p = _RSI_VOL
    df = df.sort_values(["code", "date"]).copy()

    df["rsi"] = df.groupby("code")["close"].transform(
        lambda s: _rsi(s, p["rsi_period"])
    )
    df["tv_ma"] = df.groupby("code")["trading_value"].transform(
        lambda x: x.rolling(p["vol_ma_days"], min_periods=p["vol_ma_days"]).mean()
    )

    today = df[df["date"] == last_date].copy()
    today["_name"] = today.apply(lambda r: _resolve_name(r, name_cache), axis=1)

    signaled = today[
        (today["rsi"] < p["rsi_th"]) &
        today["tv_ma"].notna() &
        (today["trading_value"] > today["tv_ma"] * p["vol_mult"]) &
        (today["trading_value"] >= p["min_tv"]) &
        ~today.apply(lambda r: is_excluded(r["_name"], r["code"]), axis=1)
    ]

    tgt = _offset_date(all_dates, last_date, p["holding"])
    rows = []
    for _, r in signaled.iterrows():
        rows.append({
            "signal_date":       last_date,
            "code":              str(r["code"]).zfill(6),
            "name":              r["_name"],
            "entry_price_close": float(r["close"]),
            "target_exit_date":  tgt,
            "strategy":          "rsi_vol",
            "holding_days":      p["holding"],
            "lookback_high":     0.0,
            "market_strong":     False,
        })
    return rows


def detect_rsi_reversal(df, last_date, all_dates, name_cache):
    """Strategy 2 (슬롯 5-8): RSI(14) < 30 과매도 반전.

    RsiReversalStrategy(rsi_period=14, rsi_threshold=30, holding_days=5) 와 동일 조건.
    """
    p = _RSI_REV
    df = df.sort_values(["code", "date"]).copy()

    df["rsi"] = df.groupby("code")["close"].transform(
        lambda s: _rsi(s, p["rsi_period"])
    )

    today = df[df["date"] == last_date].copy()
    today["_name"] = today.apply(lambda r: _resolve_name(r, name_cache), axis=1)

    signaled = today[
        (today["rsi"] < p["rsi_th"]) &
        (today["trading_value"] >= p["min_tv"]) &
        ~today.apply(lambda r: is_excluded(r["_name"], r["code"]), axis=1)
    ]

    tgt = _offset_date(all_dates, last_date, p["holding"])
    rows = []
    for _, r in signaled.iterrows():
        rows.append({
            "signal_date":       last_date,
            "code":              str(r["code"]).zfill(6),
            "name":              r["_name"],
            "entry_price_close": float(r["close"]),
            "target_exit_date":  tgt,
            "strategy":          "rsi_reversal",
            "holding_days":      p["holding"],
            "lookback_high":     0.0,
            "market_strong":     False,
        })
    return rows


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    print("=== Paper Trading 신호 감지 v3 (안C: 4:4:2) ===")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("  슬롯 1-4 : high_52w_filt  (52주 신고가, 20일 보유)")
    print("  슬롯 5-8 : rsi_reversal   (RSI<30 과매도, 5일 보유)")
    print("  슬롯 9-10: rsi_vol        (RSI<30 + 거래량 2배, 7일 보유)")

    # 1) 데이터 로드
    df = load_macro_daily()
    df = df.reset_index(drop=True)
    print(f"\n[data] {df['code'].nunique()} 종목, "
          f"{df['date'].nunique()} 영업일, 최신 {df['date'].max()}")

    last_date = df["date"].max()
    all_dates = sorted(df["date"].unique())

    # 2) CSV 마이그레이션 (구버전 → 신버전)
    _migrate_signals_csv()

    # 3) 공통 사전 계산
    name_cache = load_name_cache()
    mkt_dict   = _compute_mkt_strong(df)

    # 4) 전략별 신호 감지
    sig1 = detect_high_52w_filt(df.copy(), last_date, all_dates, name_cache, mkt_dict)
    sig2 = detect_rsi_reversal(df.copy(), last_date, all_dates, name_cache)
    sig3 = detect_rsi_vol(df.copy(), last_date, all_dates, name_cache)

    all_signals = sig1 + sig2 + sig3

    # 5) 기존 신호와 대조 — (date, code, strategy) 기준 중복 제거
    existing = load_existing_signals()
    new = [
        s for s in all_signals
        if (str(s["signal_date"]), str(s["code"]).zfill(6), s["strategy"])
           not in existing
    ]

    # 6) 전략별 요약 출력
    print(f"\n[{last_date}] 신호 감지 결과")
    print(f"{'─'*55}")
    for strat, signals in [("high_52w_filt", sig1),
                            ("rsi_reversal",  sig2),
                            ("rsi_vol",       sig3)]:
        new_cnt = sum(1 for s in new if s["strategy"] == strat)
        print(f"  {strat:<16}  총 {len(signals):>3}건  (새 신호 {new_cnt}건)")
    print(f"{'─'*55}")
    print(f"  합계              총 {len(all_signals):>3}건  (새 신호 {len(new)}건)")

    if not all_signals:
        print("\n  오늘 신호 없음.")
        return

    # 7) 전략별 콘솔 테이블
    for strat_name, strat_sigs in [("high_52w_filt", sig1),
                                    ("rsi_reversal",  sig2),
                                    ("rsi_vol",       sig3)]:
        if not strat_sigs:
            continue
        print(f"\n[{strat_name}] {len(strat_sigs)}건")
        print(f"  {'Code':>6}  {'Name':<25}  {'Close':>9}")
        print(f"  {'─'*48}")
        for s in strat_sigs[:20]:
            print(f"  {s['code']}  {s['name'][:24]:<25}  "
                  f"{s['entry_price_close']:>9,.0f}")
        if len(strat_sigs) > 20:
            print(f"  ... and {len(strat_sigs)-20} more")

    # 8) 다인수 팩터 분석 (high_52w_filt 신호 — 주력 전략)
    if _SCORER_AVAILABLE and sig1:
        print(f"\n{'='*65}")
        print("  팩터 점수 분석 (high_52w_filt — Method B: IC 가중)")
        print(f"{'='*65}")
        try:
            scorer = FactorScorer()
            if scorer.ic_weights:
                last_date_str = str(last_date).replace("-", "")
                price_feats = scorer.prepare_price_features(df)
                macro_feats = scorer.prepare_macro_features()
                con_db = _open_stock_db()
                db_feats = scorer.prepare_db_features(con_db, last_date_str) if con_db else {}
                for s in sig1[:15]:
                    feats  = scorer.build_feature_vec(
                        s["code"], price_feats, macro_feats, db_feats)
                    result = scorer.score(s["code"], s["name"], feats)
                    print(scorer.format_report(result))
                if con_db:
                    con_db.close()
            else:
                print("  [팩터 점수] trades_history_v3.csv 없음 — 점수 계산 불가")
        except Exception as e:
            print(f"  [팩터 점수] 계산 오류: {e}")

    # 9) CSV 저장
    if new:
        append_signals(new)
        print(f"\n[saved] paper_signals.csv 에 {len(new)} 건 추가")
        for strat in ["high_52w_filt", "rsi_reversal", "rsi_vol"]:
            cnt = sum(1 for s in new if s["strategy"] == strat)
            if cnt:
                print(f"  {strat}: {cnt}건")
        # 강도 백데이터 기록(실거래 영향 없음, 예외 자체흡수)
        try:
            import strength_logger
            strength_logger.log_strength("kiwoom_안C", df, last_date, new)
        except Exception as _e:
            print(f"[strength_logger][warn] {_e}")
    else:
        print("\n[skip] 모두 중복 신호 — CSV 저장 없음")


if __name__ == "__main__":
    main()
