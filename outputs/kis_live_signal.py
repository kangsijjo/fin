"""
kis_live_signal.py — KIS 모의계좌 전용 신호 감지기

매일 평일 장 마감 후 실행:
  python kis_live_signal.py

전략별 슬롯 구조 — 안D 포트폴리오 (2026-06-17):
  슬롯 1-4  →  h52w_for3d_mkt  (52주신고가 + 외국인3일순매수 + 시장강세, 20일 보유)
  슬롯 5-8  →  for_high20_mkt  (20일신고가 + 외국인3일순매수 + 시장강세,  20일 보유)
  슬롯 9-10 →  gc_for3d        (골든크로스20/60 + 외국인3일순매수,         15일 보유)

청산 오버레이 (백테스트 stop-only 최적):
  h52w_for3d_mkt : stop -15%  (delta +0.14%)
  for_high20_mkt : stop -10%  (delta +0.22%)
  gc_for3d       : stop -26%  (delta +0.07%)

저장:
  kis_paper_signals.csv : 누적 신호
  컬럼: signal_date, code, name, entry_price_close, target_exit_date,
         strategy, holding_days, lookback_high, market_strong
"""

import os
import sys
import csv
from datetime import datetime

import pandas as pd
import numpy as np

from strategies.daily_loader import load_macro_daily

try:
    from build_name_cache import load_name_cache
except ImportError:
    def load_name_cache():
        return {}

# ─── 상수 ─────────────────────────────────────────────────────────────────────
SIGNALS_CSV = "./kis_paper_signals.csv"

CSV_FIELDS = [
    "signal_date", "code", "name", "entry_price_close",
    "target_exit_date", "strategy", "holding_days",
    "lookback_high", "market_strong",
]

ETF_PREFIXES = ("KODEX", "TIGER", "KBSTAR", "KOSEF", "ARIRANG", "HANARO",
                "SOL ", "ACE ", "PLUS ", "RISE ", "1Q ", "WON ")

# 전략별 파라미터 (백테스트 최적값과 일치)
_H52W_FOR3D = dict(lookback=252, foreign_n=3, holding=20,
                   min_tv=3_000_000_000, mkt_ma=60)
_FOR_HIGH20  = dict(n_days=3, high_period=20, holding=20,
                    min_tv=3_000_000_000, mkt_ma=60)
_GC_FOR3D    = dict(fast_ma=20, slow_ma=60, foreign_n=3,
                    holding=15, min_tv=3_000_000_000)


# ─── 공통 헬퍼 ───────────────────────────────────────────────────────────────
def is_excluded(name, code):
    if not name or not code:
        return True
    if str(name).startswith(ETF_PREFIXES):
        return True
    if str(name).endswith(("우", "우B")) or "우선주" in str(name) or "스팩" in str(name):
        return True
    if len(str(code)) == 6 and str(code).startswith("5"):
        return True
    return False


def _resolve_name(r, name_cache):
    nm = str(r.get("name", "") or "").strip()
    if not nm:
        nm = name_cache.get(str(r["code"]).zfill(6), "")
    return nm


def _compute_mkt_strong(df, ma_days=60):
    """시장강세 게이트: 전체 종목 일일수익률 평균의 N일 MA > 0 → {date: bool}.

    [fix 2026-06-23] change_pct 컬럼이 최근 일부 파일에만 있어 60일 MA 가 영구 NaN →
      게이트 영구 차단(KIS 3전략 전부 0건)되던 버그. 모든 파일에 있는 close 로
      일일수익률을 직접 산출해 견고화.
    """
    d = df.sort_values(["code", "date"])
    ret = d.groupby("code")["close"].pct_change() * 100.0
    mkt = ret.groupby(d["date"]).mean()
    mkt_ma = mkt.rolling(ma_days, min_periods=ma_days).mean()
    return (mkt_ma > 0).to_dict()


def _offset_date(all_dates, last_date, n_days):
    """last_date 기준 n_days 영업일 후 날짜."""
    try:
        idx = all_dates.index(str(last_date))
        tgt = idx + n_days
        return all_dates[tgt] if tgt < len(all_dates) else ""
    except (ValueError, IndexError):
        return ""


def _consec_pos(series, n):
    """series에서 연속 n일 이상 양수인 날 True 반환."""
    return (series > 0).astype(int).rolling(n, min_periods=n).sum() >= n


# ─── CSV 관리 ─────────────────────────────────────────────────────────────────
def load_existing_signals():
    """(signal_date, code, strategy) 집합 → 멱등성 중복 방지."""
    if not os.path.exists(SIGNALS_CSV):
        return set()
    try:
        df = pd.read_csv(SIGNALS_CSV, dtype={"code": str})
        df["code"] = df["code"].astype(str).str.zfill(6)
        if "strategy" not in df.columns:
            df["strategy"] = "h52w_for3d_mkt"
        return set(zip(
            df["signal_date"].astype(str),
            df["code"].astype(str),
            df["strategy"].astype(str),
        ))
    except Exception:
        return set()


def append_signals(new_rows):
    """신호 행을 kis_paper_signals.csv에 append."""
    file_exists = os.path.exists(SIGNALS_CSV)
    # 기존 헤더 순서대로 쓴다 — CSV_FIELDS 가 나중에 재정렬돼도 컬럼이 어긋나지 않게
    # (paper_signals.csv 컬럼 어긋남 회귀와 동일 클래스 예방).
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
        writer.writerows(new_rows)


# ─── 전략 1: h52w_for3d_mkt ──────────────────────────────────────────────────
def detect_h52w_for3d_mkt(df, last_date, all_dates, name_cache):
    """52주신고가 + 외국인 3일 연속 순매수 + 시장강세 게이트.

    파라미터: lookback=252, foreign_n=3, holding=20일, min_tv=30억, mkt_ma=60
    """
    p = _H52W_FOR3D

    # 52주 신고가 (전일까지 lookback 최고가 대비 오늘 종가)
    df["prev_high"] = df.groupby("code")["high"].transform(
        lambda x: x.shift(1).rolling(p["lookback"], min_periods=p["lookback"]).max()
    )

    # 외국인 연속 순매수
    df["for_consec"] = df.groupby("code")["foreign_net"].transform(
        lambda x: _consec_pos(x, p["foreign_n"])
    )

    # 시장강세 게이트
    mkt_strong = _compute_mkt_strong(df, p["mkt_ma"])
    df["mkt_strong"] = df["date"].map(mkt_strong).fillna(False)

    today = df[df["date"] == last_date].copy()
    signaled = today[
        (today["close"] > today["prev_high"]) &
        today["prev_high"].notna() &
        today["for_consec"] &
        today["mkt_strong"] &
        (today["trading_value"] >= p["min_tv"]) &
        ~today.apply(lambda r: is_excluded(
            _resolve_name(r, name_cache), r["code"]), axis=1)
    ]

    rows = []
    for _, r in signaled.iterrows():
        nm = _resolve_name(r, name_cache)
        rows.append({
            "signal_date":      str(last_date),
            "code":             str(r["code"]).zfill(6),
            "name":             nm,
            "entry_price_close": r["close"],
            "target_exit_date": _offset_date(all_dates, last_date, p["holding"]),
            "strategy":         "h52w_for3d_mkt",
            "holding_days":     p["holding"],
            "lookback_high":    round(r["prev_high"], 2) if pd.notna(r["prev_high"]) else "",
            "market_strong":    bool(r["mkt_strong"]),
        })
    return rows


# ─── 전략 2: for_high20_mkt ──────────────────────────────────────────────────
def detect_for_high20_mkt(df, last_date, all_dates, name_cache):
    """20일 신고가 + 외국인 3일 연속 순매수 + 시장강세 게이트.

    파라미터: high_period=20, n_days=3, holding=20일, min_tv=30억, mkt_ma=60
    """
    p = _FOR_HIGH20

    # 20일 신고가 (전일까지 high_period 최고가)
    df["prev_high_20"] = df.groupby("code")["high"].transform(
        lambda x: x.shift(1).rolling(p["high_period"], min_periods=p["high_period"]).max()
    )

    # 외국인 연속 순매수
    df["for_consec"] = df.groupby("code")["foreign_net"].transform(
        lambda x: _consec_pos(x, p["n_days"])
    )

    # 시장강세 게이트
    mkt_strong = _compute_mkt_strong(df, p["mkt_ma"])
    df["mkt_strong"] = df["date"].map(mkt_strong).fillna(False)

    today = df[df["date"] == last_date].copy()
    signaled = today[
        (today["close"] >= today["prev_high_20"]) &
        today["prev_high_20"].notna() &
        today["for_consec"] &
        today["mkt_strong"] &
        (today["trading_value"] >= p["min_tv"]) &
        ~today.apply(lambda r: is_excluded(
            _resolve_name(r, name_cache), r["code"]), axis=1)
    ]

    rows = []
    for _, r in signaled.iterrows():
        nm = _resolve_name(r, name_cache)
        rows.append({
            "signal_date":      str(last_date),
            "code":             str(r["code"]).zfill(6),
            "name":             nm,
            "entry_price_close": r["close"],
            "target_exit_date": _offset_date(all_dates, last_date, p["holding"]),
            "strategy":         "for_high20_mkt",
            "holding_days":     p["holding"],
            "lookback_high":    round(r["prev_high_20"], 2) if pd.notna(r["prev_high_20"]) else "",
            "market_strong":    bool(r["mkt_strong"]),
        })
    return rows


# ─── 전략 3: gc_for3d ────────────────────────────────────────────────────────
def detect_gc_for3d(df, last_date, all_dates, name_cache):
    """골든크로스(MA20 > MA60) + 외국인 3일 연속 순매수.

    파라미터: fast_ma=20, slow_ma=60, foreign_n=3, holding=15일, min_tv=30억
    시장강세 게이트 없음 (백테스트 기준 gc_for3d, not gc_for3d_mkt).
    """
    p = _GC_FOR3D

    grp = df.groupby("code")
    df["ma_fast"] = grp["close"].transform(
        lambda x: x.rolling(p["fast_ma"], min_periods=p["fast_ma"]).mean()
    )
    df["ma_slow"] = grp["close"].transform(
        lambda x: x.rolling(p["slow_ma"], min_periods=p["slow_ma"]).mean()
    )
    df["prev_ma_fast"] = grp["ma_fast"].transform(lambda x: x.shift(1))
    df["prev_ma_slow"] = grp["ma_slow"].transform(lambda x: x.shift(1))

    # 골든크로스: 오늘 fast > slow, 어제 fast <= slow
    gc = (
        (df["ma_fast"] > df["ma_slow"]) &
        (df["prev_ma_fast"] <= df["prev_ma_slow"]) &
        df["ma_fast"].notna() & df["ma_slow"].notna() &
        df["prev_ma_fast"].notna() & df["prev_ma_slow"].notna()
    )

    # 외국인 연속 순매수
    df["for_consec"] = grp["foreign_net"].transform(
        lambda x: _consec_pos(x, p["foreign_n"])
    )

    today = df[df["date"] == last_date].copy()
    signaled = today[
        gc.reindex(today.index, fill_value=False) &
        today["for_consec"] &
        (today["trading_value"] >= p["min_tv"]) &
        ~today.apply(lambda r: is_excluded(
            _resolve_name(r, name_cache), r["code"]), axis=1)
    ]

    rows = []
    for _, r in signaled.iterrows():
        nm = _resolve_name(r, name_cache)
        rows.append({
            "signal_date":      str(last_date),
            "code":             str(r["code"]).zfill(6),
            "name":             nm,
            "entry_price_close": r["close"],
            "target_exit_date": _offset_date(all_dates, last_date, p["holding"]),
            "strategy":         "gc_for3d",
            "holding_days":     p["holding"],
            "lookback_high":    "",
            "market_strong":    "",
        })
    return rows


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    print(f"[kis_signal] 실행: {datetime.now():%Y-%m-%d %H:%M}")

    print("[kis_signal] macro_daily 로드 중...")
    df = load_macro_daily()
    if df.empty:
        print("[kis_signal][ERROR] macro_daily 비어있음 — 종료")
        sys.exit(1)

    # foreign_net 컬럼 확인
    if "foreign_net" not in df.columns or df["foreign_net"].abs().sum() == 0:
        print("[kis_signal][WARN] foreign_net 컬럼 없거나 전부 0 → h52w_for3d_mkt / for_high20_mkt / gc_for3d 신호 없음")
        print("              → update_macro_daily.py 재실행 또는 수급 데이터 수집 확인 필요")

    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)
    # foreign_net 없으면 0 채움
    if "foreign_net" not in df.columns:
        df["foreign_net"] = 0.0

    all_dates = sorted(df["date"].unique().tolist())
    last_date = all_dates[-1]
    print(f"[kis_signal] 기준일: {last_date}  종목수: {df['code'].nunique()}")

    name_cache = load_name_cache()
    existing = load_existing_signals()

    # 전략별 신호 수집
    all_new = []
    for detect_fn, label in [
        (detect_h52w_for3d_mkt, "h52w_for3d_mkt"),
        (detect_for_high20_mkt, "for_high20_mkt"),
        (detect_gc_for3d,       "gc_for3d"),
    ]:
        try:
            rows = detect_fn(df.copy(), last_date, all_dates, name_cache)
        except Exception as e:
            print(f"[kis_signal][ERROR] {label} 감지 실패: {e}")
            rows = []

        # 중복 제거
        new = [r for r in rows
               if (r["signal_date"], r["code"], r["strategy"]) not in existing]
        print(f"  {label}: 신호 {len(rows)}건  신규 {len(new)}건")
        all_new.extend(new)
        existing.update((r["signal_date"], r["code"], r["strategy"]) for r in new)

    if all_new:
        append_signals(all_new)
        print(f"[kis_signal] 총 {len(all_new)}건 저장 → {SIGNALS_CSV}")
    else:
        print("[kis_signal] 신규 신호 없음")

    # 요약 출력
    print("\n[kis_signal] 오늘 감지된 신호:")
    for r in all_new:
        print(f"  {r['strategy']:<18} {r['code']} {r['name']:<12}"
              f"  종가 {r['entry_price_close']:>8,.0f}  보유 {r['holding_days']}일"
              f"  만기 {r['target_exit_date']}")


if __name__ == "__main__":
    main()
