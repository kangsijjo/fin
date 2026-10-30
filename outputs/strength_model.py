# -*- coding: utf-8 -*-
"""
strength_model.py — 강도 v2: **전략별** 가중치 + 방향 안정성 필터 + 축소 + 시장 지표 제외. (2026-10-22)

왜(v1 의 구조 문제, 설명서 §62)
  · v1 가중치(IC)는 라이브 6전략 매매를 한데 섞어 학습했는데 그중 66% 가 rsi_reversal → '떨어진 종목을 좋아하는'
    잣대가 되어 추세형 전략엔 거꾸로 작동했다(신고가 전략 5.7 통과 0건, 골든크로스는 강도가 높을수록 나빴음).
  · 지표의 35% 가 시장 공통값(코스피 20일·VIX·반도체·환율)이라 같은 날 후보끼리는 순위를 못 가르고 '시장 타이밍'만 됐다.
v2 방식
  ① 그 전략 자신의 과거 매매(백테스트 엔진, 2018-09~)로 지표별 IC(순위상관)를 학습한다.
  ② 해마다 IC 를 따로 재서 **같은 방향이 2/3 이상의 해에서 유지된 지표만** 남긴다(우연한 상관 제거).
  ③ 표본이 적을수록 IC 를 0 쪽으로 줄인다: IC × n/(n+1000).
  ④ 시장 공통 지표 5개는 뺀다(종목 고르기에 도움 안 됨 — 시장 국면은 전략의 _mkt 게이트가 맡음).
  ⑤ 점수식은 v1 과 같다(백분위 × IC, available-only, 0~10, 5=보통) — 기준값 화면을 그대로 쓴다.
검증(워크포워드 2021-01~2026-10, 해마다 그 이전 매매로만 학습, 10슬롯 단독, 분석환경 실측):
  강도순 + v2≥5.5 의 5전략 평균 CAGR +6.6% vs 엔진 기본 −3.7% / v1 강도순 −5.9% (v1 ≥5.7 은 거의 매수 0건).

저장: db/strength_v2/{엔진이름}.json(가중치·기준분포 1001분위) + {엔진이름}_train.pkl(학습 표본: 매매+피처, 백테스트 재사용)
사용: python strength_model.py --train            # 설정(live_config)의 양 계좌 전략 전부
      python strength_model.py --train gc_for3d,star_high_500d_20
"""
import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
MODEL_DIR = os.path.join(HERE, "db", "strength_v2")
VERSION = 1
MKT_FEATURES = ("vix", "vix_chg_5d", "kospi_ret_20d", "sox_ret_5d", "usdkrw_chg_5d")
SHRINK_K = 1000
MIN_TRAIN = 300             # 이보다 적으면 모델을 만들지 않는다(→ v1 로 대체)
MIN_YEAR_N = 40             # 해마다 IC 를 잴 최소 표본
STABLE_SHARE = 2 / 3
TRAIN_FROM = "20180901"
QGRID = np.linspace(0, 1, 1001)


def _feats():
    from strength_backtest import _ic_constants
    f, cov, _ = _ic_constants()
    return [x for x in f if x not in MKT_FEATURES], cov


def fit(T):
    """학습 표본(T: net_pct·year·피처 열) → 모델 dict {ic, ref(1001분위), n, years} 또는 None(표본 부족)."""
    from scipy.stats import spearmanr
    feats, _ = _feats()
    T = T[T["net_pct"].notna()]
    n = len(T)
    if n < MIN_TRAIN:
        return None
    y = T["net_pct"].astype(float)
    ic, ref = {}, {}
    for f in feats:
        if f not in T.columns:
            continue
        x = T[f].astype(float)
        m = x.notna()
        if m.sum() < 100:
            continue
        r, _ = spearmanr(x[m], y[m])
        if r != r:
            continue
        agree = []
        for _, g in T[m].groupby("year"):
            if len(g) < MIN_YEAR_N:
                continue
            ry, _ = spearmanr(g[f].astype(float), g["net_pct"].astype(float))
            if ry == ry:
                agree.append(np.sign(ry) == np.sign(r))
        if len(agree) < 2 or np.mean(agree) < STABLE_SHARE:
            continue                                          # 해마다 방향이 뒤집히는 지표는 버린다
        ic[f] = float(r) * n / (n + SHRINK_K)
        ref[f] = np.quantile(x[m].to_numpy(float), QGRID).tolist()
    return {"ic": ic, "ref": ref, "n": int(n), "years": sorted(T["year"].unique().tolist())}


def score_frame(X, model):
    """피처 표(X) → 강도 v2 점수 배열(0~10). 모델 없으면 NaN."""
    from strength_backtest import score_rows
    if not model or not model.get("ic"):
        return np.full(len(X), np.nan)
    _, cov = _feats()
    ref = {f: np.asarray(v, float) for f, v in model["ref"].items()}
    return score_rows(X, model["ic"], ref, cov)


def model_path(engine):
    return os.path.join(MODEL_DIR, f"{engine}.json")


def train_path(engine):
    return os.path.join(MODEL_DIR, f"{engine}_train.pkl")


_CACHE = {}


def load_model(engine):
    p = model_path(engine)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    c = _CACHE.get(engine)
    if c and c[0] == mt:
        return c[1]
    try:
        m = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    _CACHE[engine] = (mt, m)
    return m


def score(engine, feats):
    """라이브 1건 채점: 엔진 이름 + 피처 dict(factor_scorer.build_feature_vec 결과) → 0~10 또는 None(모델 없음)."""
    m = load_model(engine)
    if not m:
        return None
    X = pd.DataFrame([{f: (np.nan if feats.get(f) is None else feats.get(f)) for f in m["ic"]}])
    v = score_frame(X, m)[0]
    return None if v != v else round(float(v), 2)


_PCT_CACHE = {}


def _v1_ref(live_name):
    """v1 점수(score_ic)의 그 전략 과거 분포 — 신호 강도 기록(db/signal_strength_log.csv)에서."""
    key = ("v1", live_name)
    if key in _PCT_CACHE:
        return _PCT_CACHE[key]
    ref = None
    try:
        L = pd.read_csv(os.path.join(HERE, "db", "signal_strength_log.csv"), usecols=["strategy", "score_ic"],
                        encoding="utf-8-sig")
        v = pd.to_numeric(L.loc[L["strategy"] == live_name, "score_ic"], errors="coerce").dropna().to_numpy(float)
        if len(v) >= 50:
            ref = np.quantile(v, np.linspace(0, 1, 101))
    except Exception:
        ref = None
    _PCT_CACHE[key] = ref
    return ref


def _v2_ref(engine):
    """v2 점수의 그 전략 분포 — 모델 파일의 score_ref, 없으면(옛 모델) 학습 표본을 채점해 만든다(프로세스 안 캐시)."""
    m = load_model(engine)
    if not m:
        return None
    if m.get("score_ref"):
        return np.asarray(m["score_ref"], float)
    key = ("v2", engine, m.get("trained"))
    if key in _PCT_CACHE:
        return _PCT_CACHE[key]
    ref = None
    try:
        T = pd.read_pickle(train_path(engine))
        sc = score_frame(T, m)
        sc = sc[~np.isnan(sc)]
        if len(sc) >= 50:
            ref = np.quantile(sc, np.linspace(0, 1, 101))
    except Exception:
        ref = None
    _PCT_CACHE[key] = ref
    return ref


def rank_pct(engine, live_name, score, version="v2"):
    """[§75] '칸 없음' 모드의 후보 순서용 — 그 점수가 **그 전략 과거 점수 중 상위 몇 %쯤**인지(0~100, 클수록 강함).
    전략마다 점수 척도가 달라(v2 는 전략별 학습) 원점수끼리 비교하면 점수가 후하게 나오는 전략이 칸을 다 차지한다.
    분포를 모르면 원점수×10(0~10 → 0~100)으로 근사. 점수가 없으면 None."""
    if score is None or score != score:
        return None
    ref = _v2_ref(engine) if version == "v2" else None
    if ref is None:
        ref = _v1_ref(live_name)
    if ref is None:
        return float(score) * 10.0
    return float(np.interp(float(score), ref, np.linspace(0, 100, len(ref))))


def build_train_table(engine, df, log=print):
    """엔진 백테스트(전체 기간) → 매매 + 신호일 피처 20개(strength_backtest, 학습 정의와 100% 일치 검증)."""
    import strategy_engine as E
    import strength_backtest as S
    smap = {s.name: s for s in E.ALL_STRATEGIES}
    trs = [t for t in (smap[engine].backtest(df.copy(), E.DEFAULT_COSTS) or []) if str(t.entry_date) >= TRAIN_FROM]
    T = pd.DataFrame({"code": [str(t.code).zfill(6) for t in trs], "entry_date": [str(t.entry_date) for t in trs],
                      "exit_date": [str(t.exit_date) for t in trs], "net_pct": [float(t.net_pct) for t in trs]})
    if T.empty:
        return T
    T["date"] = S.sig_dates_for(df, trs)
    X = S.features_cached(df, T[["code", "date"]], log=log)
    T = pd.concat([T.reset_index(drop=True), X.drop(columns=["code", "date"]).reset_index(drop=True)], axis=1)
    T["year"] = T["entry_date"].str[:4]
    return T


def train(engines, df=None, log=print):
    """전략별 학습·저장 → {엔진: 요약}. 표본 부족이면 모델을 만들지 않고 이유를 남긴다."""
    from strategies.daily_loader import load_macro_daily
    os.makedirs(MODEL_DIR, exist_ok=True)
    if df is None:
        df = load_macro_daily()
        df["date"] = df["date"].astype(str)
        df["code"] = df["code"].astype(str).str.zfill(6)
    out = {}
    for e in engines:
        try:
            T = build_train_table(e, df, log=log)
            T.to_pickle(train_path(e))
            m = fit(T)
            if m is None:
                out[e] = {"ok": False, "why": f"학습 매매 {len(T)}건 < {MIN_TRAIN} — v1 로 대체"}
                continue
            m.update(engine=e, version=VERSION, trained=datetime.now().strftime("%Y-%m-%d %H:%M"),
                     data_max=str(df["date"].max()))
            try:                                   # [§75] 점수 분포(101분위) — '칸 없음' 모드의 전략 간 비교(백분위)용
                sc = score_frame(T, m)
                sc = sc[~np.isnan(sc)]
                if len(sc) >= 50:
                    m["score_ref"] = np.quantile(sc, np.linspace(0, 1, 101)).round(4).tolist()
            except Exception:
                pass
            tmp = model_path(e) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(m, f, ensure_ascii=False)
            os.replace(tmp, model_path(e))
            top = sorted(m["ic"].items(), key=lambda kv: -abs(kv[1]))[:3]
            out[e] = {"ok": True, "n": m["n"], "n_ic": len(m["ic"]), "top": [f"{k} {v:+.3f}" for k, v in top]}
        except Exception as ex:
            out[e] = {"ok": False, "why": f"{type(ex).__name__}: {str(ex)[:120]}"}
        log(f"[strength_v2] {e}: {out[e]}")
    return out


def walk_forward(T_train, X, years):
    """백테스트용: X(기간 안 매매 피처)의 해 Y 행은 T_train 중 Y 이전 해만으로 학습한 모델로 채점. → (점수, 정보)"""
    out = np.full(len(X), np.nan)
    info = {}
    yrs = np.asarray(years)
    for Y in sorted(set(yrs)):
        m = fit(T_train[T_train["year"] < Y]) if T_train is not None and len(T_train) else None
        if not m:
            info[Y] = {"n_train": int((T_train["year"] < Y).sum()) if T_train is not None and len(T_train) else 0,
                       "skipped": True, "n_ic": 0, "top": []}
            continue
        mask = yrs == Y
        out[mask] = score_frame(X[mask], m)
        info[Y] = {"n_train": m["n"], "n_ic": len(m["ic"]), "skipped": False,
                   "top": [f"{k} {v:+.3f}" for k, v in sorted(m["ic"].items(), key=lambda kv: -abs(kv[1]))[:3]]}
    return out, info


def configured_engines():
    import live_config as LC
    cfg = LC.load()
    return sorted({s["engine"] for a in LC.ACCOUNTS for s in cfg["accounts"][a]["strategies"]})


def status():
    """대시보드용 {엔진: {trained, n, n_ic} | None}."""
    out = {}
    for e in configured_engines():
        m = load_model(e)
        out[e] = None if not m else {"trained": m.get("trained"), "n": m.get("n"), "n_ic": len(m.get("ic", {}))}
    return out


def main():
    os.chdir(HERE)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", nargs="?", const="", default=None, help="엔진 이름(쉼표) — 비우면 설정의 전략 전부")
    ap.add_argument("--missing-only", action="store_true", help="모델이 없는 전략만")
    a = ap.parse_args()
    if a.train is None:
        print(json.dumps(status(), ensure_ascii=False, indent=1))
        return
    names = [x for x in a.train.split(",") if x] or configured_engines()
    if a.missing_only:
        names = [n for n in names if not load_model(n)]
    if not names:
        print("[strength_v2] 학습할 전략 없음")
        return
    res = train(names)
    bad = [k for k, v in res.items() if not v.get("ok")]
    try:
        from notifier import safe_send
        safe_send("🧮 [강도 v2 학습] " + " / ".join(f"{k} {'✅' if v.get('ok') else '⚠ ' + v.get('why', '')}"
                                                for k, v in res.items()))
    except Exception:
        pass
    sys.exit(0 if not bad or len(bad) < len(res) else 1)


if __name__ == "__main__":
    main()
