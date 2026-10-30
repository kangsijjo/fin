# -*- coding: utf-8 -*-
"""
strategy_reco.py — 지금까지 돌린 백테스트를 종합해 '안 고른 좋은 전략'을 추천한다. (2026-10-30, 설명서 §86)

근거(사용자 결정: 전부 · 균형)
  ① 기간 백테스트 기록 전부(db/period_bt/pbt_*.json + archive/) — 사용자가 돌린 여러 기간·강도·손절 실험
  ② 주간 전체 백테스트(results/strategy_full_*.csv 최신) — 전체 기간 + 최근 2년(표본 밖)
  ②' [§88] 강도·손절·익절 탐색(db/setting_sweep.json)의 표본 밖 성적 — '그 해 이전만 보고 고른 설정'으로 했을 때
  ③ (화면 표시) 개선 AI 의 전략별 검증(같은 날 상위 2개 효과), 전략 겹침 표
점수(균형) — 기록마다 그 실험 안에서의 상대 순위로 매긴다(약세장 기간이면 모두 낮으니 절대값 대신 순위):
  0.35 × CAGR 순위 + 0.25 × 샤프 순위 + 0.20 × MDD 순위(얕을수록 위) + 0.20 × 해마다 플러스였던 비율
  가중치 = 기간 연수(최대 5) × 최근성(60일마다 절반) × 계산 규칙(§64 수정 전 결과는 0.5) × 비교 전략 수(8개 미만이면 줄임)
  전략 점수 = 가중 평균을 '근거 1단위'만큼 0.5(보통) 쪽으로 당김 — 한두 번 좋았던 전략이 1등이 되지 않게.
추천 조건: 점수 ≥ 0.60 · 가중 평균 CAGR > 0 · 플러스 비율 ≥ 50% · 가중 평균 MDD > −60%, 그 자리(계좌·포트)에 안 고른 전략.
권장 설정: 같은 전략의 강도·손절 실험 중 점수가 가장 높은 조합(근거 2회 이상이고 기본보다 높을 때만).
주의: 과거 성적 순위다 — 59개 중 위를 고르면 운이 섞인다(§52 SPA p≈0.4). 추천은 '검토할 후보'이지 보증이 아님.
"""
import glob
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

W = {"cagr": 0.35, "sharpe": 0.25, "mdd": 0.20, "pos": 0.20}
MIN_SCORE = 0.60
SHRINK_W = 1.0
HALF_LIFE_DAYS = 60
RULE_CHANGE = "2026-10-24 00:00"     # §64 계산 수정(상폐·분할·정리매매·상한가·거래세) 반영 이후 결과만 온전히
TOP_N = 5


def _jobs_dir():
    return os.getenv("PBT_DIR") or os.path.join(HERE, "db", "period_bt")


def _pct_rank(vals):
    """값 목록 → 0~1 순위(클수록 좋음, 동점 평균). None 은 None."""
    idx = [i for i, v in enumerate(vals) if v is not None]
    out = [None] * len(vals)
    if not idx:
        return out
    srt = sorted(idx, key=lambda i: vals[i])
    n = len(srt)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[srt[j + 1]] == vals[srt[i]]:
            j += 1
        r = ((i + j) / 2) / (n - 1) if n > 1 else 0.5
        for k in range(i, j + 1):
            out[srt[k]] = r
        i = j + 1
    return out


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _years(a, b):
    try:
        return max((datetime.strptime(str(b), "%Y%m%d") - datetime.strptime(str(a), "%Y%m%d")).days / 365.25, 0.0)
    except Exception:
        return 0.0


def _obs_from_rows(rows, years, weight, source, variant_of):
    """한 실험의 결과 줄들 → 관측치(전략·조합별 점수). rows: dict(cagr, sharpe, mdd, yearly, engine, variant)."""
    cg = _pct_rank([_f(r.get("cagr")) for r in rows])
    sh = _pct_rank([_f(r.get("sharpe")) for r in rows])
    md = _pct_rank([_f(r.get("mdd")) for r in rows])
    cohort = min(1.0, len(rows) / 8.0)
    out = []
    for r, a, b, c in zip(rows, cg, sh, md):
        if a is None:
            continue
        b = a if b is None else b
        c = a if c is None else c
        yr = [v for v in (r.get("yearly") or {}).values() if _f(v) is not None]
        pos = (sum(1 for v in yr if float(v) > 0) / len(yr)) if yr else (1.0 if (_f(r.get("cagr")) or 0) > 0 else 0.0)
        s = W["cagr"] * a + W["sharpe"] * b + W["mdd"] * c + W["pos"] * pos
        out.append({"engine": r["engine"], "variant": variant_of(r), "score": s, "w": weight * cohort,
                    "cagr": _f(r.get("cagr")), "mdd": _f(r.get("mdd")), "sharpe": _f(r.get("sharpe")),
                    "pos": pos, "years": years, "source": source})
    return out


def load_observations(now=None):
    now = now or datetime.now()
    obs, n_jobs = [], 0
    files = glob.glob(os.path.join(_jobs_dir(), "pbt_*.json")) + glob.glob(os.path.join(_jobs_dir(), "archive", "pbt_*.json"))
    for p in sorted(set(files)):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if d.get("status") != "done":
            continue
        prm = d.get("params") or {}
        start, end = prm.get("start"), prm.get("eff_end") or prm.get("end")
        yrs = _years(start, end)
        if yrs < 0.25:
            continue
        fin = str(d.get("finished") or d.get("started") or "")
        try:
            age = (now - datetime.strptime(fin[:16], "%Y-%m-%d %H:%M")).days
        except Exception:
            age = 365
        rule = 1.0 if fin[:16] >= RULE_CHANGE else 0.5
        w = min(yrs, 5.0) * (0.5 ** (max(age, 0) / HALF_LIFE_DAYS)) * rule
        sv = prm.get("strength_version") or "v1"
        stop, take = prm.get("stop"), prm.get("take")
        base_rows, str_rows = [], []
        for r in d.get("results") or []:
            if r.get("error") or r.get("cagr") is None:
                continue
            e = r.get("base") or r.get("strategy")
            rr = dict(r, engine=e)
            (str_rows if r.get("strength_min") is not None else base_rows).append(rr)

        def vk(r, sv=sv, stop=stop, take=take):
            th = r.get("strength_min")
            return json.dumps({"sv": sv if th is not None else None, "th": th, "stop": stop, "take": take}, sort_keys=True)
        if len(base_rows) >= 2:
            n_jobs += 1
            obs += _obs_from_rows(base_rows, yrs, w, f"기간 {start}~{end}", vk)
            # 강도 줄은 같은 실험의 기본 줄 + 강도 줄을 한 무리로 순위(같은 기간 안에서 비교)
            if str_rows:
                both = _obs_from_rows(base_rows + str_rows, yrs, w, f"기간 {start}~{end}", vk)
                obs += [o for o in both if json.loads(o["variant"])["th"] is not None]
    # 주간 전체 백테스트(최신)
    fs = sorted(glob.glob(os.path.join(HERE, "results", "strategy_full_*.csv")))
    full_info = None
    if fs:
        import pandas as pd
        try:
            F = pd.read_csv(fs[-1])
            full_info = os.path.basename(fs[-1])
            base_v = json.dumps({"sv": None, "th": None, "stop": None, "take": None}, sort_keys=True)
            rows = [{"engine": r["strategy"], "cagr": r.get("real_cagr_pct"), "sharpe": r.get("real_sharpe"),
                     "mdd": r.get("real_mdd_pct"),
                     "yearly": {"full": r.get("real_cagr_pct"), "oos": r.get("oos_cagr_pct")}} for _, r in F.iterrows()]
            y_full = _years(str(F["period_start"].iloc[0]), str(F["period_end"].iloc[0]))
            obs += _obs_from_rows(rows, y_full, min(y_full, 5.0), "주간 전체(2018~)", lambda r: base_v)
            rows = [{"engine": r["strategy"], "cagr": r.get("oos_cagr_pct"), "sharpe": None, "mdd": r.get("oos_mdd_pct"),
                     "yearly": {}} for _, r in F.iterrows()]
            obs += _obs_from_rows(rows, 2.0, 2.0, "주간 최근 2년(표본 밖)", lambda r: base_v)
        except Exception:
            pass
    # [§88] 강도·손절·익절 탐색의 '표본 밖' 성적 — 해 Y 를 'Y 이전만 보고 고른 설정'으로 했을 때(강도 v2 기준, 모의계좌 방식)
    #   설정을 잘 맞추면 좋아지는 전략이 추천에 올라오게. 탐색한 전략끼리 순위.
    sweep_info = None
    try:
        sp = os.getenv("SETTING_SWEEP_PATH") or os.path.join(HERE, "db", "setting_sweep.json")
        sw = json.load(open(sp, encoding="utf-8"))
        gen = str(sw.get("generated") or "")
        try:
            age = (now - datetime.strptime(gen[:16], "%Y-%m-%d %H:%M")).days
        except Exception:
            age = 30
        rows, nys = [], []
        for e, v in (sw.get("engines") or {}).items():
            f = ((v or {}).get("families") or {}).get("v2") or {}
            oy = f.get("oos_years") or {}
            if len(oy) < 2:
                continue
            rets = [float(x[0]) for x in oy.values()]
            comp = 1.0
            for r_ in rets:
                comp *= 1 + r_ / 100
            rows.append({"engine": e, "cagr": (comp ** (1 / len(rets)) - 1) * 100,
                         "mdd": sum(float(x[1]) for x in oy.values()) / len(oy), "sharpe": None,
                         "yearly": {y: x[0] for y, x in oy.items()}})
            nys.append(len(oy))
        if len(rows) >= 2:
            yrs_ = sum(nys) / len(nys)
            base_v = json.dumps({"sv": None, "th": None, "stop": None, "take": None}, sort_keys=True)
            obs += _obs_from_rows(rows, yrs_, min(yrs_, 5.0) * (0.5 ** (max(age, 0) / HALF_LIFE_DAYS)),
                                  "설정 탐색(표본 밖, 그 해 이전만 보고 고른 설정)", lambda r: base_v)
            sweep_info = {"generated": gen, "n": len(rows)}
    except Exception:
        pass
    return obs, {"n_jobs": n_jobs, "full": full_info, "sweep": sweep_info}


def _agg(os_):
    sw = sum(o["w"] for o in os_)
    if sw <= 0:
        return None
    m = lambda k: sum(o["w"] * o[k] for o in os_ if o[k] is not None) / max(sum(o["w"] for o in os_ if o[k] is not None), 1e-9)  # noqa: E731
    return {"score": (sum(o["w"] * o["score"] for o in os_) + 0.5 * SHRINK_W) / (sw + SHRINK_W),
            "raw": sum(o["w"] * o["score"] for o in os_) / sw, "w": sw, "n": len(os_),
            "cagr": m("cagr"), "mdd": m("mdd"), "pos": m("pos"),
            "sharpe": (m("sharpe") if any(o["sharpe"] is not None for o in os_) else None)}


def variant_text(v):
    v = json.loads(v) if isinstance(v, str) else v
    parts = []
    if v.get("th") is not None:
        parts.append(f"강도 {('AI' if v.get('sv') == 'ai' else v.get('sv') or 'v1')} ≥{v['th']:g}")
    if v.get("stop") is not None:
        parts.append(f"손절 {v['stop']:g}%")
    if v.get("take") is not None:
        parts.append(f"익절 +{v['take']:g}%")
    return " · ".join(parts) or "기본(강도·손절 없음)"


def evaluate(now=None):
    """전략별 종합 평가 → {엔진: {score, cagr, mdd, pos, sharpe, n, w, best_variant, best, base}} + 정보."""
    obs, info = load_observations(now)
    by = {}
    for o in obs:
        by.setdefault(o["engine"], []).append(o)
    out = {}
    for e, os_ in by.items():
        # 전략 자체 평가 = 강도로 거르지 않은 줄(기본·손절/익절 실험 포함) — 강도 줄은 '권장 설정' 비교에만
        a = _agg([o for o in os_ if json.loads(o["variant"])["th"] is None])
        if not a:
            continue
        vs = {}
        for o in os_:
            vs.setdefault(o["variant"], []).append(o)
        base_key = json.dumps({"sv": None, "th": None, "stop": None, "take": None}, sort_keys=True)
        base = _agg(vs.get(base_key, [])) if base_key in vs else None
        best_k, best = None, None
        for k, l in vs.items():
            if k == base_key or len(l) < 2:
                continue
            g = _agg(l)
            if g and (best is None or g["score"] > best["score"]):
                best_k, best = k, g
        if best is not None and base is not None and best["score"] <= base["score"]:
            best_k, best = None, None
        a.update(engine=e, base=base, best_variant=best_k, best=best,
                 sources=sorted({o["source"] for o in os_})[:12])
        out[e] = a
    return out, info


def _overlap(matrix, a, b):
    if not matrix:
        return 0.0
    s = matrix.get("same") or {}
    return max(float((s.get(a) or {}).get(b) or 0), float((s.get(b) or {}).get(a) or 0))


def recommend(place, selected, engines=None, now=None, top=TOP_N, ev=None):
    """그 자리(kiwoom|kis|ai|strength)에 안 고른 전략 중 추천 → (목록, 정보)."""
    ev, info = ev if ev is not None else evaluate(now)
    sel = set(selected or [])
    try:
        import strategy_overlap as OV
        mat = OV.load()
    except Exception:
        mat = None
    try:
        import virtual_config as VC
        usage = VC.strategy_usage()
    except Exception:
        usage = {}
    ai_val = {}
    try:
        import ai_model as AIM
        m = AIM.meta_info() or {}
        ai_val = {r["engine"]: r for r in ((m.get("validation") or {}).get("engines") or [])}
        ai_eng = set(m.get("engines") or [])
    except Exception:
        ai_eng = set()
    cand = []
    for e, a in ev.items():
        if e in sel or (engines is not None and e not in engines):
            continue
        if a["score"] < MIN_SCORE or (a["cagr"] or 0) <= 0 or a["pos"] < 0.5 or (a["mdd"] is not None and a["mdd"] <= -60):
            continue
        ov = sorted(((s, _overlap(mat, e, s)) for s in sel), key=lambda x: -x[1])
        ov = [{"engine": s, "same": round(p, 1)} for s, p in ov if p >= 50]
        used = [u for u in usage.get(e, []) if u["place"] != place]
        bv = a.get("best_variant")
        cand.append({
            "engine": e, "score": round(a["score"] * 100), "cagr": round(a["cagr"], 1),
            "mdd": round(a["mdd"], 1) if a["mdd"] is not None else None,
            "sharpe": round(a["sharpe"], 2) if a["sharpe"] is not None else None,
            "pos": round(a["pos"] * 100), "n": a["n"], "weight": round(a["w"], 1),
            "best": ({"text": variant_text(bv), **json.loads(bv), "score": round(a["best"]["score"] * 100),
                      "cagr": round(a["best"]["cagr"], 1), "n": a["best"]["n"]} if bv else None),
            "overlap": ov, "used_elsewhere": [{"place": u["place"], "label": u["label"]} for u in used],
            "ai": ai_val.get(e), "in_ai_model": e in ai_eng})
    cand.sort(key=lambda c: (bool(c["overlap"]), -c["score"]))   # 겹침 없는 전략 먼저, 그 안에서 점수 순
    return cand[:top], dict(info, n_eval=len(ev), min_score=round(MIN_SCORE * 100))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ev, info = evaluate()
    print(info)
    for e, a in sorted(ev.items(), key=lambda kv: -kv[1]["score"])[:20]:
        print(f"{e:24s} 점수 {a['score'] * 100:5.1f}  CAGR {a['cagr']:+6.1f}  MDD {a['mdd'] if a['mdd'] is None else round(a['mdd'], 1)}"
              f"  플러스 {a['pos'] * 100:4.0f}%  근거 {a['n']}건(가중 {a['w']:.1f})  권장 {variant_text(a['best_variant']) if a['best_variant'] else '-'}")


if __name__ == "__main__":
    main()
