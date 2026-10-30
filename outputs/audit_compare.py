# -*- coding: utf-8 -*-
"""
audit_compare.py — 전략 계산 점검 5건(§64) 수정 전/후 영향 비교 (일회성 연구 스크립트, 2026-10-23)

수정 전 코드(백업본)와 수정 후 코드를 같은 데이터·같은 기간으로 돌려 단계별로 CAGR 이 어떻게 바뀌는지 본다.
  A 수정 전 전부  →  B +① 슬롯 규칙  →  C +② 상폐·정지 포함  →  D +③ 분할·병합 신호 제외
  →  E +④ 상한가 시초가 제외  →  F +⑤ 비용 현실화(= 수정 후 전부)
사용: python audit_compare.py --start 20230102 --end 20261021 --names a,b --old-sb 백업_swing_base.py --old-pb 백업_period_backtest.py
"""
import argparse
import gc
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OLD_COSTS = {"fee_pct": 0.015, "tax_pct": 0.18, "slip_pct": 0.05, "total_pct": 0.245}
_FUNCS = ("_make_trades_for_signals", "_make_trades_with_stops")


def _load(name, path):
    import importlib.machinery
    spec = importlib.util.spec_from_file_location(name, path, loader=importlib.machinery.SourceFileLoader(name, path))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _patch_trade_makers(src):
    """strategies.* 모듈이 import 해 둔 매매 생성 함수를 src 모듈의 것으로 바꾼다 → 원래 함수 복구용 목록."""
    import strategies._swing_base as SB
    real = {n: getattr(SB, n) for n in _FUNCS}
    patched = []
    for mod in list(sys.modules.values()):
        nm = getattr(mod, "__name__", "") or ""
        if not nm.startswith("strategies."):
            continue
        for n in _FUNCS:
            if getattr(mod, n, None) is real[n]:
                setattr(mod, n, getattr(src, n))
                patched.append((mod, n, real[n]))
    return patched


def _unpatch(patched):
    for mod, n, f in patched:
        setattr(mod, n, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--names", required=True)
    ap.add_argument("--old-sb", required=True)
    ap.add_argument("--old-pb", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    import pandas as pd
    import period_backtest as P
    import strategy_engine as E
    import strategies.base as B
    import strategies._swing_base as SB
    from capital_simulator import build_price_lookup
    from strategies.daily_loader import load_macro_daily

    sb_old = _load("strategies._sb_old", a.old_sb)
    pb_old = _load("pb_old", a.old_pb)
    names = [x for x in a.names.split(",") if x]
    cal = P.trading_calendar()
    smap, _ = P.strategies_by_name()
    win = [d for d in cal if a.start <= d <= a.end]
    i0, i1 = cal.index(win[0]), cal.index(win[-1])
    need = max(P.warmup_days(smap[n]) for n in names)
    df = load_macro_daily()
    df["date"] = df["date"].astype(str)
    df = df[(df["date"] >= cal[max(0, i0 - need - 5)]) & (df["date"] <= cal[min(len(cal) - 1, i1 + 45)])]
    df = df.reset_index(drop=True)
    gc.collect()
    new_costs = dict(E.DEFAULT_COSTS)
    steps = [("A 수정 전", dict(sb="old", eq="old", ca=False, ul=False, cost="old")),
             ("B +①슬롯", dict(sb="old", eq="new", ca=False, ul=False, cost="old")),
             ("C +②상폐", dict(sb="new", eq="new", ca=False, ul=False, cost="old")),
             ("D +③분할·정지재개", dict(sb="new", eq="new", ca=True, ul=False, cost="old")),
             ("E +④상한가", dict(sb="new", eq="new", ca=True, ul=True, cost="old")),
             ("F +⑤비용", dict(sb="new", eq="new", ca=True, ul=True, cost="new"))]
    out = {}
    ul0 = SB.UPPER_LIMIT_SKIP_PCT
    for n in names:
        s = smap[n]
        out[n] = {}
        cache = {}
        for lab, c in steps:
            key = (c["sb"], c["ca"], c["ul"], c["cost"])
            if key not in cache:
                patched = _patch_trade_makers(sb_old) if c["sb"] == "old" else []
                B.CA_LOOKBACK_FILTER = c["ca"]
                SB.UPPER_LIMIT_SKIP_PCT = ul0 if c["ul"] else None
                try:
                    trs = s.backtest(df.copy(), OLD_COSTS if c["cost"] == "old" else new_costs) or []
                finally:
                    _unpatch(patched)
                    B.CA_LOOKBACK_FILTER = True
                    SB.UPPER_LIMIT_SKIP_PCT = ul0
                cache[key] = [t for t in trs if win[0] <= str(t.entry_date) <= win[-1]]
                gc.collect()
            tw = cache[key]
            codes = {str(t.code) for t in tw}
            pm, _ = build_price_lookup(df[df["code"].astype(str).isin(codes) & df["date"].isin(win)])
            eq, taken = (pb_old if c["eq"] == "old" else P).equity_curve(tw, pm, win)
            st = P.curve_stats(eq)
            nets = [float(t.net_pct) for t in tw]
            avg = sum(nets) / len(nets) if nets else 0.0
            savg = sum(taken) / len(taken) if taken else 0.0
            out[n][lab] = {"cagr": st["cagr"], "mdd": st["mdd"], "sharpe": st.get("sharpe"),
                           "n": len(tw), "slot_n": len(taken), "avg_net": round(avg, 3), "slot_avg": round(savg, 3)}
            print(f"{n:24s} {lab:12s} 매매 {len(tw):5d} 평균 {avg:+6.2f}% | 슬롯 {len(taken):4d} 평균 {savg:+6.2f}% "
                  f"CAGR {st['cagr']:+7.2f}% MDD {st['mdd']:+7.2f}%", flush=True)
            del pm, eq
            gc.collect()
        del cache
        gc.collect()
    if a.out:                                    # 여러 번(전략별 프로세스) 돌려도 결과가 쌓이게 병합 저장
        prev = {}
        if os.path.exists(a.out):
            try:
                with open(a.out, encoding="utf-8") as f:
                    prev = json.load(f).get("results", {})
            except Exception:
                prev = {}
        prev.update(out)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"start": a.start, "end": a.end, "results": prev}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
