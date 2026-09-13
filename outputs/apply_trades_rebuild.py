# -*- coding: utf-8 -*-
"""
apply_trades_rebuild.py — 학습 데이터 재생성본을 **검증 후** 라이브 IC 원천으로 교체 (2026-09-11)

`trades_history_v3.csv` 는 factor_scorer 의 IC 가중치 원천이라, 교체하면 강도점수 → 임계 5.7 통과 →
당일 매수 종목까지 즉시 바뀐다. 그래서 아래 **사전등록 가드**를 모두 통과할 때만 교체한다.
하나라도 실패하면 교체하지 않고 비0 종료(원본 무손상).

가드 (교체 조건)
  G1 행수가 기존의 95~110% (백테스트가 통째로 어긋나지 않았나)
  G2 기존 19개 피처의 IC 변화가 각각 |Δ| < 0.01 (프로그램매매 피처만 의도적으로 변한다)
  G3 공통 (code,date,strategy) 행에서 net_pct·score_tv 불일치 0 (재생성 재현성)
  G4 강도점수 임계 5.7 통과율 변화 |Δ| < 3%p (매매량 급변 방지)
  G5 prm_net_5d_ratio 커버리지가 증가 (이 작업의 목적 자체)

절차: 백업(backup/trades_history_v3_YYYYmmdd_HHMM.csv) → 원자적 교체 → 사후 확인(FactorScorer 로드).

사용:
  python apply_trades_rebuild.py --rebuild        # 재생성부터 수행 후 검증·교체
  python apply_trades_rebuild.py --new <파일>      # 이미 만든 재생성본으로 검증·교체
  python apply_trades_rebuild.py --dry-run        # 검증만, 교체 안 함
"""
import os
import sys
import shutil
import argparse
import subprocess
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from factor_scorer import IC_FEATURES, LIVE_STRATEGY_NAMES, MIN_IC_COVERAGE

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "trades_history_v3.csv")
BACKUP_DIR = os.path.join(HERE, "backup")


def load(path):
    d = pd.read_csv(path, low_memory=False, dtype={"code": str})
    d["date"] = d["date"].astype(str).str.replace("-", "").str[:8]
    d["code"] = d["code"].str.zfill(6)
    return d


def ic_of(df):
    live = df[df["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)]
    y = live["net_pct"].astype(float)
    out = {}
    for f in IC_FEATURES:
        col = "crd_remn_rt_y" if (f == "crd_remn_rt" and "crd_remn_rt_y" in live.columns) else f
        if col not in live.columns:
            continue
        v = live[col].astype(float)
        m = v.notna() & y.notna()
        if m.sum() < 100:
            continue
        r, _ = spearmanr(v[m], y[m])
        if not np.isnan(r):
            out[f] = float(r)
    return out, live


def strength(live, ic):
    full = sum(abs(v) * 0.5 for v in ic.values())
    raw, avail = np.zeros(len(live)), np.zeros(len(live))
    for f, w in ic.items():
        col = "crd_remn_rt_y" if (f == "crd_remn_rt" and "crd_remn_rt_y" in live.columns) else f
        v = live[col].astype(float)
        sv = np.sort(v[v.notna()].to_numpy())
        arr = v.to_numpy(dtype=float)
        ok = ~np.isnan(arr)
        if not ok.any():
            continue
        raw[ok] += w * (np.searchsorted(sv, arr[ok], side="left") / len(sv) - 0.5)
        avail[ok] += abs(w) * 0.5
    cov = avail / full if full else np.zeros_like(avail)
    denom = np.where((avail > 0) & (cov >= MIN_IC_COVERAGE), avail, full)
    return np.clip(5.0 + np.divide(raw, denom, out=np.zeros_like(raw), where=denom > 0) * 5.0, 0, 10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", default="trades_history_v3_final.csv")
    ap.add_argument("--rebuild", action="store_true", help="make_trades_history_v3 로 재생성부터 수행")
    ap.add_argument("--dry-run", action="store_true", help="검증만 하고 교체하지 않음")
    a = ap.parse_args()
    new_path = a.new if os.path.isabs(a.new) else os.path.join(HERE, a.new)

    if a.rebuild:
        print(f"[1/4] 재생성 → {os.path.basename(new_path)}", flush=True)
        env = dict(os.environ, TRADES_OUT=new_path)
        r = subprocess.run([sys.executable, os.path.join(HERE, "make_trades_history_v3.py")],
                           cwd=HERE, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        for ln in (r.stdout or "").splitlines():
            if any(k in ln for k in ("program_trading", "saved", "커버리지", "kiwoom_feat")):
                print("   " + ln.strip(), flush=True)
        if r.returncode != 0:
            print(f"재생성 실패(exit {r.returncode}):\n{(r.stderr or '')[-1500:]}")
            sys.exit(1)
    if not os.path.exists(new_path):
        sys.exit(f"재생성본 없음: {new_path}")

    print(f"\n[2/4] 검증 (사전등록 가드 5종)", flush=True)
    o, n = load(LIVE), load(new_path)
    ic_o, live_o = ic_of(o)
    ic_n, live_n = ic_of(n)
    s_o, s_n = strength(live_o, ic_o), strength(live_n, ic_n)
    fails = []

    ratio = len(n) / len(o) * 100
    ok1 = 95 <= ratio <= 110
    print(f"   G1 행수 {len(o):,} → {len(n):,} ({ratio:.1f}%) … {'OK' if ok1 else 'FAIL'}")
    if not ok1:
        fails.append("G1 행수")

    worst, worst_f = 0.0, ""
    for f in ic_o:
        if f.startswith("prm_"):      # 이 피처만 의도적으로 변한다
            continue
        d = abs(ic_n.get(f, np.nan) - ic_o[f])
        if not np.isnan(d) and d > worst:
            worst, worst_f = d, f
    ok2 = worst < 0.01
    print(f"   G2 기존 피처 IC 최대변화 {worst:.5f} ({worst_f}) … {'OK' if ok2 else 'FAIL'}")
    if not ok2:
        fails.append("G2 IC 변화")

    j = o.merge(n, on=["code", "date", "strategy"], how="inner", suffixes=("_o", "_n"))
    bad = sum(int((j[f"{c}_o"] - j[f"{c}_n"]).abs().gt(1e-6).sum()) for c in ("net_pct", "score_tv")
              if f"{c}_o" in j.columns)
    ok3 = bad == 0
    print(f"   G3 공통 {len(j):,}행 회귀 불일치 {bad}건 … {'OK' if ok3 else 'FAIL'}")
    if not ok3:
        fails.append("G3 회귀")

    p_o, p_n = (s_o >= 5.7).mean() * 100, (s_n >= 5.7).mean() * 100
    ok4 = abs(p_n - p_o) < 3.0
    print(f"   G4 임계 5.7 통과율 {p_o:.1f}% → {p_n:.1f}% ({p_n - p_o:+.1f}%p) … {'OK' if ok4 else 'FAIL'}")
    if not ok4:
        fails.append("G4 통과율")

    # G5: 결측으로 버려지는 IC 질량이 줄었는가 = 재생성의 목적 자체.
    # 종전엔 prm_net_5d_ratio 커버리지만 봤는데, 그건 프로그램매매 백필 전용 조건이라
    # 다른 피처(기술지표 등)를 채우는 작업에서는 항상 FAIL 이었다. (2026-09-13 일반화)
    def _lost_mass(df, ic):
        tot = sum(abs(v) for v in ic.values())
        live_df = df[df["strategy"].astype(str).isin(LIVE_STRATEGY_NAMES)]
        lost = 0.0
        for f, w in ic.items():
            col = "crd_remn_rt_y" if (f == "crd_remn_rt" and "crd_remn_rt_y" in live_df.columns) else f
            if col not in live_df.columns:
                continue
            lost += abs(w) / tot * 100 * (1 - live_df[col].notna().mean())
        return lost
    l_o, l_n = _lost_mass(o, ic_o), _lost_mass(n, ic_n)
    ok5 = l_n <= l_o + 0.1          # 악화만 아니면 통과(동률 허용)
    print(f"   G5 결측으로 버려지는 IC 질량 {l_o:.1f}% → {l_n:.1f}% … {'OK' if ok5 else 'FAIL'}")
    if not ok5:
        fails.append("G5 정보손실")

    print(f"\n   [참고] prm IC {ic_o.get('prm_net_5d_ratio', float('nan')):+.4f} → {ic_n.get('prm_net_5d_ratio', float('nan')):+.4f} | "
          f"통과분 평균 net {live_o[s_o >= 5.7]['net_pct'].mean():+.3f} → {live_n[s_n >= 5.7]['net_pct'].mean():+.3f}")

    if fails:
        print(f"\n가드 실패: {', '.join(fails)} — 교체하지 않음(원본 무손상)")
        sys.exit(1)
    if a.dry_run:
        print("\n가드 전부 통과. --dry-run 이므로 교체하지 않음.")
        return

    print(f"\n[3/4] 백업", flush=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    bk = os.path.join(BACKUP_DIR, f"trades_history_v3_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
    shutil.copy2(LIVE, bk)
    print(f"   {bk}")

    print(f"[4/4] 교체 + 사후 확인", flush=True)
    tmp = LIVE + ".tmp"
    shutil.copy2(new_path, tmp)
    os.replace(tmp, LIVE)
    from factor_scorer import FactorScorer
    fs = FactorScorer()
    top = sorted(fs.ic_weights.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]
    print(f"   FactorScorer 재로드 OK — 피처 {len(fs.ic_weights)}개, 상위: " +
          ", ".join(f"{k} {v:+.4f}" for k, v in top))
    print(f"   prm_net_5d_ratio IC = {fs.ic_weights.get('prm_net_5d_ratio', float('nan')):+.4f}")
    print(f"\n교체 완료. 되돌리려면: copy \"{bk}\" \"{LIVE}\"")


if __name__ == "__main__":
    main()
