# -*- coding: utf-8 -*-
"""
backfill_strength.py — 과거 신호의 강도 백데이터를 소급 생성한다.

strength_logger 는 '앞으로 생성되는' 신호만 기록한다. 이 스크립트는 이미
paper_signals.csv / kis_paper_signals.csv 에 들어 있는 과거 신호들에 대해,
해당 신호일 시점의 가격 df 슬라이스로 FactorScorer 를 돌려 강도(score_ic 등)를
db/signal_strength_log.csv 에 소급 기록한다.

- 실거래/실데이터 변경 없음. signal_strength_log.csv 에 append 만 한다.
- (account, signal_date) 단위 dedup: 이미 기록된 날짜는 통째로 건너뜀
  → 두 번 돌려도 중복 안 생기고, 한 날짜의 slot_rank 도 항상 그 날 전체 기준.
- 신호일 시점 재현: df 를 그 신호일 이하로 자르고(최근 N거래일) last_date=신호일 로 채점.

사용:
    python backfill_strength.py            # 기본: signal_date >= 20260623
    python backfill_strength.py 20260622   # 시작일 지정
"""

import os
import sys
import csv

import pandas as pd

from strategies.daily_loader import load_macro_daily
import strength_logger

PAPER_KIWOOM = "paper_signals.csv"
PAPER_KIS    = "kis_paper_signals.csv"
LOG_PATH     = strength_logger._LOG_PATH   # db/signal_strength_log.csv
HISTORY_WIN  = 150   # 신호일별로 가져올 최근 거래일 수(FactorScorer rolling 충분 + 속도)

DEFAULT_START = "20260623"   # 6/23 신호 → 6/24 매매. '어제(24일) 매매분부터'.


def _norm(d):
    return str(d).replace("-", "").strip()


def _already_logged_dates():
    """(account, signal_date) 이미 기록된 조합 집합."""
    done = set()
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 0:
        try:
            with open(LOG_PATH, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    done.add((row.get("account", ""), _norm(row.get("signal_date", ""))))
        except Exception as e:
            print(f"[warn] 기존 로그 읽기 실패(무시): {e}")
    return done


def _load_signals(csv_path, start):
    if not os.path.exists(csv_path):
        print(f"[skip] {csv_path} 없음")
        return pd.DataFrame()
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    df["signal_date"] = df["signal_date"].map(_norm)
    df = df[df["signal_date"] >= start].copy()
    return df


def backfill(account, csv_path, full_df, all_dates, start, done):
    sig = _load_signals(csv_path, start)
    if sig.empty:
        print(f"[{account}] 대상 신호 없음(>= {start})")
        return 0
    total = 0
    for sdate, grp in sig.groupby("signal_date"):
        if (account, sdate) in done:
            print(f"[{account}] {sdate} 이미 기록됨 — 건너뜀({len(grp)}건)")
            continue
        if sdate not in all_dates:
            print(f"[{account}] {sdate} 가격데이터에 없는 날짜 — 건너뜀")
            continue
        # 신호일 시점 재현: 그 날짜 이하 최근 HISTORY_WIN 거래일만
        idx = all_dates.index(sdate)
        window = set(all_dates[max(0, idx - HISTORY_WIN + 1): idx + 1])
        df_d = full_df[full_df["date"].isin(window)]
        signals = [
            {"signal_date": sdate,
             "code": r["code"], "name": r.get("name", ""),
             "strategy": r.get("strategy", ""),
             "entry_price_close": r.get("entry_price_close", ""),
             "market_strong": r.get("market_strong", "")}
            for _, r in grp.iterrows()
        ]
        print(f"[{account}] {sdate}: {len(signals)}건 채점 중...")
        strength_logger.log_strength(account, df_d, sdate, signals, verbose=False)
        total += len(signals)
    return total


def main():
    start = _norm(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_START
    print(f"=== 강도 백데이터 소급 생성 (signal_date >= {start}) ===")
    full_df = load_macro_daily().reset_index(drop=True)
    full_df["date"] = full_df["date"].map(_norm)
    all_dates = sorted(full_df["date"].unique())
    done = _already_logged_dates()

    n1 = backfill("kiwoom_안C", PAPER_KIWOOM, full_df, all_dates, start, done)
    n2 = backfill("KIS_안D",    PAPER_KIS,    full_df, all_dates, start, done)

    print(f"\n[완료] 신규 기록 {n1 + n2}건 → {LOG_PATH}")
    print("       (대시보드 강도매매 탭에서 확인)")


if __name__ == "__main__":
    main()
