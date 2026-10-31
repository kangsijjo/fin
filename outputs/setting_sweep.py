# -*- coding: utf-8 -*-
"""
setting_sweep.py — 전략마다 강도 기준 × 손절 × 익절 조합을 탐색해 '더 나은 설정'을 찾는다. (2026-10-30, 설명서 §87)

사용자 결정: 주간 자동 탐색 + 해마다 검증 / 고른 전략 + 추천 전략 / 손절 + 익절.
방법
  ① 대상 = 모의계좌·가상매매에 고른 전략 + 백테스트 종합 추천 상위(strategy_reco).
  ② 전략마다 엔진 백테스트 매매(2021~) + 강도 점수(워크포워드 — 그 해 이전 매매로만 학습):
     v2(전략별 선형) · AI(개선 AI). 손절·익절 재청산은 기간 백테스트와 같은 규칙(종가 판정 → 다음날 시가, 만기는 상한).
  ③ 조합 = 강도[기준 없음(강도 순서로만)·4.0·4.5·5.0·5.5·6.0] × 손절[없음·−10·−15·−20·−25·−30] × 익절[없음·+20·+30·+50]
     × [§91] 상한가 마감 신호 제외[끔·켬] (+ 지금 설정값), 해마다 따로 10칸 단독 자본 시뮬 → 해별 수익·MDD.
  ④ 점수 = 해 평균 수익 + 0.3 × 해 평균 MDD(음수) — 균형. 해 평균 매매 10건 미만인 조합은 제외(기준이 너무 빡빡).
  ⑤ 과적합 점검(해마다 검증): 해 Y 에 대해 'Y 이전 해들만 보고 고른 조합'이 Y 에도 '기준 없음·손절/익절 없음'보다 나았나.
     권장 = 전체 기간 최고 조합이 기본보다 낫고 · 해마다 검증 60% 이상 이김 · 이웃 값(강도 ±0.5, 손절 ±5%p)도 기본보다 나음.
     아니면 '지금(기본) 유지' — 바꿔도 나아진다는 근거가 약하다는 뜻.
주의: 과거 기준이다. 손절·익절은 종가 판정 → 다음날 시가 체결(키움 실제: 손절 15:21 판정·마감 매도, 익절 장중 지정가 — 조금 다름).
저장: db/setting_sweep.json. 실행: python setting_sweep.py [--names a,b] (주간 AI 파이프라인 [10])
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "db", "setting_sweep.json")
FROM_YEAR = 2021
TH = [4.0, 4.5, 5.0, 5.5, 6.0]
STOPS = [None, -10.0, -15.0, -20.0, -25.0, -30.0]
TAKES = [None, 20.0, 30.0, 50.0]
# [§88] 주말 촘촘 탐색(--fine): 거친 격자에서 점수 좋은 구간 3곳 → 그 둘레를 강도 0.1·손절 1%p·익절 2%p 로
FINE_SEEDS = 3
FINE_TH = (0.5, 0.1)          # ±0.5 를 0.1 씩
FINE_ST = (5.0, 1.0)          # ±5%p 를 1%p 씩
FINE_TK = (10.0, 2.0)         # ±10%p 를 2%p 씩
# 고를 때는 한 점이 아니라 '주변 평균'(강도 ±0.2 · 손절 ±2%p · 익절 ±4%p) — 튀는 한 점(운) 대신 넓게 좋은 구간
SMOOTH = (0.2, 2.0, 4.0)
MDD_W = 0.3
MIN_TRADES_YEAR = 10
WIN_SHARE = 0.6
FAMILIES = ("v2", "ai")
RECO_TOP = 8


def out_path():
    return os.getenv("SETTING_SWEEP_PATH") or OUT


# ── 시뮬 ─────────────────────────────────────────────────────────────────────

def year_sim(R, slots=10, capital=1.0):
    """한 해 진입분(R: entry_date·exit_date·code·net·score) → (수익%, MDD%, 매수 건수). 10칸·남은 현금/남은 칸·보유 종목 재매수 금지.
    하루 후보는 점수 상위 2×칸 수까지만 본다(그날 살 수 있는 건 최대 10개, 보유 중이라 건너뛰는 것도 최대 10개 — 결과 동일, 속도용)."""
    if R is None or len(R) == 0:
        return 0.0, 0.0, 0
    R = R.sort_values(["entry_date", "score"], ascending=[True, False], kind="stable")
    R = R[R.groupby("entry_date", sort=False).cumcount() < 2 * slots]
    ed = R["entry_date"].to_numpy()
    xd = R["exit_date"].to_numpy()
    cd = R["code"].to_numpy()
    nt = R["net"].to_numpy(float)
    days, first = np.unique(ed, return_index=True)
    bounds = list(first) + [len(ed)]
    start = {d: (bounds[k], bounds[k + 1]) for k, d in enumerate(days)}
    dates = sorted(set(days.tolist()) | set(xd.tolist()))
    cash, pos, peak, mdd, n = float(capital), [], float(capital), 0.0, 0
    for d in dates:
        if pos:
            keep = []
            for p in pos:
                if p[1] < d:
                    cash += p[2] * (1 + p[3] / 100)
                else:
                    keep.append(p)
            pos = keep
        se = start.get(d)
        if se is not None:
            held = {p[0] for p in pos}
            for k in range(se[0], se[1]):
                if cd[k] in held:
                    continue
                left = slots - len(pos)
                if left <= 0:
                    break
                inv = cash / left
                pos.append((cd[k], xd[k], inv, nt[k]))
                held.add(cd[k])
                cash -= inv
                n += 1
        if pos:
            keep = []
            for p in pos:
                if p[1] <= d:
                    cash += p[2] * (1 + p[3] / 100)
                else:
                    keep.append(p)
            pos = keep
        eq = cash + sum(p[2] for p in pos)
        if eq > peak:
            peak = eq
        if eq / peak - 1 < mdd:
            mdd = eq / peak - 1
    return (cash / capital - 1) * 100, mdd * 100, n


def objective(stats, years):
    """해별 결과 {Y: (ret, mdd, n)} 중 years 만 → 점수(없거나 매매 적으면 None)."""
    v = [stats[y] for y in years if y in stats]
    if not v:
        return None
    if np.mean([x[2] for x in v]) < MIN_TRADES_YEAR:
        return None
    return float(np.mean([x[0] for x in v]) + MDD_W * np.mean([x[1] for x in v]))


def _key(th, stop, take, lu=False):
    """조합 열쇠 (강도, 손절, 익절, 상한가 마감 신호 제외[§91])."""
    return (None if th is None else float(th), None if stop is None else float(stop), None if take is None else float(take),
            bool(lu))


KEYS = ("th", "stop", "take", "lu")


def summarize(stats):
    ys = sorted(stats)
    rets = [stats[y][0] for y in ys]
    comp = float(np.prod([1 + r / 100 for r in rets]))
    yrs = max(len(ys), 1)
    return {"cagr": round((comp ** (1 / yrs) - 1) * 100, 1), "mdd": round(float(np.mean([stats[y][1] for y in ys])), 1),
            "n_year": round(float(np.mean([stats[y][2] for y in ys])), 1),
            "yearly": {y: round(stats[y][0], 1) for y in ys}}


def _arr(keys):
    k = np.array([[np.nan if v is None else float(v) for v in key[:3]] for key in keys], float).reshape(-1, 3)
    lu = np.array([bool(key[3]) if len(key) > 3 else False for key in keys], bool)
    return k[:, 0], k[:, 1], k[:, 2], lu


def _near(keys, rad, chunk=400):
    """keys 사이 '가까움' 행렬(희소) — 같은 칸이 비었는지(None)가 같고 차이가 rad 이내. 자기 자신 포함.
    촘촘 탐색은 조합이 수천 개라 n×n 을 한 번에 만들지 않고 줄 묶음으로 나눠 만든다(메모리 작게)."""
    from scipy.sparse import csr_matrix
    th, st, tk, lu = _arr(keys)
    n = len(keys)
    rows, cols = [], []
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        m = lu[a:b, None] == lu[None, :]          # [§91] 상한가 제외 켬/끔은 서로 이웃이 아님
        for v, r in ((th, rad[0]), (st, rad[1]), (tk, rad[2])):
            nan = np.isnan(v)
            vv = np.nan_to_num(v).astype(np.float32)
            same_nan = nan[a:b, None] == nan[None, :]
            close = np.abs(vv[a:b, None] - vv[None, :]) <= r + 1e-6
            m &= same_nan & (close | nan[a:b, None])
        rr, cc = np.nonzero(m)
        rows.append(rr + a)
        cols.append(cc)
    rr, cc = np.concatenate(rows), np.concatenate(cols)
    return csr_matrix((np.ones(len(rr)), (rr, cc)), shape=(n, n))


def _smoothed(M, o):
    ok = ~np.isnan(o)
    num = M @ np.where(ok, o, 0.0)
    den = M @ ok.astype(float)
    sm = np.divide(num, den, out=np.full(len(o), np.nan), where=den > 0)
    sm[~ok] = np.nan                      # 그 점 자체가 무효(매매 부족)면 고르지 않음
    return sm


def choose(table, years, smooth=SMOOTH):
    """table: {key: {Y: (ret, mdd, n)}} → 권장 판정. 고르기 = 주변 평균 점수 최고(촘촘 격자일수록 의미 — 거친 격자에선 이웃이 없어 그 점 점수).
    해마다 검증: Y 이전 해만 보고 고른 조합이 Y 에서 기본보다 나았나. 넓은 이웃(강도 ±0.5·손절 ±5·익절 ±10) 평균도 기본보다 나아야 권장."""
    base_k = _key(None, None, None, False)
    if base_k not in table:
        return None
    keys = list(table)
    bi = keys.index(base_k)
    M = _near(keys, smooth)
    W = _near(keys, (0.5, 5.0, 10.0))

    def objs(ys):
        return np.array([np.nan if (o := objective(table[k], ys)) is None else o for k in keys], float)
    o_all = objs(years)
    sm = _smoothed(M, o_all)
    if np.isnan(sm).all():
        return None
    b = int(np.nanargmax(sm))
    best_k = keys[b]
    base_o = None if np.isnan(o_all[bi]) else float(o_all[bi])
    wins, tests, pick_comp, base_comp, oos = 0, 0, 1.0, 1.0, {}
    for i, Y in enumerate(years):
        prev = years[:i]
        if not prev or Y not in table[base_k]:
            continue
        sp = _smoothed(M, objs(prev))
        if np.isnan(sp).all():
            continue
        pk = keys[int(np.nanargmax(sp))]
        if Y not in table[pk]:
            continue
        r_pick, r_base = table[pk][Y][0], table[base_k][Y][0]
        tests += 1
        wins += int(r_pick > r_base)
        pick_comp *= 1 + r_pick / 100
        base_comp *= 1 + r_base / 100
        oos[Y] = [round(r_pick, 2), round(table[pk][Y][1], 2), round(r_base, 2)]
    row = W.getrow(b).toarray().ravel() > 0
    row[b] = False
    neigh = o_all[row]
    neigh = neigh[~np.isnan(neigh)]
    plateau = base_o is None or len(neigh) == 0 or float(neigh.mean()) > base_o
    better = base_o is None or sm[b] > base_o + 1e-9
    win_ok = tests > 0 and wins / tests >= WIN_SHARE
    status = "권장" if (best_k != base_k and better and win_ok and plateau) else "유지"
    return {"status": status, "best": dict(zip(KEYS, best_k)), "best_stats": summarize(table[best_k]),
            "base_stats": summarize(table[base_k]), "obj_best": round(float(o_all[b]), 2), "obj_best_smooth": round(float(sm[b]), 2),
            "obj_base": None if base_o is None else round(base_o, 2),
            "wins": f"{wins}/{tests}", "oos_pick": round((pick_comp - 1) * 100, 1), "oos_base": round((base_comp - 1) * 100, 1),
            "oos_years": oos, "plateau": bool(plateau), "n_combos": int((~np.isnan(o_all)).sum())}


def fine_combos(table, years, seeds=FINE_SEEDS):
    """거친 결과에서 점수 좋은 구간 seeds 곳(서로 다른 조합) → 그 둘레 촘촘 조합 목록(이미 계산한 것 제외)."""
    keys = [k for k in table if objective(table[k], years) is not None]
    keys.sort(key=lambda k: -objective(table[k], years))
    out, picked = set(), []
    for k in keys:
        if len(picked) >= seeds:
            break
        if any(abs((k[0] or 0) - (p[0] or 0)) <= 0.5 and abs((k[1] or 0) - (p[1] or 0)) <= 5 and (k[2] == p[2])
               and k[3:] == p[3:] for p in picked):
            continue                                  # 이미 고른 구간과 겹치면 다음 후보
        picked.append(k)
    for pk in picked:
        th, st, tk = pk[:3]
        lu = pk[3] if len(pk) > 3 else False
        ths = [None] if th is None else [round(th + d, 1) for d in np.arange(-FINE_TH[0], FINE_TH[0] + 1e-9, FINE_TH[1])]
        sts = [None] if st is None else [round(st + d, 1) for d in np.arange(-FINE_ST[0], FINE_ST[0] + 1e-9, FINE_ST[1])]
        tks = [None] if tk is None else [round(tk + d, 1) for d in np.arange(-FINE_TK[0], FINE_TK[0] + 1e-9, FINE_TK[1])]
        for a in ths:
            if a is not None and not 0 <= a <= 10:
                continue
            for b in sts:
                if b is not None and not -50 <= b <= -1:
                    continue
                for c in tks:
                    if c is not None and not 1 <= c <= 300:
                        continue
                    k = _key(a, b, c, lu)
                    if k not in table:
                        out.add(k)
    return sorted(out, key=lambda k: tuple(-1e9 if v is None else float(v) for v in k)), picked


# ── 대상·현재 설정 ──────────────────────────────────────────────────────────

def current_settings():
    """{엔진: [{place, family, th, stop, take, lu}]} — 지금 모의계좌·가상매매 설정(가상은 예약분 우선). lu = 상한가 마감 신호 제외."""
    import live_config as LC
    import virtual_config as VC
    out = {}
    cfg = LC.load()
    for a in LC.ACCOUNTS:
        A = cfg["accounts"][a]
        fam = A.get("strength_version") or "v2"
        for s in A["strategies"]:
            th = None if s.get("exempt") else (s.get("min_strength") if s.get("min_strength") is not None else A.get("min_strength"))
            out.setdefault(s["engine"], []).append({"place": a, "family": fam, "th": th, "stop": s.get("stop"), "take": s.get("take"),
                                                    "lu": bool(s.get("skip_limitup"))})
    act, pend = VC.state()
    e = pend or act
    if e and not e.get("legacy"):
        for p in VC.PORTFOLIOS:
            P = e["portfolios"][p]
            fam = "ai" if p == "ai" else (P.get("strength_version") or "v2")
            for s in P["strategies"]:
                th = None if s.get("exempt") else (s.get("min_score") if s.get("min_score") is not None else P.get("min_score"))
                out.setdefault(s["engine"], []).append({"place": p, "family": fam, "th": th, "stop": s.get("stop"), "take": s.get("take"),
                                                        "lu": bool(s.get("skip_limitup"))})
    return out


def targets():
    cur = current_settings()
    names = set(cur)
    try:
        import strategy_reco as SR
        ev, _ = SR.evaluate()
        good = sorted((e for e, a in ev.items() if a["score"] >= SR.MIN_SCORE and (a["cagr"] or 0) > 0),
                      key=lambda e: -ev[e]["score"])[:RECO_TOP]
        names |= set(good)
    except Exception:
        pass
    return sorted(names), cur


# ── 전략 1개 ─────────────────────────────────────────────────────────────────

def sweep_engine(engine, df, smap, T_all, cur_rows, log=print, fine=False, lu_keys=None):
    import strategy_engine as E
    import strength_backtest as SB
    import strength_model as SM
    import ai_model as AIM
    from period_backtest import apply_exits, build_panel
    t0 = time.time()
    trs = [t for t in (smap[engine].backtest(df.copy(), E.DEFAULT_COSTS) or []) if str(t.entry_date) >= f"{FROM_YEAR}0101"]
    if len(trs) < 30:
        return {"error": f"{FROM_YEAR}년 이후 매매 {len(trs)}건 — 표본 부족"}
    years = sorted({str(t.entry_date)[:4] for t in trs})
    X = SB.trade_features(df, trs, log=lambda *a: None)
    yrs = [str(t.entry_date)[:4] for t in trs]
    sc = {}
    try:                     # 학습표가 없으면 v2 는 건너뜀(학습표는 ai_model --learn·강도 v2 주간 학습이 만든다 — 여기 df 는 잘려 있음)
        p = SM.train_path(engine)
        if os.path.exists(p):
            sc["v2"], _ = SM.walk_forward(pd.read_pickle(p), X.reset_index(drop=True), yrs)
    except Exception as e:
        log(f"  [{engine}] v2 점수 실패: {e}")
    try:
        sc["ai"], _ = AIM.walk_forward(T_all, X.reset_index(drop=True), yrs, engine)
    except Exception as e:
        log(f"  [{engine}] AI 점수 실패: {e}")
    panel = build_panel(df, {str(t.code) for t in trs})
    ex = {}
    import signal_filters as SFL
    B = pd.DataFrame({"entry_date": np.array([int(t.entry_date) for t in trs], np.int32),
                      "code": [str(t.code) for t in trs], "Y": yrs,
                      # [§91] 신호일(진입 전날) 상한가 마감 — '상한가 마감 신호 제외' 축
                      "lu": SFL.limitup_flags(df, [t.code for t in trs], [t.entry_date for t in trs], keys=lu_keys)})

    def exits(st, tk):
        """(손절, 익절) 재청산 결과 — 처음 쓸 때 계산해 (청산일 int32, 순수익 float32)만 둔다(촘촘 탐색 수백 조합도 메모리 작게).
        apply_exits 는 원래 매매 순서 그대로 돌려준다."""
        k = (st, tk)
        if k not in ex:
            out = apply_exits(trs, panel, st, tk, "close") if (st is not None or tk is not None) else list(trs)
            ex[k] = (np.array([int(t.exit_date) for t in out], np.int32), np.array([float(t.net_pct) for t in out], np.float32))
        xd, nt = ex[k]
        return B.assign(exit_date=xd, net=nt.astype(float))

    def evaluate(fam, combos, table):
        s_ = sc[fam]
        by = {}
        for k in combos:
            by.setdefault((k[1], k[2]), []).append((k[0], k[3] if len(k) > 3 else False))
        for (st, tk), tls in by.items():
            D = exits(st, tk).assign(score=np.where(np.isnan(s_), -1.0, s_), _s=s_)
            byY = {Y: g for Y, g in D.groupby("Y")}
            for th, lu in tls:
                stats = {}
                for Y in years:
                    g = byY.get(Y)
                    if g is not None and lu:
                        g = g[~g["lu"]]                        # [§91] 상한가 마감 신호 제외
                    # 라이브처럼 강도 기록이 없는 신호는 통과(맨 뒤 순서) — fail-open
                    R = g if (g is None or th is None) else g[(g["_s"] >= th) | g["_s"].isna()]
                    stats[Y] = year_sim(R)
                table[_key(th, st, tk, lu)] = stats

    res = {"n_trades": len(trs), "years": years, "families": {}, "current": [], "fine": bool(fine)}
    tables = {}
    for fam in FAMILIES:
        s_ = sc.get(fam)
        if s_ is None or np.isnan(s_).all():
            res["families"][fam] = {"error": "점수 없음(모델에 없는 전략이거나 학습 표본 부족)"}
            continue
        coarse = [_key(th, st, tk, lu) for th in [None] + TH for st in STOPS for tk in TAKES for lu in (False, True)]
        cur = [_key(r["th"], r["stop"], r["take"], r.get("lu", False)) for r in cur_rows if r["family"] == fam]
        table = {}
        evaluate(fam, sorted(set(coarse) | set(cur), key=str), table)
        search = {k: table[k] for k in coarse}          # 고르는 대상 = 격자(지금 설정값은 비교용으로만)
        seeds = []
        if fine:
            more, seeds = fine_combos(search, years)
            evaluate(fam, more, table)
            for k in more:
                search[k] = table[k]
        tables[fam] = table
        r_ = choose(search, years) or {"error": "선택 불가"}
        if seeds:
            r_["seeds"] = [dict(zip(KEYS, k)) for k in seeds]
        res["families"][fam] = r_
    for r in cur_rows:
        t = tables.get(r["family"])
        k = _key(r["th"], r["stop"], r["take"], r.get("lu", False))
        res["current"].append(dict(r, stats=summarize(t[k]) if t and k in t else None))
    res["sec"] = round(time.time() - t0, 1)
    res["n_exits"] = len(ex)
    log(f"  [{engine}] 매매 {len(trs):,} · 조합 {sum(len(t) for t in tables.values())} · 재청산 {len(ex)} · {res['sec']}초 · "
        + " / ".join(f"{f}: {v.get('status', v.get('error'))} {v.get('best') if v.get('status') == '권장' else ''}"
                     for f, v in res["families"].items()))
    return res


def run(names=None, log=print, fine=False):
    from strategies.daily_loader import load_macro_daily
    import strategy_engine as E
    import ai_model as AIM
    t0 = time.time()
    tg, cur = targets()
    names = names or tg
    smap = {s.name: s for s in E.ALL_STRATEGIES}
    names = [n for n in names if n in smap]
    df = load_macro_daily()
    df["date"] = df["date"].astype(str)
    df = df[df["date"] >= f"{FROM_YEAR - 3}0101"].reset_index(drop=True)   # 워밍업(최대 ~560거래일) 여유만 남겨 메모리 절약
    df["code"] = df["code"].astype(str).str.zfill(6)
    T_all = AIM.load_tables()
    import signal_filters as SFL
    lu_keys = SFL.limitup_entry_keys(df)          # [§91] 상한가 마감 신호일 → 진입 열쇠(전략마다 다시 만들지 않게 한 번만)
    old = {}
    try:
        old = json.load(open(out_path(), encoding="utf-8")).get("engines") or {}
    except Exception:
        pass
    eng = dict(old)
    for n in names:
        try:
            eng[n] = sweep_engine(n, df, smap, T_all, cur.get(n, []), log=log, fine=fine, lu_keys=lu_keys)
            import gc
            gc.collect()
            eng[n]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            eng[n] = {"error": f"{type(e).__name__}: {str(e)[:150]}", "updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
            log(f"  [{n}] 실패: {eng[n]['error']}")
    out = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "from_year": FROM_YEAR, "fine": bool(fine),
           "grid": {"th": TH, "stop": STOPS, "take": TAKES, "lu": [False, True],
                    "fine": {"seeds": FINE_SEEDS, "th": FINE_TH, "stop": FINE_ST, "take": FINE_TK, "smooth": SMOOTH} if fine else None},
           "rule": {"mdd_w": MDD_W, "min_trades_year": MIN_TRADES_YEAR, "win_share": WIN_SHARE, "exit_mode": "close"},
           "engines": eng, "sec": round(time.time() - t0)}
    os.makedirs(os.path.dirname(out_path()), exist_ok=True)
    tmp = out_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, out_path())
    return out


def load():
    try:
        return json.load(open(out_path(), encoding="utf-8"))
    except Exception:
        return None


def main():
    os.chdir(HERE)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default="")
    ap.add_argument("--fine", action="store_true", help="[§88] 주말 촘촘 탐색(강도 0.1·손절 1%p·익절 2%p, 좋은 구간 3곳)")
    a = ap.parse_args()
    out = run([x for x in a.names.split(",") if x] or None, fine=a.fine)
    rec = [f"{e}({f}): {r['best']}" for e, v in out["engines"].items() for f, r in (v.get("families") or {}).items()
           if isinstance(r, dict) and r.get("status") == "권장"]
    try:
        from notifier import safe_send
        safe_send(f"🎯 [강도·손절·익절 탐색{' · 주말 촘촘' if out.get('fine') else ''}] {len(out['engines'])}전략 · {out['sec']}초 · 권장 {len(rec)}건"
                  + ("\n" + "\n".join("  · " + x for x in rec[:12]) if rec else ""))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
