"""
profit_target_test.py — 전체 전략 × 청산방식 비교

청산 방식 (셋 중 가장 먼저 달성되는 조건으로 청산)
  A. 기존   : 전략별 보유기간 만료 종가 청산
  B. 수익실현: 종가 ≥ 진입가 × (1 + target_pct)  달성 즉시 청산
  C. 손절   : 종가 ≤ 진입가 × (1 + stop_pct)     달성 즉시 청산  (stop_pct < 0)
  → B/C 둘 다 설정 시: 먼저 닿는 쪽 실행. 모두 미달 시 만기 청산.

전략: strategy_engine.py 의 ALL_STRATEGIES (약 35개) 전부 사용

실행 예:
  python profit_target_test.py                           # 수익실현+5% (첫 실행: 백테스트 수행 + 캐시 저장)
  python profit_target_test.py --target 7                # 이후 실행: 캐시 로드 → 오버레이만 재계산 (빠름)
  python profit_target_test.py --stop -8.5               # 손절-8.5% 단독
  python profit_target_test.py --target 5 --stop -8.5    # 수익+5% 또는 손절-8.5% 선착
  python profit_target_test.py --target 0 --stop -8.5    # 손절만 (target=0 → 비활성)
  python profit_target_test.py --target 5 --no-cache     # 전략/기간 변경 후 강제 재실행
"""

import argparse, os, sys, pickle
from datetime import datetime
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import pandas as pd
import numpy as np

from strategy_engine      import ALL_STRATEGIES, DEFAULT_COSTS
from strategies.daily_loader import load_macro_daily

# ── CLI 인수 ──────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser()
ap.add_argument("--start",  default=None,  help="시작일 YYYYMMDD (기본: 전체)")
ap.add_argument("--end",    default=None,  help="종료일 YYYYMMDD (기본: 전체)")
ap.add_argument("--target", default=5.0,   type=float,
                help="수익실현 기준 %% (기본: 5.0 / 0이면 비활성)")
ap.add_argument("--stop",   default=0.0,   type=float,
                help="손절 기준 %% (기본: 0=비활성 / 예: -8.5)")
ap.add_argument("--out",       default=None,   help="CSV 저장 경로 (기본: results/ 자동)")
ap.add_argument("--no-cache",  action="store_true",
                help="캐시 무시하고 백테스트 재실행 (전략/기간 변경 시 사용)")
args = ap.parse_args()

START_DATE  = args.start
END_DATE    = args.end
TARGET_PCT  = args.target / 100.0 if args.target != 0 else None   # None → 비활성
STOP_PCT    = args.stop   / 100.0 if args.stop   != 0 else None   # None → 비활성
COSTS       = DEFAULT_COSTS

# 모드 레이블
_parts = []
if TARGET_PCT: _parts.append(f"+{args.target}% 수익실현")
if STOP_PCT:   _parts.append(f"{args.stop}% 손절")
MODE_LABEL = " & ".join(_parts) if _parts else "수익실현+5% (기본)"
if not _parts:  # 둘 다 0이면 기본값 적용
    TARGET_PCT = 0.05
    MODE_LABEL = "+5% 수익실현"

print("=" * 70)
print(f"  청산 조건 테스트  /  {MODE_LABEL}  — 먼저 닿는 조건으로 청산")
if START_DATE or END_DATE:
    print(f"  기간: {START_DATE or '처음'} ~ {END_DATE or '끝'}")
print("=" * 70)

# ═══════════════════════════════════════════════════════════════════════════
# 1. 데이터 로드
# ═══════════════════════════════════════════════════════════════════════════
print("\n▶ 데이터 로딩...")
df = load_macro_daily(start_date=START_DATE, end_date=END_DATE)
df = df.reset_index(drop=True)
df["code"] = df["code"].astype(str).str.zfill(6)

all_dates    = sorted(df["date"].unique().tolist())
date_idx_map = {d: i for i, d in enumerate(all_dates)}
print(f"  {df['code'].nunique():,} 종목 / {len(all_dates):,} 영업일 / {len(df):,} 행")

# 시장 게이트 공통 컬럼 추가 (일부 전략 내부에서 사용)
mkt    = df.groupby("date")["change_pct"].mean()
mkt_ma = mkt.rolling(60, min_periods=60).mean()
mkt_d  = (mkt_ma > 0).to_dict()
df["mkt_strong"] = df["date"].map(mkt_d).fillna(False)

# 종가 lookup (profit target 판단용)
print("▶ 가격 인덱스 빌드...")
close_lk = {}
for r in df[["code","date","close"]].itertuples(index=False):
    close_lk[(r.code, r.date)] = float(r.close)

# ═══════════════════════════════════════════════════════════════════════════
# 2. 청산 조건 오버레이 (수익실현 + 손절 + 만기 — 선착순)
# ═══════════════════════════════════════════════════════════════════════════
def apply_exit_rules(trades, target_pct=TARGET_PCT, stop_pct=STOP_PCT):
    """
    StrategyTrade list → 오버레이 적용 DataFrame.

    매 보유일 종가를 순서대로 확인하여 가장 먼저 달성되는 조건으로 청산:
      1. 수익실현 : 종가 ≥ entry × (1 + target_pct)   [target_pct=None 이면 비활성]
      2. 손절     : 종가 ≤ entry × (1 + stop_pct)     [stop_pct=None  이면 비활성]
      3. 만기     : 원래 exit_date 종가

    반환 컬럼:
      hit_target (bool) / hit_stop (bool) — 어느 조건으로 청산됐는지
    """
    if not trades:
        return pd.DataFrame()

    rows = []
    for t in trades:
        code = str(t.code).zfill(6)
        ed   = str(t.entry_date)
        xd   = str(t.exit_date)
        ep   = float(t.entry_price)
        if ep <= 0:
            continue
        ei = date_idx_map.get(ed)
        xi = date_idx_map.get(xd)
        if ei is None or xi is None:
            continue

        tgt_px  = ep * (1 + target_pct) if target_pct else None  # e.g. 진입가 × 1.05
        stop_px = ep * (1 + stop_pct)   if stop_pct  else None   # e.g. 진입가 × 0.915

        new_xd  = xd
        new_xp  = float(t.exit_price)
        hit_tgt = False
        hit_stp = False

        for di in range(ei, xi + 1):
            if di >= len(all_dates):
                break
            c = close_lk.get((code, all_dates[di]))
            if c is None:
                continue

            # 수익실현 먼저 체크 (같은 날 동시 달성 시 수익실현 우선)
            if tgt_px is not None and c >= tgt_px:
                new_xd, new_xp, hit_tgt = all_dates[di], c, True
                break
            if stop_px is not None and c <= stop_px:
                new_xd, new_xp, hit_stp = all_dates[di], c, True
                break

        gross = (new_xp / ep - 1) * 100
        rows.append({
            "entry_date":   ed,
            "exit_date":    new_xd,
            "entry_price":  ep,
            "exit_price":   new_xp,
            "gross_pct":    round(gross, 3),
            "net_pct":      round(gross - COSTS["total_pct"], 3),
            "holding_days": date_idx_map.get(new_xd, ei) - ei,
            "hit_target":   hit_tgt,
            "hit_stop":     hit_stp,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()

# ═══════════════════════════════════════════════════════════════════════════
# 3. 통계 함수 (기존 / PT 공통)
# ═══════════════════════════════════════════════════════════════════════════
def stat(src, is_overlay=False):
    """src: StrategyTrade list 또는 DataFrame."""
    if src is None:
        return None
    if isinstance(src, list):
        if not src:
            return None
        df_t = pd.DataFrame([t.__dict__ for t in src])
    else:
        if len(src) == 0:
            return None
        df_t = src

    n    = len(df_t)
    net  = df_t["net_pct"]
    wins = net[net > 0]
    loss = net[net <= 0]
    avg  = net.mean()
    std  = net.std()
    sr   = round(avg / std * (252 ** 0.5), 2) if std > 0 else 0.0
    pf   = round(wins.sum() / (-loss.sum()), 2) if loss.sum() < 0 else 999.0

    hold     = df_t["holding_days"].mean() if "holding_days" in df_t.columns else None
    hit_tgt  = (df_t["hit_target"].mean() * 100
                if is_overlay and "hit_target" in df_t.columns else None)
    hit_stp  = (df_t["hit_stop"].mean() * 100
                if is_overlay and "hit_stop" in df_t.columns else None)

    return {
        "n":        n,
        "win%":     round(len(wins) / n * 100, 1),
        "avg":      round(avg, 2),
        "med":      round(net.median(), 2),
        "std":      round(std, 2),
        "pf":       pf,
        "sharpe":   sr,
        "hold":     round(hold, 1) if hold is not None else "-",
        "hit_tgt%": round(hit_tgt, 1) if hit_tgt is not None else "-",
        "hit_stp%": round(hit_stp, 1) if hit_stp is not None else "-",
    }

# ═══════════════════════════════════════════════════════════════════════════
# 4. 기존 백테스트 실행 (캐시 우선)
# ═══════════════════════════════════════════════════════════════════════════
os.makedirs("results", exist_ok=True)
_range_tag = f"{START_DATE or 'all'}_{END_DATE or 'all'}"
CACHE_FILE = Path(f"results/_base_trades_cache_{_range_tag}.pkl")

if not args.no_cache and CACHE_FILE.exists():
    print(f"\n▶ 기존 백테스트 캐시 로드: {CACHE_FILE.name}")
    with open(CACHE_FILE, "rb") as f:
        base_cache = pickle.load(f)   # {name: {"trades": [...], "sb": {...}}}
    print(f"  {len(base_cache)}개 전략 캐시 완료 (--no-cache 로 재실행 가능)")
else:
    print(f"\n▶ {len(ALL_STRATEGIES)}개 전략 백테스트 실행 중...\n")
    base_cache = {}
    for strat in ALL_STRATEGIES:
        name = strat.name
        print(f"  [{name}]", end=" ", flush=True)
        try:
            trades = strat.backtest(df.copy(), COSTS)
        except Exception as e:
            print(f"ERROR: {e}")
            continue
        sb = stat(trades)
        if sb is None:
            print("0 거래")
            continue
        base_cache[name] = {"trades": trades, "sb": sb}
        print(f"{sb['n']:,} 거래  avg {sb['avg']:+.2f}%")
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(base_cache, f)
    print(f"\n  ✓ 캐시 저장: {CACHE_FILE.name}")

# ═══════════════════════════════════════════════════════════════════════════
# 5. 청산 조건 오버레이만 새로 계산
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n▶ 오버레이 적용 중 [{MODE_LABEL}]...\n")

results = []

for name, cached in base_cache.items():
    trades = cached["trades"]
    sb     = cached["sb"]

    ov_df = apply_exit_rules(trades)
    sp    = stat(ov_df, is_overlay=True)

    delta = round(sp["avg"] - sb["avg"], 2) if sp else None
    results.append({"name": name, "sb": sb, "sp": sp, "delta": delta})

    tgt_info = (f"수익달성 {sp['hit_tgt%']}%" if TARGET_PCT and sp else "")
    stp_info = (f"손절 {sp['hit_stp%']}%"     if STOP_PCT   and sp else "")
    extra    = " / ".join(filter(None, [tgt_info, stp_info]))
    if sp:
        print(f"  [{name}] {sb['n']:,} 거래  "
              f"avg {sb['avg']:+.2f}% → {sp['avg']:+.2f}% "
              f"(Δ{delta:+.2f}%)  {extra}")
    else:
        # 오버레이 0건(stat None — 캐시/데이터 불일치 가능). None 역참조 크래시 방지(2026-06-30)
        print(f"  [{name}] {sb['n']:,} 거래  (오버레이 0건 — --no-cache 권장)")

# ═══════════════════════════════════════════════════════════════════════════
# 5. 비교 테이블 출력 (Δavg 내림차순 정렬)
# ═══════════════════════════════════════════════════════════════════════════
results.sort(key=lambda x: x["delta"] if x["delta"] is not None else -999, reverse=True)

W = 120
print("\n" + "═" * W)
print(f"  전략별 청산 조건 효과 비교  [{MODE_LABEL}]  — Δavg 내림차순")
print("═" * W)

tgt_col = f"수익달성%" if TARGET_PCT else ""
stp_col = f"손절%"     if STOP_PCT   else ""
_extra_hdr = "  ".join(filter(None, [f"{tgt_col:>7}", f"{stp_col:>6}"]))
HDR = (f"  {'전략명':<22}  {'방식':<12}  {'건수':>5}  "
       f"{'승률%':>6}  {'avg%':>7}  {'med%':>7}  "
       f"{'std%':>6}  {'PF':>5}  {'Sharpe':>7}  {'avg보유':>7}  {_extra_hdr}")
print(HDR)
print("─" * W)

_ov_label = MODE_LABEL[:12]

for r in results:
    name = r["name"]
    sb, sp, delta = r["sb"], r["sp"], r["delta"]

    def _fmt(s, extra_cols):
        base = (f"  {str(s['n']):>5}  {str(s['win%']):>6}  "
                f"{s['avg']:>+7.2f}  {s['med']:>+7.2f}  "
                f"{str(s['std']):>6}  {str(s['pf']):>5}  "
                f"{s['sharpe']:>7.2f}  {str(s['hold']):>7}")
        return base + extra_cols

    d_tag = f"  Δ{delta:+.2f}%" if delta is not None else ""

    ov_extra = ""
    if TARGET_PCT:
        ov_extra += f"  {str(sp.get('hit_tgt%', '-')):>7}"
    if STOP_PCT:
        ov_extra += f"  {str(sp.get('hit_stp%', '-')):>6}"

    print(f"  {name:<22}  {'[기존]':<12}" + _fmt(sb, ""))
    print(f"  {'':22}  {f'[{_ov_label}]':<12}" + _fmt(sp, ov_extra) + d_tag)
    print("─" * W)

print("═" * W)

# ═══════════════════════════════════════════════════════════════════════════
# 6. 요약 순위
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n▶ [{MODE_LABEL}] 효과 순위  (Δavg 내림차순)")
_sum_hdr = f"  {'순위':<4}  {'전략명':<24}  {'기존avg%':>9}  {'조건avg%':>9}  {'Δavg%':>8}"
if TARGET_PCT: _sum_hdr += f"  {'수익달성%':>9}"
if STOP_PCT:   _sum_hdr += f"  {'손절발동%':>9}"
print(_sum_hdr)
print("  " + "─" * (len(_sum_hdr) - 2))
for i, r in enumerate(results, 1):
    sb, sp = r["sb"], r["sp"]
    if not sp:   # 오버레이 0건 — None 역참조(sp['avg']/delta>0) 방지(2026-06-30)
        print(f"  {i:<4}  {r['name']:<24}  {sb['avg']:>+9.2f}  {'(N/A)':>9}")
        continue
    d = r["delta"] if r["delta"] is not None else 0.0
    marker = "▲" if d > 0 else ("▼" if d < 0 else " ")
    line = (f"  {i:<4}  {r['name']:<24}  "
            f"{sb['avg']:>+9.2f}  {sp['avg']:>+9.2f}  "
            f"{marker}{d:>+7.2f}")
    if TARGET_PCT: line += f"  {str(sp.get('hit_tgt%','-')):>9}"
    if STOP_PCT:   line += f"  {str(sp.get('hit_stp%','-')):>9}"
    print(line)

# ═══════════════════════════════════════════════════════════════════════════
# 7. CSV 저장
# ═══════════════════════════════════════════════════════════════════════════
os.makedirs("results", exist_ok=True)

# 파일명: 기간 + CLI 옵션 그대로
# 예) exit_compare_20210101_20241231_target5_stop-8.5.csv
_start_tag = START_DATE or "all"
_end_tag   = END_DATE   or "all"
_opt_parts = []
if args.target != 0: _opt_parts.append(f"target{args.target:g}")
if args.stop   != 0: _opt_parts.append(f"stop{args.stop:g}")
_opt_tag  = "_".join(_opt_parts) if _opt_parts else "target5"
out_path  = args.out or f"results/exit_compare_{_start_tag}_{_end_tag}_{_opt_tag}.csv"

rows_csv = []
for r in results:
    for mode, s in [("기존", r["sb"]), (MODE_LABEL[:20], r["sp"])]:
        rows_csv.append({
            "strategy":        r["name"],
            "mode":            mode,
            "n":               s["n"],
            "win_pct":         s["win%"],
            "avg_net":         s["avg"],
            "median":          s["med"],
            "std":             s["std"],
            "profit_factor":   s["pf"],
            "sharpe":          s["sharpe"],
            "avg_hold":        s["hold"],
            "hit_target_pct":  s.get("hit_tgt%", ""),
            "hit_stop_pct":    s.get("hit_stp%", ""),
            "delta_avg":       r["delta"] if mode != "기존" else "",
        })

pd.DataFrame(rows_csv).to_csv(out_path, index=False, encoding="utf-8-sig")
print(f"\n  ✓ CSV 저장: {out_path}")
_cond = []
if TARGET_PCT: _cond.append(f"수익실현 종가 ≥ 진입가 × {1 + TARGET_PCT:.4f}")
if STOP_PCT:   _cond.append(f"손절 종가 ≤ 진입가 × {1 + STOP_PCT:.4f}")
print(f"  ※ 비용 {COSTS['total_pct']}% (왕복)  /  조건: {' | '.join(_cond)}")
