"""
live_signal.py — v4: 안C' 포트폴리오 신호 감지 (신고가 6:4, 2026-10-18)

매일 평일 장 마감 후 실행:
  python live_signal.py

전략별 슬롯 구조 — 안C' (2026-10-18 사용자 결정, 설명서 §56):
  슬롯 1-6  →  high_500d_filt  (500일 신고가 + 거래량 + 시장 강세, 20일 보유) = 백테스트 star_high_500d_20
  슬롯 7-10 →  high_52w_filt   (52주 신고가 + 거래량 + 시장 강세,  20일 보유) = 백테스트 star_high_52w_20_filt
  (2026-10-18 rsi_reversal·rsi_vol 신호 생성 중단 — 두 기간 백테스트 모두 최하위권. 감지 함수는
   intraday_preview·검증 스크립트 호환을 위해 남겨 둠)

저장:
  paper_signals.csv : 누적 신호
  컬럼: signal_date, code, name, entry_price_close, target_exit_date,
         strategy, holding_days, lookback_high, market_strong
"""

import os
from fin_paths import STOCK_DB as _FIN_STOCK_DB   # 절대경로 단일 출처(2026-09-04)
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
    str(_FIN_STOCK_DB),                       # FIN_ROOT 기반(fin_paths) — 이관 시 자동 추적
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
_HIGH_500D = dict(_HIGH_52W, lookback=500)      # HighWithFiltersStrategy(500, 20) — star_high_500d_20
# [2026-10-21] 키움 운용 전략은 live_config(대시보드에서 고름)가 정한다 — 이 모듈은 손으로 짠
# 검증된 감지기만 제공하고, 감지기가 없는 전략은 engine_signals 가 백테스트 엔진 코드로 만든다.
KNOWN_DETECTORS = ("high_500d_filt", "high_52w_filt", "rsi_reversal", "rsi_vol")
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
    # 우선주 판정: 이름 끝 '우/우B/우C' + 코드 끝자리 0 아님(보통주=0) 결합.
    # [2026-07-11] 이름만으로 판정하던 기존 로직은 '우'로 끝나는 보통주
    # (에코글로우 159910·이오플로우 294090·성우 458650)를 영구 오제외했음.
    if (name.endswith(("우", "우B", "우C")) and not str(code).endswith("0")) \
            or "우선주" in name or "스팩" in name:
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
    """신호 행을 paper_signals.csv에 append (헤더 없으면 생성).

    [2026-07-11] 0바이트 파일(직전 실행이 쓰다 죽은 잔재)은 '없음'으로 취급 — 기존엔
    존재만 보고 헤더 없이 데이터를 append 해 트레이더 todays_signals 가 KeyError 로
    죽는 손상 CSV 를 만들었음(strength_logger 는 이미 getsize 검사, 비대칭 해소)."""
    file_exists = os.path.exists(SIGNALS_CSV) and os.path.getsize(SIGNALS_CSV) > 0
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
    """52주 신고가 + 거래량 1.5x + 시장 강세 게이트 (슬롯 7-10).

    HighWithFiltersStrategy(lookback_days=252, holding_days=20,
        use_market_filter=True, use_volume_filter=True, vol_mult=1.5) 와 동일 조건.
    """
    return _detect_high_filt(df, last_date, all_dates, name_cache, mkt_dict, _HIGH_52W, "high_52w_filt")


def detect_high_500d_filt(df, last_date, all_dates, name_cache, mkt_dict):
    """500일 신고가 + 거래량 1.5x + 시장 강세 게이트 (슬롯 1-6, 2026-10-18 신설).
    HighWithFiltersStrategy(lookback_days=500, holding_days=20) = 백테스트 star_high_500d_20."""
    return _detect_high_filt(df, last_date, all_dates, name_cache, mkt_dict, _HIGH_500D, "high_500d_filt")


def _detect_high_filt(df, last_date, all_dates, name_cache, mkt_dict, p, strategy):
    """N일 신고가 + 거래량 + 시장강세 공통 감지(창 길이만 다름)."""
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
            "strategy":          strategy,
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
    import live_config as LC
    import engine_signals as ES
    acc = LC.account("kiwoom")
    print("=== Paper Trading 신호 감지 v5 (키움 — 설정: db/live_config.json) ===")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for ln in acc["priority"]:
        print(f"  {ln:<22} {acc['slots'][ln]}슬롯 · {acc['holding'][ln]}일 보유"
              f"{' · 강도 면제' if ln in acc['exempt'] else ''}"
              f"{' · 감지기' if ln in KNOWN_DETECTORS else ' · 엔진 신호'}")

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

    # 4) 전략별 신호 감지 — 설정 순서대로. 감지기가 있으면 그것, 없으면 엔진 신호(engine_signals)
    known = {"high_500d_filt": lambda d, ld, ad, nc: detect_high_500d_filt(d, ld, ad, nc, mkt_dict),
             "high_52w_filt": lambda d, ld, ad, nc: detect_high_52w_filt(d, ld, ad, nc, mkt_dict),
             "rsi_reversal": detect_rsi_reversal, "rsi_vol": detect_rsi_vol}
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)
    groups, skipped = ES.run_configured("kiwoom", df, str(last_date), [str(x) for x in all_dates], name_cache,
                                        known, is_excluded, _offset_date)
    if skipped:
        try:
            import notifier
            notifier.safe_send("⚠ [키움 신호] 오늘 신호를 만들지 못한 전략: "
                               + " / ".join(f"{n} — {w}" for n, w in skipped))
        except Exception:
            pass
    all_signals = [r for _, rows in groups for r in rows]

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
    for strat, signals in groups:
        new_cnt = sum(1 for s in new if s["strategy"] == strat)
        print(f"  {strat:<16}  총 {len(signals):>3}건  (새 신호 {new_cnt}건)")
    print(f"{'─'*55}")
    print(f"  합계              총 {len(all_signals):>3}건  (새 신호 {len(new)}건)")

    if not all_signals:
        print("\n  오늘 신호 없음.")
        return

    # 7) 전략별 콘솔 테이블
    for strat_name, strat_sigs in groups:
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

    # 8) 다인수 팩터 분석 (설정 순서 우선, 같은 종목 중복 제외)
    _seen = set()
    sig_rep = [s for s in all_signals if not (s["code"] in _seen or _seen.add(s["code"]))]
    if _SCORER_AVAILABLE and sig_rep:
        print(f"\n{'='*65}")
        print("  팩터 점수 분석 (신고가 신호 — Method B: IC 가중)")
        print(f"{'='*65}")
        try:
            scorer = FactorScorer()
            if scorer.ic_weights:
                last_date_str = str(last_date).replace("-", "")
                price_feats = scorer.prepare_price_features(df)
                macro_feats = scorer.prepare_macro_features()
                con_db = _open_stock_db()
                db_feats = scorer.prepare_db_features(con_db, last_date_str) if con_db else {}
                for s in sig_rep[:15]:
                    # strategy 전달 필수 — 빠지면 기본값(h500_40_MKT) 원-핫이 찍혀
                    # 리포트의 AI 근거가 다른 전략 기준으로 계산된다(2026-08-20).
                    feats  = scorer.build_feature_vec(
                        s["code"], price_feats, macro_feats, db_feats,
                        strategy=s.get("strategy") or "h500_40_MKT")
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
        for strat in acc["priority"]:
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
