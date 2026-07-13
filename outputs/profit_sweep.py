"""
profit_sweep.py — 수익실현(익절) 청산룰 스윕: 라이브 6전략 × 목표수익 +10%~+50%.

stop_sweep.py 컨벤션 준수: 표만 뽑고 '최적' 자동선택 안 함(커브피팅 방지)
— full/fwd 일관성과 plateau 는 사람이 판단한다.

두 가지 체결 의미론(둘 다 계산):
  close : 그날 '종가' ≥ 목표가 → 그날 '종가' 체결.
          ⚠ [2026-07-12 정정] 현행 라이브는 EOD(18:30) 판정 → 매도는 익일 09시대다.
          따라서 '판정일 종가 체결'은 실행 불가능한 가격(낙관 편향) — 급등으로 종가가
          목표 위인 날 그 종가에 팔았다고 가정하지만 실전은 익일 시가(갭하락 잦음).
          close 행의 avg/Δavg 는 실현 가능 상한의 낙관 추정으로만 볼 것. 실제 구현
          예정 방식은 limit(지정가)이며 그 행이 라이브와 정합한다. 손절 베이스라인
          (아래 stop)도 같은 이유로 판정일 종가 체결이라 동일 편향.
  limit : 장중 '고가' ≥ 목표가 → 목표가 체결(지정가 매도 대기). 갭상승으로 시가가 이미
          목표 위면 시가 체결(더 유리). 15분 폴링/지정가 구현에 해당 — 사용자 아이디어.
          ※ 같은 날 목표(장중)와 손절(종가)이 겹치면 목표 우선(장중이 먼저라는 가정) —
            낙관 편향 소지 있으나 발생 빈도 낮음.

베이스라인 = '현행 라이브 룰':
  키움 3전략(star_high_52w_20_filt·rsi_reversal·rsi_vol) = 만기 시간청산(손절 없음)
  KIS 3전략(h52w_for3d_mkt -15% / for_high20_mkt -10% / gc_for3d -26%) = 만기 + EOD 종가 손절
오버레이 = 베이스라인 + 목표수익 청산(선착순). 즉 'KIS 는 손절 유지한 채 익절만 추가'.

사용:
  python profit_sweep.py                     # 기본 +10~+50, step 5
  python profit_sweep.py --lo 10 --hi 50 --step 10
  python profit_sweep.py --fwd-start 20260101
출력: profit_sweep_result.csv (+ 콘솔 요약표)
"""

import argparse
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import pandas as pd  # noqa: E402

from strategy_engine import ALL_STRATEGIES, DEFAULT_COSTS  # noqa: E402
from strategies.daily_loader import load_macro_daily        # noqa: E402

# 라이브 운용 6전략 + 현행 손절(EOD 종가 기준, 키움은 None=무손절)
LIVE_STOPS = {
    "star_high_52w_20_filt": None,     # 키움 high_52w_filt
    "rsi_reversal":          None,     # 키움
    "rsi_vol":               None,     # 키움
    "h52w_for3d_mkt":        -0.15,    # KIS
    "for_high20_mkt":        -0.10,    # KIS
    "gc_for3d":              -0.26,    # KIS
}

ap = argparse.ArgumentParser()
ap.add_argument("--lo", type=float, default=10.0)
ap.add_argument("--hi", type=float, default=50.0)
ap.add_argument("--step", type=float, default=5.0)
ap.add_argument("--fwd-start", default=None,
                help="forward(OOS) 구간 시작일 YYYYMMDD (기본: 전체 거래일의 70%% 지점)")
args = ap.parse_args()

TARGETS = []
t = args.lo
while t <= args.hi + 1e-9:
    TARGETS.append(round(t, 1))
    t += args.step

print("=" * 74)
print(f"  수익실현 스윕  +{args.lo:.0f}% ~ +{args.hi:.0f}% (step {args.step:.0f})"
      f"  ×  의미론 [close(종가), limit(장중 지정가)]")
print("  베이스라인 = 현행 라이브 룰(키움 무손절 / KIS EOD 손절 유지)")
print("=" * 74)

# ── 데이터/가격 인덱스 ────────────────────────────────────────────────────────
print("\n[1/3] 데이터 로딩...")
df = load_macro_daily()
df = df.reset_index(drop=True)
df["code"] = df["code"].astype(str).str.zfill(6)
all_dates = sorted(df["date"].astype(str).unique().tolist())
date_idx = {d: i for i, d in enumerate(all_dates)}
print(f"  {df['code'].nunique():,} 종목 / {len(all_dates):,} 영업일")

# 시장 게이트(일부 전략 내부 사용)
mkt = df.groupby("date")["change_pct"].mean()
mkt_ma = mkt.rolling(60, min_periods=60).mean()
mkt_d = (mkt_ma > 0).to_dict()
df["mkt_strong"] = df["date"].map(mkt_d).fillna(False)

print("[1/3] 가격 인덱스 빌드(open/high/close)...")
px = {}
for r in df[["code", "date", "open", "high", "close"]].itertuples(index=False):
    px[(r.code, str(r.date))] = (float(r.open or 0), float(r.high or 0), float(r.close or 0))

FWD_START = args.fwd_start or all_dates[int(len(all_dates) * 0.7)]
print(f"  forward(OOS) 구간: {FWD_START} ~")

# ── 오버레이 ─────────────────────────────────────────────────────────────────
def overlay(trades, target_pct, stop_pct, sem):
    """베이스 트레이드에 목표수익(+손절) 선착 청산 적용 → 행 리스트.

    target_pct: 0.10.. (None=미적용) / stop_pct: -0.15.. (None=미적용)
    sem: 'close' | 'limit'(장중 고가→목표가 체결, 갭상승은 시가)
    """
    out = []
    cost = DEFAULT_COSTS["total_pct"]
    for t in trades:
        code = str(t.code).zfill(6)
        ei = date_idx.get(str(t.entry_date))
        xi = date_idx.get(str(t.exit_date))
        ep = float(t.entry_price)
        if ei is None or xi is None or ep <= 0:
            continue
        tgt_px = ep * (1 + target_pct) if target_pct else None
        stop_px = ep * (1 + stop_pct) if stop_pct else None

        nxd, nxp, hit_tgt, hit_stp = str(t.exit_date), float(t.exit_price), False, False
        for di in range(ei, xi + 1):
            o, h, c = px.get((code, all_dates[di]), (0, 0, 0))
            if c <= 0:
                continue
            if tgt_px is not None:
                if sem == "limit":
                    if o >= tgt_px:                       # 갭상승 — 시가 체결(유리)
                        nxd, nxp, hit_tgt = all_dates[di], o, True
                        break
                    if h >= tgt_px:                       # 장중 터치 — 목표가 체결
                        nxd, nxp, hit_tgt = all_dates[di], tgt_px, True
                        break
                else:                                     # close 의미론
                    if c >= tgt_px:
                        nxd, nxp, hit_tgt = all_dates[di], c, True
                        break
            if stop_px is not None and c <= stop_px:      # 손절은 현행과 동일(EOD 종가)
                nxd, nxp, hit_stp = all_dates[di], c, True
                break
        gross = (nxp / ep - 1) * 100
        out.append({
            "entry_date": str(t.entry_date), "net": gross - cost,
            "hold": (date_idx.get(nxd, ei) - ei + 1),
            "hit_tgt": hit_tgt, "hit_stp": hit_stp,
        })
    return out


def agg(rows):
    """행 리스트 → 지표 dict (full)."""
    if not rows:
        return None
    s = pd.DataFrame(rows)
    net = s["net"]
    wins, loss = net[net > 0], net[net <= 0]
    return {
        "n": len(s),
        "win%": round(len(wins) / len(s) * 100, 1),
        "avg": round(net.mean(), 3),
        "med": round(net.median(), 2),
        "pf": round(wins.sum() / -loss.sum(), 2) if loss.sum() < 0 else 999.0,
        "hit_tgt%": round(s["hit_tgt"].mean() * 100, 1),
        "hit_stp%": round(s["hit_stp"].mean() * 100, 1),
        "hold": round(s["hold"].mean(), 1),
    }


# ── 백테스트(라이브 6전략만) + 스윕 ──────────────────────────────────────────
print("\n[2/3] 라이브 6전략 백테스트...")
live = {s.name: s for s in ALL_STRATEGIES if s.name in LIVE_STOPS}
rows_out = []
for name, strat in live.items():
    try:
        trades = strat.backtest(df.copy(), DEFAULT_COSTS)
    except Exception as e:
        print(f"  [{name}] ERROR: {e}")
        continue
    if not trades:
        print(f"  [{name}] 0 거래 — 스킵")
        continue
    stop = LIVE_STOPS[name]
    print(f"  [{name}] {len(trades):,} 거래  (현행 손절 {f'{stop*100:.0f}%' if stop else '없음'})")

    # 베이스라인 = 현행 룰(target 없음, KIS 는 손절만)
    combos = [("base", None, "-")]
    for tg in TARGETS:
        combos.append((f"+{tg:.0f}%", tg / 100.0, "close"))
        combos.append((f"+{tg:.0f}%", tg / 100.0, "limit"))

    base_avg = {}
    for label, tgt, sem in combos:
        sems = ["-"] if tgt is None else [sem]
        for sm in sems:
            rows = overlay(trades, tgt, stop, sm if tgt else "close")
            full = agg(rows)
            fwd = agg([r for r in rows if r["entry_date"] >= FWD_START])
            if full is None:
                continue
            if tgt is None:
                base_avg = {"full": full["avg"], "fwd": (fwd or {}).get("avg")}
            rec = {
                "strategy": name, "target": label, "sem": sm,
                "n": full["n"], "win%": full["win%"], "avg": full["avg"],
                "d_avg": (round(full["avg"] - base_avg.get("full", 0), 3) if tgt else 0.0),
                "med": full["med"], "pf": full["pf"],
                "hit_tgt%": full["hit_tgt%"], "hit_stp%": full["hit_stp%"], "hold": full["hold"],
                "fwd_n": (fwd or {}).get("n", 0), "fwd_avg": (fwd or {}).get("avg"),
                "fwd_d_avg": (round((fwd or {}).get("avg", 0) - (base_avg.get("fwd") or 0), 3)
                              if (tgt and fwd and base_avg.get("fwd") is not None) else 0.0),
                "fwd_win%": (fwd or {}).get("win%"), "fwd_pf": (fwd or {}).get("pf"),
            }
            rows_out.append(rec)

# ── 출력 ─────────────────────────────────────────────────────────────────────
print("\n[3/3] 결과")
res = pd.DataFrame(rows_out)
res.to_csv("profit_sweep_result.csv", index=False, encoding="utf-8-sig")
print(f"  저장: profit_sweep_result.csv ({len(res)} 행)\n")

for name in live:
    sub = res[res["strategy"] == name]
    if sub.empty:
        continue
    b = sub[sub["target"] == "base"].iloc[0]
    print(f"─── {name}  (base: avg {b['avg']:+.3f}%  win {b['win%']}%  pf {b['pf']}"
          f"  | fwd avg {b['fwd_avg']}) ───")
    print(f"  {'target':>7} {'sem':>6} {'avg':>8} {'Δavg':>8} {'win%':>6} {'pf':>6}"
          f" {'hit%':>6} {'hold':>5} | {'fwdΔ':>8} {'fwd_pf':>6}")
    for _, r in sub[sub["target"] != "base"].iterrows():
        print(f"  {r['target']:>7} {r['sem']:>6} {r['avg']:>+8.3f} {r['d_avg']:>+8.3f}"
              f" {r['win%']:>6} {r['pf']:>6} {r['hit_tgt%']:>6} {r['hold']:>5}"
              f" | {r['fwd_d_avg']:>+8.3f} {str(r['fwd_pf']):>6}")
    print()

print("※ 자동 '최적' 선택 없음 — Δavg 가 full/fwd 모두 양수 + 이웃 target 도 양수(plateau)인지 확인 후 결정.")
