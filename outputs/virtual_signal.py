# -*- coding: utf-8 -*-
"""
virtual_signal.py — AI·강도 가상매매 전용 신호. (2026-10-22, 설명서 §68)

가상매매 설정(virtual_config)에 고른 전략 중 **모의계좌(키움·KIS)가 운용하지 않는 전략**의 오늘 신호를
백테스트와 같은 엔진(engine_signals.today_signals)으로 만들어 db/virtual_signals.csv 에 쌓고,
강도·AI 확률을 signal_strength_log.csv 에 계좌 'virtual' 로 기록한다(실제 주문에는 절대 쓰지 않음).
모의계좌가 운용하는 전략은 그 계좌 신호를 그대로 쓴다(ai_paper_trader 가 세 파일을 합침).

실행: run_kis_signal.bat(평일 18:31)에서 ai_paper_trader.py 바로 앞. 수동: python virtual_signal.py
같은 (신호일·종목·전략)은 다시 쓰지 않는다(여러 번 돌려도 안전).
"""
import csv
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SIGNALS_CSV = os.path.join(HERE, "db", "virtual_signals.csv")
FIELDS = ["signal_date", "code", "name", "entry_price_close", "target_exit_date", "strategy", "holding_days",
          "lookback_high", "market_strong"]


def existing_keys(path=SIGNALS_CSV):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return set()
    with open(path, encoding="utf-8-sig") as f:
        return {(r["signal_date"], str(r["code"]).zfill(6), r["strategy"]) for r in csv.DictReader(f)}


def append(rows, path=SIGNALS_CSV):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def targets(today=None):
    """[(엔진, 라이브이름, 보유일)] — 가상 설정 전략 중 모의계좌가 운용하지 않는 것."""
    import live_config as LC
    import virtual_config as VC
    live = set()
    for a in LC.ACCOUNTS:
        live |= set(LC.account(a)["priority"])
    active, pending = VC.state(today)
    out = {}
    for e in (active, pending):
        if not e or e.get("legacy"):
            continue
        for p in VC.PORTFOLIOS:
            for s in e["portfolios"][p]["strategies"]:
                ln = LC.live_name(s["engine"])
                if ln not in live and s["engine"] not in out:
                    out[s["engine"]] = (s["engine"], ln, int(s["holding"]))
    return list(out.values())


def main():
    tg = targets()
    print(f"=== 가상매매 전용 신호 {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    if not tg:
        print("  만들 전략 없음(가상 설정이 없거나 전부 모의계좌 전략) — 종료")
        return 0
    import engine_signals as ES
    import strategy_engine as E
    from live_signal import is_excluded, _offset_date, load_name_cache
    from strategies.daily_loader import load_macro_daily
    df = load_macro_daily().reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)
    last_date = str(df["date"].max())
    all_dates = sorted(df["date"].unique())
    name_cache = load_name_cache()
    smap = {s.name: s for s in E.ALL_STRATEGIES}
    have = existing_keys()
    new, skipped = [], []
    for eng, ln, hold in tg:
        if eng not in smap:
            skipped.append((ln, f"엔진에 없는 전략({eng})"))
            continue
        try:
            rows, why = ES.today_signals(smap[eng], df, last_date, all_dates, name_cache, ln, hold,
                                         is_excluded, _offset_date)
            rows = ES._drop_ca_rows(rows, smap[eng], df, last_date, ln)
        except Exception as e:
            rows, why = [], f"신호 계산 실패: {type(e).__name__}: {str(e)[:120]}"
        if why:
            skipped.append((ln, why))
        fresh = [r for r in rows if (str(r["signal_date"]), str(r["code"]).zfill(6), r["strategy"]) not in have]
        print(f"  {ln:<24} 신호 {len(rows):>3}건 (새 {len(fresh)}건){' — ' + why if why else ''}")
        new += fresh
    if new:
        append(new)
        print(f"[saved] db/virtual_signals.csv 에 {len(new)}건")
        try:
            import strength_logger
            strength_logger.log_strength("virtual", df, last_date, new)
        except Exception as e:
            print(f"[strength_logger][warn] {e}")
    if skipped:
        try:
            import notifier
            notifier.safe_send("ℹ [가상매매 신호] 오늘 신호를 만들지 못한 전략: "
                               + " / ".join(f"{n} — {w}" for n, w in skipped))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
