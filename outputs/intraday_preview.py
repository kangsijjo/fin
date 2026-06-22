"""
intraday_preview.py — 장중 '예비 후보' 미리보기 (정보용, 2026-06-22 신설)

목적: 오늘 종가에 신호가 뜰 가능성이 있는 종목을 장중에 미리 본다.
      ※ 매매 트리거가 아니다. paper_signals.csv 에 절대 쓰지 않는다.
      ※ 잠정(provisional) — 장중 현재가 기준이라 종가에 뒤집힐 수 있다.
      정식 신호는 장 마감 후 live_signal.py(18:30, 확정 종가)가 담당.

방식: live_signal 의 detect_* 신호감지 로직을 '그대로' 재사용(전략과 100% 일관).
      오늘 바를 pykrx 장중 잠정 OHLCV 로 만들어 일봉 히스토리에 덧붙인 뒤 감지.

출력: db/kiwoom/intraday_preview.json  → 대시보드 장중시황 탭이 읽어 '잠정 후보'로 표시.
실행: python intraday_preview.py   (장중 매시간 스케줄)
"""
import os, sys, json
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import pandas as pd

from strategies.daily_loader import load_macro_daily
from live_signal import (detect_high_52w_filt, detect_rsi_reversal, detect_rsi_vol,
                         load_name_cache, _compute_mkt_strong)

OUT = "db/kiwoom/intraday_preview.json"


def _provisional_today(df):
    """pykrx 로 오늘 장중 잠정 OHLCV 를 만들어 (오늘 date, 종목별 행) DataFrame 반환.
    실패하면 None — 그 경우 미리보기는 건너뛴다(정보용이라 조용히)."""
    try:
        from pykrx import stock as pystock
    except ImportError:
        print("[preview] pykrx 미설치 → 스킵")
        return None
    today = datetime.today().strftime("%Y%m%d")
    try:
        raw = pystock.get_market_ohlcv(today, market="ALL")
    except Exception as e:
        print(f"[preview] pykrx 조회 실패 → 스킵: {e}")
        return None
    if raw is None or len(raw) < 100:
        print("[preview] 장중 데이터 없음(장외/휴장?) → 스킵")
        return None
    raw = raw.rename(columns={"시가": "open", "고가": "high", "저가": "low",
                              "종가": "close", "거래량": "volume",
                              "거래대금": "trading_value", "등락률": "change_pct"})
    raw = raw.reset_index().rename(columns={"티커": "code"})
    raw["code"] = raw["code"].astype(str).str.zfill(6)
    raw["date"] = datetime.today().strftime("%Y-%m-%d")
    # 히스토리 df 의 컬럼에 맞춰 누락분 0/NaN 채움(3개 미리보기 전략은 가격/거래량/시장게이트만 사용)
    for col in df.columns:
        if col not in raw.columns:
            raw[col] = 0
    raw = raw[[c for c in df.columns]]
    # 종가=0(거래정지 등) 제외
    return raw[raw["close"] > 0].copy()


def main():
    df = load_macro_daily()
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)

    prov = _provisional_today(df)
    result = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "provisional": True, "as_of": None, "items": []}

    if prov is None or prov.empty:
        _write(result)
        return

    today = prov["date"].iloc[0]
    # 오늘 행이 이미 있으면(스케줄러가 빈/구 데이터로 넣었을 수 있음) 제거 후 잠정행으로 교체
    df = df[df["date"] != today]
    df = pd.concat([df, prov], ignore_index=True)
    df = df.sort_values(["code", "date"]).reset_index(drop=True)

    last_date = today
    all_dates = sorted(df["date"].unique())
    name_cache = load_name_cache()
    mkt_dict = _compute_mkt_strong(df)

    sig1 = detect_high_52w_filt(df.copy(), last_date, all_dates, name_cache, mkt_dict)
    sig2 = detect_rsi_reversal(df.copy(), last_date, all_dates, name_cache)
    sig3 = detect_rsi_vol(df.copy(), last_date, all_dates, name_cache)

    items = []
    for strat, sigs in [("high_52w_filt", sig1), ("rsi_reversal", sig2), ("rsi_vol", sig3)]:
        for s in sigs:
            items.append({
                "code": str(s.get("code", "")).zfill(6),
                "name": s.get("name", ""),
                "strategy": strat,
                "price": s.get("entry_price_close", 0),  # 잠정 현재가(=장중 종가 대용)
            })
    result["as_of"] = today
    result["items"] = items
    result["counts"] = {"high_52w_filt": len(sig1), "rsi_reversal": len(sig2), "rsi_vol": len(sig3)}
    _write(result)
    print(f"[preview] 잠정 후보 {len(items)}건 "
          f"(52주 {len(sig1)} / RSI반전 {len(sig2)} / RSI+거래량 {len(sig3)}) → {OUT}")


def _write(result):
    try:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        print(f"[preview] 저장 실패: {e}")


if __name__ == "__main__":
    main()
